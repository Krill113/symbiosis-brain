"""Tests for private, process-safe Codex session state."""
from __future__ import annotations

import csv
import ctypes
import io
import json
import math
import os
import re
import stat
import subprocess
import sys
import time
from ctypes import wintypes
from pathlib import Path

import pytest

from symbiosis_brain.adapters.codex import session_state as state_module
from symbiosis_brain.adapters.codex.session_state import (
    SessionState,
    delete_state,
    load_state,
    lock_path,
    save_state,
    session_key,
    session_lock,
    state_path,
)

_HASH = re.compile(r"^[0-9a-f]{64}$")
_SID = re.compile(r"^S-\d+(?:-\d+)+$")
_PRIVACY_ERROR = "persistent Codex state privacy could not be guaranteed"


def _set_codex_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, Path]:
    codex_home = tmp_path / "codex-home"
    temp_dir = tmp_path / "temp"
    temp_dir.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    monkeypatch.setenv("TEMP", str(temp_dir))
    monkeypatch.setenv("TMPDIR", str(temp_dir))
    return codex_home, temp_dir


def _windows_system_directory() -> Path:
    buffer = (wintypes.WCHAR * 32768)()
    length = ctypes.windll.kernel32.GetSystemDirectoryW(buffer, len(buffer))
    assert 0 < length < len(buffer)
    return Path(buffer.value)


def _windows_current_sid() -> str:
    result = subprocess.run(
        [
            _windows_system_directory() / "whoami.exe",
            "/user",
            "/fo",
            "csv",
            "/nh",
        ],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    rows = list(csv.reader(io.StringIO(result.stdout)))
    assert len(rows) == 1 and len(rows[0]) == 2
    sid = rows[0][1]
    assert _SID.fullmatch(sid)
    return sid


def _windows_access_sddl(path: Path) -> str:
    powershell = (
        _windows_system_directory()
        / "WindowsPowerShell"
        / "v1.0"
        / "powershell.exe"
    )
    script = (
        "& { param([string]$p) $ErrorActionPreference='Stop'; "
        "$acl=[System.IO.Directory]::GetAccessControl($p); "
        "[Console]::Out.Write($acl.GetSecurityDescriptorSddlForm("
        "[System.Security.AccessControl.AccessControlSections]::Access)) }"
    )
    result = subprocess.run(
        [
            powershell,
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            script,
            str(path),
        ],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return result.stdout.strip()


def _assert_expected_acl(path: Path, sid: str) -> None:
    powershell = (
        _windows_system_directory()
        / "WindowsPowerShell"
        / "v1.0"
        / "powershell.exe"
    )
    script = (
        "& { param([string]$p) $ErrorActionPreference='Stop'; "
        "$acl=[System.IO.Directory]::GetAccessControl($p); "
        "$rules=@($acl.GetAccessRules($true,$true,"
        "[System.Security.Principal.SecurityIdentifier]) | ForEach-Object { "
        "[pscustomobject]@{sid=$_.IdentityReference.Value; "
        "type=$_.AccessControlType.ToString(); rights=[int]$_.FileSystemRights; "
        "inheritance=[int]$_.InheritanceFlags; propagation=[int]$_.PropagationFlags; "
        "inherited=$_.IsInherited} }); "
        "[pscustomobject]@{protected=$acl.AreAccessRulesProtected; rules=$rules} "
        "| ConvertTo-Json -Compress -Depth 4 }"
    )
    result = subprocess.run(
        [powershell, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script, str(path)],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    snapshot = json.loads(result.stdout)
    rules = snapshot["rules"]
    if isinstance(rules, dict):
        rules = [rules]
    assert snapshot["protected"] is True
    assert rules == [{
        "sid": sid,
        "type": "Allow",
        "rights": 0x001F01FF,
        "inheritance": 3,  # ObjectInherit | ContainerInherit
        "propagation": 0,  # None
        "inherited": False,
    }]


def _windows_sddl_trustee_sids(sddl: str) -> list[str]:
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    descriptor = ctypes.c_void_p()
    convert_descriptor = advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW
    convert_descriptor.argtypes = (
        wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(wintypes.DWORD),
    )
    convert_descriptor.restype = wintypes.BOOL
    get_dacl = advapi32.GetSecurityDescriptorDacl
    get_dacl.argtypes = (
        ctypes.c_void_p, ctypes.POINTER(wintypes.BOOL),
        ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wintypes.BOOL),
    )
    get_dacl.restype = wintypes.BOOL
    get_ace = advapi32.GetAce
    get_ace.argtypes = (ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p))
    get_ace.restype = wintypes.BOOL
    get_acl_information = advapi32.GetAclInformation
    get_acl_information.argtypes = (ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD)
    get_acl_information.restype = wintypes.BOOL
    convert_sid = advapi32.ConvertSidToStringSidW
    convert_sid.argtypes = (ctypes.c_void_p, ctypes.POINTER(wintypes.LPWSTR))
    convert_sid.restype = wintypes.BOOL
    local_free = kernel32.LocalFree
    local_free.argtypes = (ctypes.c_void_p,)
    local_free.restype = ctypes.c_void_p
    try:
        assert convert_descriptor(sddl, 1, ctypes.byref(descriptor), None)
        present, dacl, defaulted = wintypes.BOOL(), ctypes.c_void_p(), wintypes.BOOL()
        assert get_dacl(descriptor, ctypes.byref(present), ctypes.byref(dacl), ctypes.byref(defaulted))
        assert present.value and dacl.value
        class _AclSizeInformation(ctypes.Structure):
            _fields_ = [("ace_count", wintypes.DWORD), ("bytes_used", wintypes.DWORD), ("bytes_free", wintypes.DWORD)]

        information = _AclSizeInformation()
        assert get_acl_information(dacl, ctypes.byref(information), ctypes.sizeof(information), 2)
        trustees = []
        for index in range(information.ace_count):
            ace = ctypes.c_void_p()
            assert get_ace(dacl, index, ctypes.byref(ace))
            trustee_sid = ctypes.c_void_p(ace.value + 8)
            sid_text = wintypes.LPWSTR()
            assert convert_sid(trustee_sid, ctypes.byref(sid_text))
            try:
                trustees.append(sid_text.value)
            finally:
                local_free(sid_text)
        return trustees
    finally:
        if descriptor:
            local_free(descriptor)


def _grant_windows_everyone_full_control(path: Path) -> None:
    result = subprocess.run(
        [
            _windows_system_directory() / "icacls.exe",
            path,
            "/grant",
            "*S-1-1-0:(OI)(CI)F",
        ],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert result.returncode == 0, result.stderr


def _wait_for_subprocess_path(
    path: Path, process: subprocess.Popen[str], *, timeout_s: float = 15.0
) -> None:
    deadline = time.monotonic() + timeout_s
    while not path.exists():
        if process.poll() is not None:
            stdout, stderr = process.communicate()
            pytest.fail(
                f"subprocess exited before creating {path.name}: "
                f"{process.returncode=}, {stdout=}, {stderr=}"
            )
        if time.monotonic() >= deadline:
            process.kill()
            stdout, stderr = process.communicate()
            pytest.fail(
                f"subprocess did not create {path.name}: "
                f"{process.returncode=}, {stdout=}, {stderr=}"
            )
        time.sleep(0.02)


def test_session_keys_cannot_collide_after_sanitizing():
    assert session_key("a/b") != session_key("a_b")


def test_session_key_is_hash_only_and_does_not_reveal_session_id():
    key = session_key("private/thread/name")
    assert _HASH.fullmatch(key)
    assert "private" not in key
    assert "thread" not in key


def test_empty_session_id_is_rejected():
    with pytest.raises(ValueError):
        session_key("")


@pytest.mark.parametrize("session_id", [None, 3, b"id"])
def test_non_string_session_id_is_rejected(session_id: object):
    with pytest.raises((TypeError, ValueError)):
        session_key(session_id)  # type: ignore[arg-type]


def test_state_paths_live_only_under_private_codex_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    codex_home, temp_dir = _set_codex_home(tmp_path, monkeypatch)

    path = state_path("thread-1")

    assert path.parent == codex_home.resolve() / ".symbiosis-brain" / "state"
    assert path.parent != temp_dir
    assert path.name == f"{session_key('thread-1')}.json"
    assert _HASH.fullmatch(path.stem)
    assert lock_path("thread-1").name == f"{session_key('thread-1')}.lock"


def test_private_state_directory_has_user_only_mode_on_posix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    if os.name == "nt":
        pytest.skip("POSIX mode bits are not the Windows ACL contract")
    codex_home, _ = _set_codex_home(tmp_path, monkeypatch)

    directory = state_path("thread-1").parent

    assert directory == codex_home.resolve() / ".symbiosis-brain" / "state"
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700


@pytest.mark.skipif(os.name != "nt", reason="Windows ACL contract")
def test_windows_acl_uses_process_sid_not_poisoned_identity_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    _set_codex_home(tmp_path, monkeypatch)
    monkeypatch.setenv("USERNAME", "attacker")
    monkeypatch.setenv("USERDOMAIN", "POISON")
    sid = _windows_current_sid()

    directory = state_path("thread-1").parent

    _assert_expected_acl(directory, sid)


@pytest.mark.skipif(os.name != "nt", reason="Windows ACL contract")
def test_windows_acl_repairs_preexisting_everyone_access(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    codex_home, _ = _set_codex_home(tmp_path, monkeypatch)
    private_root = codex_home / ".symbiosis-brain"
    directory = private_root / "state"
    directory.mkdir(parents=True)
    _grant_windows_everyone_full_control(private_root)
    _grant_windows_everyone_full_control(directory)
    assert "S-1-1-0" in _windows_sddl_trustee_sids(_windows_access_sddl(directory))
    sid = _windows_current_sid()

    state_path("thread-1")

    _assert_expected_acl(private_root, sid)
    _assert_expected_acl(directory, sid)


@pytest.mark.skipif(os.name != "nt", reason="Windows ACL contract")
def test_windows_acl_sddl_alias_must_resolve_to_exact_token_sid():
    # Parse LA on this machine, then prove that alias is accepted only when its
    # resolved SID is exactly the supplied token SID. No admin rights required.
    alias_sddl = "D:PAI(A;OICI;FA;;;LA)"
    local_admin_sid = _windows_sddl_trustee_sids(alias_sddl)[0]
    sid_parts = local_admin_sid.split("-")
    assert sid_parts[-1] == "500"
    foreign_sid = "-".join(
        [*sid_parts[:-2], str((int(sid_parts[-2]) + 1) % (2**32)), "500"]
    )
    assert state_module._windows_sddl_matches_user(alias_sddl, local_admin_sid)
    assert foreign_sid.endswith("-500") and foreign_sid != local_admin_sid
    assert not state_module._windows_sddl_matches_user(alias_sddl, foreign_sid)


@pytest.mark.skipif(os.name != "nt", reason="Windows ACL contract")
@pytest.mark.parametrize(
    "sddl_template",
    [
        "D:PAI(D;OICI;FA;;;{sid})",  # Deny ACE
        "D:PAI(A;OICI;FR;;;{sid})",  # Not FullControl
        "D:PAI(A;OICIIO;FA;;;{sid})",  # Inherit-only
        "D:PAI(A;OICIID;FA;;;{sid})",  # Already inherited
        "D:PAI(A;OI;FA;;;{sid})",  # Missing container inheritance
        "D:PAI(A;OICI;FA;;;{sid})(A;OICI;FA;;;WD)",  # Extra trustee
        "D:PAI(A;OICI;FA;;;WD)",  # Wrong trustee alias
        "D:P",  # Empty DACL
        "D:NO_ACCESS_CONTROL",  # Null DACL
        "not an SDDL descriptor",  # Malformed descriptor
    ],
)
def test_windows_acl_semantic_verifier_rejects_invalid_dacls(sddl_template: str):
    sid = _windows_current_sid()
    assert state_module._windows_sddl_matches_user(
        f"D:PAI(A;OICI;FA;;;{sid})", sid
    )
    assert not state_module._windows_sddl_matches_user(
        sddl_template.format(sid=sid), sid
    )


@pytest.mark.skipif(os.name != "nt", reason="Windows ACL contract")
def test_windows_acl_wrapper_fast_path_resolves_alias_and_checks_both_directories(
    monkeypatch: pytest.MonkeyPatch,
):
    first, second = Path("private-root"), Path("private-state")
    alias_sddl = "D:PAI(A;OICI;FA;;;LA)"
    local_admin_sid = _windows_sddl_trustee_sids(alias_sddl)[0]
    parts = local_admin_sid.split("-")
    foreign_sid = "-".join([*parts[:-2], str((int(parts[-2]) + 1) % (2**32)), "500"])
    descriptors = {first: alias_sddl, second: alias_sddl}
    monkeypatch.setattr(state_module, "_current_windows_sid", lambda _path: local_admin_sid)
    monkeypatch.setattr(
        state_module, "_windows_access_sddl", lambda path: descriptors[path]
    )

    assert state_module._windows_acl_is_expected(first, second)
    descriptors[second] = f"D:PAI(A;OICI;FA;;;{foreign_sid})"
    assert not state_module._windows_acl_is_expected(first, second)


@pytest.mark.skipif(os.name != "nt", reason="Windows ACL contract")
def test_windows_acl_apply_wrapper_resolves_alias_and_rejects_foreign_second_dacl(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    first, second = tmp_path / "root", tmp_path / "state"
    alias_sddl = "D:PAI(A;OICI;FA;;;LA)"
    local_admin_sid = _windows_sddl_trustee_sids(alias_sddl)[0]
    parts = local_admin_sid.split("-")
    foreign_sid = "-".join([*parts[:-2], str((int(parts[-2]) + 1) % (2**32)), "500"])
    monkeypatch.setattr(state_module, "_windows_acl_is_expected", lambda *_paths: False)
    monkeypatch.setattr(state_module, "_windows_system_directory", lambda: tmp_path)
    monkeypatch.setattr(state_module, "_current_windows_sid", lambda _path: local_admin_sid)

    def completed(stdout: str):
        monkeypatch.setattr(
            state_module.subprocess,
            "run",
            lambda args, **_kwargs: subprocess.CompletedProcess(args, 0, stdout, ""),
        )

    completed(f"{alias_sddl}\n{alias_sddl}\n")
    state_module._apply_and_verify_windows_acls(first, second)

    completed(f"{alias_sddl}\nD:PAI(A;OICI;FA;;;{foreign_sid})\n")
    with pytest.raises(
        state_module.PersistentStatePrivacyError, match=f"^{_PRIVACY_ERROR}$"
    ):
        state_module._apply_and_verify_windows_acls(first, second)


@pytest.mark.skipif(os.name != "nt", reason="Windows ACL contract")
def test_windows_acl_failure_raises_fixed_privacy_exception(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    _set_codex_home(tmp_path, monkeypatch)

    def fail_run(*args, **kwargs):
        return subprocess.CompletedProcess(args[0], 1, "", "private diagnostic")

    monkeypatch.setattr(state_module.subprocess, "run", fail_run)

    with pytest.raises(
        state_module.PersistentStatePrivacyError, match=f"^{_PRIVACY_ERROR}$"
    ):
        state_path("thread-1")


@pytest.mark.skipif(os.name != "nt", reason="Windows ACL contract")
def test_windows_acl_fast_path_avoids_shell_processes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    _set_codex_home(tmp_path, monkeypatch)
    state_module._VERIFIED_PRIVATE_DIRS.clear()
    monkeypatch.setattr(
        state_module,
        "_windows_acl_is_expected",
        lambda first, second: True,
        raising=False,
    )

    def fail_process(*args, **kwargs):
        raise AssertionError("ACL fast path must not spawn a shell process")

    monkeypatch.setattr(state_module.subprocess, "run", fail_process)

    state_path("thread-1")


@pytest.mark.skipif(os.name != "nt", reason="Windows ACL contract")
def test_windows_state_acl_is_verified_once_per_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    codex_home, _ = _set_codex_home(tmp_path, monkeypatch)
    calls: list[tuple[Path, Path]] = []
    monkeypatch.setattr(
        state_module,
        "_apply_and_verify_windows_acls",
        lambda first, second: calls.append((first, second)),
    )
    state_module._VERIFIED_PRIVATE_DIRS.clear()

    state_path("first")
    state_path("second")

    private_root = codex_home / ".symbiosis-brain"
    assert calls == [
        (private_root, private_root / "state"),
    ]


def test_corrupt_state_resets_to_fresh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    _set_codex_home(tmp_path, monkeypatch)
    path = state_path("thread-1")
    path.write_text("{", encoding="utf-8")

    assert load_state("thread-1") == SessionState(
        session_key=session_key("thread-1")
    )


@pytest.mark.parametrize(
    "payload",
    [
        [],
        {"turn": True},
        {"turn": -1},
        {"last_save_turn": "1"},
        {"save_reminded_turn": 1.5},
        {"compaction_pending": 1},
        {"bootstrap_source": "raw/project/handoff.md"},
        {
            "turn": 1,
            "last_save_turn": 0,
            "save_reminded_turn": 0,
            "compaction_pending": False,
            "bootstrap_source": "a" * 64,
        },
    ],
)
def test_invalid_state_schema_resets_to_fresh(
    payload: object, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    _set_codex_home(tmp_path, monkeypatch)
    path = state_path("thread-1")
    path.write_text(json.dumps(payload), encoding="utf-8")

    assert load_state("thread-1") == SessionState(
        session_key=session_key("thread-1")
    )


def test_state_round_trip_persists_only_allowlisted_private_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    _set_codex_home(tmp_path, monkeypatch)
    state = SessionState(
        session_key=session_key("private-session-id"),
        turn=7,
        last_save_turn=3,
        save_reminded_turn=6,
        compaction_pending=True,
    )

    save_state(state)
    text = state_path("private-session-id").read_text(encoding="utf-8")
    payload = json.loads(text)
    loaded = load_state("private-session-id")

    assert set(payload) == {
        "turn",
        "last_save_turn",
        "save_reminded_turn",
        "compaction_pending",
    }
    assert "private-session-id" not in text
    assert not hasattr(state, "bootstrap_source")
    assert loaded == SessionState(
        session_key=session_key("private-session-id"),
        turn=7,
        last_save_turn=3,
        save_reminded_turn=6,
        compaction_pending=True,
    )


def test_save_rejects_non_hash_session_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    _set_codex_home(tmp_path, monkeypatch)
    with pytest.raises(ValueError):
        save_state(SessionState(session_key="../../raw-session"))


def test_state_file_is_user_only_on_posix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    if os.name == "nt":
        pytest.skip("POSIX mode bits are not the Windows ACL contract")
    _set_codex_home(tmp_path, monkeypatch)
    state = SessionState(session_key=session_key("thread-1"))

    save_state(state)

    assert stat.S_IMODE(state_path("thread-1").stat().st_mode) == 0o600


def test_delete_state_preserves_an_active_lock_until_context_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    _set_codex_home(tmp_path, monkeypatch)
    lock = lock_path("thread-1")

    with session_lock("thread-1"):
        save_state(SessionState(session_key=session_key("thread-1"), turn=1))
        delete_state("thread-1")
        assert not state_path("thread-1").exists()
        assert lock.exists()

    assert lock.exists()
    assert lock.stat().st_size >= 1


def test_lock_guard_is_persistent_and_contains_at_least_one_byte(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    _set_codex_home(tmp_path, monkeypatch)
    lock = lock_path("thread-1")

    with session_lock("thread-1"):
        assert lock.exists()
        assert lock.stat().st_size >= 1

    assert lock.exists()
    assert lock.stat().st_size >= 1


def test_async_exception_after_successful_acquire_releases_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    codex_home, _ = _set_codex_home(tmp_path, monkeypatch)
    original_try_lock_file = state_module._try_lock_file
    acquired_fds: list[int] = []
    armed = False
    caller_line_events = 0

    class InjectedAsyncError(BaseException):
        pass

    def try_lock_file_and_arm(fd: int) -> bool:
        nonlocal armed
        acquired = original_try_lock_file(fd)
        if acquired:
            acquired_fds.append(fd)
            armed = True
        return acquired

    session_lock_code = session_lock.__wrapped__.__code__

    def inject_after_acquire(frame, event, arg):
        nonlocal armed, caller_line_events
        if armed and frame.f_code is session_lock_code and event == "line":
            caller_line_events += 1
            if caller_line_events == 2:
                armed = False
                raise InjectedAsyncError
        return inject_after_acquire

    monkeypatch.setattr(state_module, "_try_lock_file", try_lock_file_and_arm)
    sys.settrace(inject_after_acquire)
    try:
        with pytest.raises(InjectedAsyncError):
            with session_lock("thread-async", timeout_s=1.0):
                pytest.fail("async exception was not injected before context entry")
    finally:
        sys.settrace(None)

    assert len(acquired_fds) == 1
    leaked_fd = acquired_fds[0]
    env = os.environ.copy()
    env["CODEX_HOME"] = str(codex_home)
    try:
        successor = subprocess.run(
            [
                sys.executable,
                "-c",
                """
from symbiosis_brain.adapters.codex.session_state import session_lock
try:
    with session_lock("thread-async", timeout_s=0.2):
        print("acquired")
except TimeoutError:
    print("timeout")
""",
            ],
            cwd=Path(__file__).resolve().parents[2],
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=15,
        )
        assert (successor.returncode, successor.stdout, successor.stderr) == (
            0,
            "acquired\n",
            "",
        )
    finally:
        try:
            state_module._unlock_file(leaked_fd)
        except OSError:
            pass
        try:
            os.close(leaked_fd)
        except OSError:
            pass


def test_process_crash_releases_lock_without_deleting_guard_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    codex_home, _ = _set_codex_home(tmp_path, monkeypatch)
    ready = tmp_path / "crash-ready"
    guard = lock_path("thread-crash")
    guard.write_text("{", encoding="utf-8")
    script = """
import os
import sys
from pathlib import Path
from symbiosis_brain.adapters.codex.session_state import session_lock
with session_lock("thread-crash", timeout_s=5.0):
    Path(sys.argv[1]).write_text("ready", encoding="utf-8")
    os._exit(23)
"""
    env = os.environ.copy()
    env["CODEX_HOME"] = str(codex_home)
    crashed = subprocess.run(
        [sys.executable, "-c", script, str(ready)],
        cwd=Path(__file__).resolve().parents[2],
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=15,
    )
    assert crashed.returncode == 23
    assert ready.read_text(encoding="utf-8") == "ready"
    assert guard.exists()
    assert guard.read_text(encoding="utf-8") == "{"

    successor = subprocess.run(
        [
            sys.executable,
            "-c",
            """
from symbiosis_brain.adapters.codex.session_state import session_lock
with session_lock("thread-crash", timeout_s=1.0):
    print("acquired")
""",
        ],
        cwd=Path(__file__).resolve().parents[2],
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=15,
    )

    assert (successor.returncode, successor.stdout, successor.stderr) == (
        0,
        "acquired\n",
        "",
    )


def test_waiter_cannot_enter_while_owner_alive_when_guard_is_old_and_malformed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    codex_home, _ = _set_codex_home(tmp_path, monkeypatch)
    ready = tmp_path / "owner-ready"
    release = tmp_path / "owner-release"
    guard = lock_path("thread-live")
    guard.write_text("{", encoding="utf-8")
    old = time.time() - 25 * 60 * 60
    os.utime(guard, (old, old))
    owner_script = """
import sys
import time
from pathlib import Path
from symbiosis_brain.adapters.codex.session_state import session_lock
ready = Path(sys.argv[1])
release = Path(sys.argv[2])
with session_lock("thread-live", timeout_s=5.0):
    ready.write_text("ready", encoding="utf-8")
    while not release.exists():
        time.sleep(0.02)
"""
    env = os.environ.copy()
    env["CODEX_HOME"] = str(codex_home)
    owner = subprocess.Popen(
        [sys.executable, "-c", owner_script, str(ready), str(release)],
        cwd=Path(__file__).resolve().parents[2],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    )
    try:
        _wait_for_subprocess_path(ready, owner)
        assert guard.stat().st_mtime <= old + 1
        contender = subprocess.run(
            [
                sys.executable,
                "-c",
                """
from symbiosis_brain.adapters.codex.session_state import session_lock
try:
    with session_lock("thread-live", timeout_s=0.2):
        print("entered")
except TimeoutError:
    print("timeout")
""",
            ],
            cwd=Path(__file__).resolve().parents[2],
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=15,
        )
        assert (contender.returncode, contender.stdout, contender.stderr) == (
            0,
            "timeout\n",
            "",
        )
        assert owner.poll() is None
    finally:
        release.write_text("release", encoding="utf-8")
        try:
            owner_stdout, owner_stderr = owner.communicate(timeout=15)
        except subprocess.TimeoutExpired:
            owner.kill()
            owner_stdout, owner_stderr = owner.communicate()
    assert (owner.returncode, owner_stdout, owner_stderr) == (0, "", "")
    assert guard.read_text(encoding="utf-8") == "{"


@pytest.mark.parametrize(
    "timeout_s",
    [math.nan, math.inf, -math.inf, -0.01, True, "1"],
)
def test_lock_timeout_must_be_a_non_negative_finite_number(
    timeout_s: object, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    _set_codex_home(tmp_path, monkeypatch)

    with pytest.raises(ValueError):
        with session_lock("thread-1", timeout_s=timeout_s):  # type: ignore[arg-type]
            pass


def test_old_state_files_are_garbage_collected_without_unlinking_guard_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    _set_codex_home(tmp_path, monkeypatch)
    directory = state_path("seed").parent
    old_state = directory / ("a" * 64 + ".json")
    old_lock = directory / ("b" * 64 + ".lock")
    unrelated = directory / "keep.txt"
    fresh_state = directory / ("c" * 64 + ".json")
    for path in (old_state, unrelated, fresh_state):
        path.write_text("x", encoding="utf-8")
    old_lock.write_text("{", encoding="utf-8")
    old = time.time() - 25 * 60 * 60
    os.utime(old_state, (old, old))
    os.utime(old_lock, (old, old))
    os.utime(unrelated, (old, old))

    state_path("trigger")

    assert not old_state.exists()
    assert old_lock.exists()
    assert unrelated.exists()
    assert fresh_state.exists()


def test_garbage_collection_never_deletes_an_old_guard_it_cannot_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    codex_home, _ = _set_codex_home(tmp_path, monkeypatch)
    ready = tmp_path / "gc-owner-ready"
    release = tmp_path / "gc-owner-release"
    guard = lock_path("thread-gc")
    guard.write_text("{", encoding="utf-8")
    old = time.time() - 25 * 60 * 60
    os.utime(guard, (old, old))
    owner_script = """
import sys
import time
from pathlib import Path
from symbiosis_brain.adapters.codex.session_state import session_lock
ready = Path(sys.argv[1])
release = Path(sys.argv[2])
with session_lock("thread-gc", timeout_s=5.0):
    ready.write_text("ready", encoding="utf-8")
    while not release.exists():
        time.sleep(0.02)
"""
    env = os.environ.copy()
    env["CODEX_HOME"] = str(codex_home)
    owner = subprocess.Popen(
        [sys.executable, "-c", owner_script, str(ready), str(release)],
        cwd=Path(__file__).resolve().parents[2],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    )
    try:
        _wait_for_subprocess_path(ready, owner)

        state_path("trigger")

        assert guard.exists()
        assert owner.poll() is None
    finally:
        release.write_text("release", encoding="utf-8")
        try:
            owner_stdout, owner_stderr = owner.communicate(timeout=15)
        except subprocess.TimeoutExpired:
            owner.kill()
            owner_stdout, owner_stderr = owner.communicate()
    assert (owner.returncode, owner_stdout, owner_stderr) == (0, "", "")
    assert guard.read_text(encoding="utf-8") == "{"


def test_symlink_component_is_rejected_when_supported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    if os.name == "nt":
        pytest.skip("Windows reparse handling is covered by the same runtime check")
    real_home = tmp_path / "real-home"
    real_home.mkdir()
    linked_home = tmp_path / "linked-home"
    linked_home.symlink_to(real_home, target_is_directory=True)
    monkeypatch.setenv("CODEX_HOME", str(linked_home))

    with pytest.raises(OSError):
        state_path("thread-1")


def test_two_processes_update_one_session_under_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    codex_home, _ = _set_codex_home(tmp_path, monkeypatch)
    script = """
import time
from symbiosis_brain.adapters.codex.session_state import load_state, save_state, session_lock
with session_lock('thread-1', timeout_s=5.0):
    state = load_state('thread-1')
    state.turn += 1
    time.sleep(0.05)
    save_state(state)
"""
    env = os.environ.copy()
    env["CODEX_HOME"] = str(codex_home)
    processes = [
        subprocess.Popen(
            [sys.executable, "-c", script],
            cwd=Path(__file__).resolve().parents[2],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
        )
        for _ in range(2)
    ]
    results = [process.communicate(timeout=10) for process in processes]

    assert [(process.returncode, stdout, stderr) for process, (stdout, stderr) in zip(processes, results)] == [
        (0, "", ""),
        (0, "", ""),
    ]
    assert load_state("thread-1").turn == 2
