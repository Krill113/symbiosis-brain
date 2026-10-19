from __future__ import annotations

import hashlib
import base64
import ctypes
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest


HELPER = "from symbiosis_brain.adapters.opencode.conditional_replace import main; main()"


def _run(request: dict[str, object], *, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, "-c", HELPER], input=json.dumps(request), text=True, encoding="utf-8",
                          capture_output=True, timeout=15, check=False, env=env)


def _barrier_child(target: Path, tmp_path: Path, phase: str) -> tuple[subprocess.Popen[str], Path, Path]:
    ready, release = tmp_path / f"{phase}.ready", tmp_path / f"{phase}.release"
    env = os.environ.copy()
    env["SB_TEST_SANDBOX"] = str(tmp_path)
    request = {"path": str(target), "expectedSha256": hashlib.sha256(target.read_bytes()).hexdigest(),
               "replacementBase64": base64.b64encode(b'{"model":"installed"}').decode("ascii"),
               "testBarrier": {"phase": phase, "ready": str(ready), "release": str(release)}}
    child = subprocess.Popen([sys.executable, "-c", HELPER], stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", env=env)
    assert child.stdin is not None
    child.stdin.write(json.dumps(request))
    child.stdin.close()
    child.stdin = None
    deadline = time.monotonic() + 10
    while not ready.exists() and child.poll() is None and time.monotonic() < deadline:
        time.sleep(.01)
    assert ready.exists(), f"helper did not reach {phase} barrier"
    return child, ready, release


def _create_file_symlink(link: Path, target: Path) -> bool:
    create_link = ctypes.WinDLL("kernel32", use_last_error=True).CreateSymbolicLinkW
    create_link.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint32]
    create_link.restype = ctypes.c_ubyte
    # SYMBOLIC_LINK_FLAG_ALLOW_UNPRIVILEGED_CREATE, Windows 10+
    if create_link(str(link), str(target), 0x2):
        return True
    error = ctypes.get_last_error()
    if error in {5, 1314, 50}:
        pytest.skip(f"host does not permit creating a test reparse point (WinError {error})")
    raise OSError(error, "CreateSymbolicLinkW failed", str(link))


@pytest.mark.skipif(os.name != "nt", reason="conditional commit intentionally fails closed off Windows")
def test_intervening_write_before_protected_commit_is_a_conflict(tmp_path: Path) -> None:
    target = tmp_path / "config.jsonc"
    target.write_text('{"model":"before"}', encoding="utf-8")
    observed_hash = hashlib.sha256(target.read_bytes()).hexdigest()
    user_version = '{"model":"concurrent-user-edit"}'
    target.write_text(user_version, encoding="utf-8")
    request = {"path": str(target), "expectedSha256": observed_hash,
               "replacementBase64": base64.b64encode(b'{"model":"installed"}').decode("ascii")}
    result = subprocess.run([sys.executable, "-c", HELPER], input=json.dumps(request), text=True, encoding="utf-8",
                            capture_output=True, timeout=10, check=False)
    assert result.returncode != 0
    assert json.loads(result.stdout)["code"] == "conditional-commit-conflict"
    assert target.read_text(encoding="utf-8") == user_version


@pytest.mark.skipif(os.name != "nt", reason="conditional commit intentionally fails closed off Windows")
def test_intervening_write_prevents_owned_file_deletion(tmp_path: Path) -> None:
    target = tmp_path / "owned-command.md"
    target.write_text("adapter command before", encoding="utf-8")
    observed_hash = hashlib.sha256(target.read_bytes()).hexdigest()
    user_version = "user-edited command after observation"
    target.write_text(user_version, encoding="utf-8")
    request = {"path": str(target), "expectedSha256": observed_hash, "replacementBase64": None}
    result = subprocess.run([sys.executable, "-c", HELPER], input=json.dumps(request), text=True, encoding="utf-8",
                            capture_output=True, timeout=10, check=False)
    assert result.returncode != 0
    assert json.loads(result.stdout)["code"] == "conditional-commit-conflict"
    assert target.read_text(encoding="utf-8") == user_version


@pytest.mark.skipif(os.name != "nt", reason="conditional commit intentionally fails closed off Windows")
def test_external_writer_cannot_change_target_after_version_check(tmp_path: Path) -> None:
    target = tmp_path / "config.jsonc"
    target.write_text('{"model":"before"}', encoding="utf-8")
    child, _, release = _barrier_child(target, tmp_path, "validated")
    # Simulate an editor's atomic pathname swap, not a write through the path.
    user_file = tmp_path / "user-version"
    user_file.write_text("user replacement", encoding="utf-8")
    swapped = subprocess.run([sys.executable, "-c",
                              "import os,sys; os.replace(sys.argv[1],sys.argv[2])",
                              str(user_file), str(target)], capture_output=True, text=True, encoding="utf-8", timeout=10, check=False)
    release.write_text("continue", encoding="utf-8")
    stdout, stderr = child.communicate(timeout=10)
    assert swapped.returncode != 0, "open target handle must deny pathname rename/delete"
    assert child.returncode == 0, stderr
    assert json.loads(stdout)["ok"] is True
    assert target.read_text(encoding="utf-8") == '{"model":"installed"}'


@pytest.mark.skipif(os.name != "nt", reason="conditional commit intentionally fails closed off Windows")
@pytest.mark.parametrize("phase", ["prepared", "captured", "committed"])
def test_new_process_recovers_after_helper_crash(tmp_path: Path, phase: str) -> None:
    target = tmp_path / "config.jsonc"
    original = '{"model":"before"}'
    target.write_text(original, encoding="utf-8")
    child, _, _ = _barrier_child(target, tmp_path, phase)
    child.kill()
    child.communicate(timeout=10)
    env = os.environ.copy()
    env["SB_TEST_SANDBOX"] = str(tmp_path)
    result = _run({"path": str(target), "operation": "recover"}, env=env)
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout)["ok"] is True
    if phase in {"prepared", "captured"}:
        assert target.read_text(encoding="utf-8") == original
    else:
        assert target.read_text(encoding="utf-8") == '{"model":"installed"}'


@pytest.mark.skipif(os.name != "nt", reason="conditional commit intentionally fails closed off Windows")
def test_recover_without_journal_returns_clean_without_starting_commit(tmp_path: Path) -> None:
    target = tmp_path / "config.jsonc"
    untouched = b'{"model":"untouched"}'
    target.write_bytes(untouched)
    result = _run({"path": str(target), "operation": "recover",
                   "expectedSha256": hashlib.sha256(untouched).hexdigest(),
                   "replacementBase64": base64.b64encode(b'{"model":"must-not-install"}').decode("ascii")})
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout) == {"ok": True, "state": "clean"}
    assert target.read_bytes() == untouched


@pytest.mark.skipif(os.name != "nt", reason="conditional commit intentionally fails closed off Windows")
def test_recover_without_existing_parent_is_clean_and_nonmutating(tmp_path: Path) -> None:
    target = tmp_path / "not-created" / "config.jsonc"
    result = _run({"path": str(target), "operation": "recover"})
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout) == {"ok": True, "state": "clean"}
    assert not target.parent.exists()


@pytest.mark.skipif(os.name != "nt", reason="conditional commit intentionally fails closed off Windows")
def test_recovery_cleanup_is_idempotent_and_next_commit_works(tmp_path: Path) -> None:
    target = tmp_path / "config.jsonc"
    target.write_text('{"model":"before"}', encoding="utf-8")
    child, _, _ = _barrier_child(target, tmp_path, "committed")
    child.kill()
    child.communicate(timeout=10)
    recovery = _run({"path": str(target), "operation": "recover"})
    assert recovery.returncode == 0, recovery.stdout + recovery.stderr
    installed = b'{"model":"installed"}'
    committed = _run({"path": str(target), "expectedSha256": hashlib.sha256(installed).hexdigest(),
                      "replacementBase64": base64.b64encode(b'{"model":"next"}').decode("ascii")})
    assert committed.returncode == 0, committed.stdout + committed.stderr
    assert target.read_bytes() == b'{"model":"next"}'


@pytest.mark.skipif(os.name != "nt", reason="conditional commit intentionally fails closed off Windows")
def test_delete_crash_cleanup_recovery_does_not_restore_deleted_target(tmp_path: Path) -> None:
    target = tmp_path / "owned.md"
    original = b"owned original"
    target.write_bytes(original)
    request = {"path": str(target), "expectedSha256": hashlib.sha256(original).hexdigest(),
               "replacementBase64": None,
               "testBarrier": {"phase": "committed", "ready": str(tmp_path / "delete.ready"),
                               "release": str(tmp_path / "delete.release")}}
    env = os.environ.copy()
    env["SB_TEST_SANDBOX"] = str(tmp_path)
    child = subprocess.Popen([sys.executable, "-c", HELPER], stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", env=env)
    assert child.stdin is not None
    child.stdin.write(json.dumps(request)); child.stdin.close(); child.stdin = None
    deadline = time.monotonic() + 10
    while not (tmp_path / "delete.ready").exists() and child.poll() is None and time.monotonic() < deadline:
        time.sleep(.01)
    assert (tmp_path / "delete.ready").exists()
    child.kill(); child.communicate(timeout=10)
    result = _run({"path": str(target), "operation": "recover"}, env=env)
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout) == {"ok": True, "state": "absent"}
    assert not target.exists()


@pytest.mark.skipif(os.name != "nt", reason="conditional commit intentionally fails closed off Windows")
@pytest.mark.parametrize("sidecar", ["journal", "capture", "marker"])
def test_recovery_rejects_sidecar_symlink_without_touching_link_or_target(tmp_path: Path, sidecar: str) -> None:
    target = tmp_path / "config.jsonc"
    original = b'{"model":"keep"}'
    target.write_bytes(original)
    digest = hashlib.sha256(os.path.normcase(str(target)).encode("utf-8")).hexdigest()
    stem = f".sb-commit-{digest}"
    journal = tmp_path / f"{stem}.json"
    capture = tmp_path / f"{stem}.capture"
    marker = tmp_path / f"{stem}.committed"
    external = tmp_path / "outside-bytes"
    external.write_bytes(b"must remain unchanged")
    if sidecar == "journal":
        link = journal
        _create_file_symlink(link, external)
    else:
        intent = {"version": 1, "target": str(target), "capture": str(capture),
                  "temp": f".{target.name}.sb-commit-fake.tmp", "before": hashlib.sha256(original).hexdigest(),
                  "after": hashlib.sha256(b"replacement").hexdigest(), "phase": "prepared"}
        journal.write_text(json.dumps(intent), encoding="utf-8")
        if sidecar == "capture":
            _create_file_symlink(capture, external)
        else:
            _create_file_symlink(marker, external)
    result = _run({"path": str(target), "operation": "recover"})
    assert result.returncode != 0
    response = json.loads(result.stdout)
    assert response["code"] == response["state"] == "recovery-conflict"
    assert response["detail"] == "reparse-sidecar"
    assert external.read_bytes() == b"must remain unchanged"
    assert target.read_bytes() == original
    assert link.is_symlink() if sidecar == "journal" else (capture if sidecar == "capture" else marker).is_symlink()


@pytest.mark.skipif(os.name != "nt", reason="conditional commit intentionally fails closed off Windows")
def test_editor_create_during_capture_gap_is_preserved_with_recovery_copy(tmp_path: Path) -> None:
    target = tmp_path / "config.jsonc"
    original = b'{"model":"before"}'
    target.write_bytes(original)
    child, _, release = _barrier_child(target, tmp_path, "captured")
    # The capture owns the prior bytes; a competing CREATE_NEW owns the pathname.
    user = b'{"model":"concurrent-user"}'
    target.write_bytes(user)
    release.write_text("continue", encoding="utf-8")
    stdout, _ = child.communicate(timeout=10)
    assert child.returncode != 0
    outcome = json.loads(stdout)
    assert outcome["state"] == "conditional-commit-conflict"
    assert target.read_bytes() == user
    captures = list(tmp_path.glob(".sb-commit-*.capture"))
    assert captures, f"capture absent; directory contents: {list(tmp_path.iterdir())}; result: {stdout}"
    assert captures[0].read_bytes() == original
    recovery = _run({"path": str(target), "operation": "recover"})
    assert recovery.returncode != 0
    diagnostic = json.loads(recovery.stdout)
    assert diagnostic["state"] == diagnostic["code"] == "recovery-conflict"
    assert diagnostic["recovery_path"] == str(captures[0])
    assert target.read_bytes() == user
    assert captures[0].read_bytes() == original
