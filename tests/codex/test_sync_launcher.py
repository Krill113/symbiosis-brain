from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

import symbiosis_brain.adapters.codex.sync_launcher as sync_launcher


def _config(bash: Path | None):
    return SimpleNamespace(sync_bash_path=bash)


def test_packaged_sync_script_resolves_wheel_layout(monkeypatch, tmp_path: Path):
    script = tmp_path / "hooks" / "brain-sync.sh"
    script.parent.mkdir()
    script.write_text("#!/bin/bash\n", encoding="utf-8")
    monkeypatch.setattr(
        sync_launcher,
        "packaged_dir",
        lambda _pkg_file, name: tmp_path / name,
    )

    assert sync_launcher.packaged_sync_script() == script


def test_packaged_sync_script_resolves_editable_layout(monkeypatch, tmp_path: Path):
    root = tmp_path / "repo"
    script = root / "hooks" / "brain-sync.sh"
    script.parent.mkdir(parents=True)
    script.write_text("#!/bin/bash\n", encoding="utf-8")
    monkeypatch.setattr(
        sync_launcher,
        "packaged_dir",
        lambda _pkg_file, name: root / name,
    )

    assert sync_launcher.packaged_sync_script() == script


@pytest.mark.parametrize("candidate", [
    Path("C:/Program Files/Git/bin/bash.exe"),
    Path("C:/Program Files/Git/usr/bin/bash.exe"),
])
def test_find_bash_uses_the_pinned_windows_path(
    monkeypatch, candidate: Path, tmp_path: Path
):
    monkeypatch.setattr(sync_launcher, "load_codex_config", lambda: _config(candidate))
    monkeypatch.setattr(sync_launcher, "_is_windows", lambda: True)
    monkeypatch.setattr(Path, "exists", lambda self: self == candidate)
    monkeypatch.setattr(
        sync_launcher,
        "verify_bash_prerequisites",
        lambda path: path == candidate,
    )

    assert sync_launcher.find_bash() == candidate


def test_find_bash_never_uses_environment_or_path_search(monkeypatch, tmp_path: Path):
    pinned = tmp_path / "pinned" / "bash.exe"
    monkeypatch.setattr(sync_launcher, "load_codex_config", lambda: _config(pinned))
    monkeypatch.setenv("GIT_BASH", str(tmp_path / "poison.exe"))
    monkeypatch.setenv("PATH", str(tmp_path / "poison-path"))
    monkeypatch.setattr(Path, "exists", lambda self: self == pinned)
    monkeypatch.setattr(
        sync_launcher,
        "verify_bash_prerequisites",
        lambda path: True,
    )

    assert sync_launcher.find_bash() == pinned


def test_find_bash_rejects_missing_prerequisites(monkeypatch, tmp_path: Path):
    pinned = tmp_path / "bash"
    monkeypatch.setattr(sync_launcher, "load_codex_config", lambda: _config(pinned))
    monkeypatch.setattr(Path, "exists", lambda self: self == pinned)
    monkeypatch.setattr(sync_launcher, "verify_bash_prerequisites", lambda _path: False)

    assert sync_launcher.find_bash() is None


def test_auto_sync_only_checks_pinned_bash_existence(
    monkeypatch, tmp_path: Path
):
    pinned = tmp_path / "bash"
    script = tmp_path / "brain-sync.sh"
    script.write_text("#!/bin/sh\n", encoding="utf-8")
    calls: list[Path] = []
    monkeypatch.setattr(sync_launcher, "load_codex_config", lambda: _config(pinned))
    monkeypatch.setattr(Path, "exists", lambda self: self in {pinned, script})
    monkeypatch.setattr(
        sync_launcher,
        "verify_bash_prerequisites",
        lambda path: calls.append(path) or False,
    )
    monkeypatch.setattr(
        sync_launcher,
        "packaged_sync_script",
        lambda: script,
    )
    monkeypatch.setattr(
        sync_launcher.subprocess,
        "Popen",
        lambda *args, **kwargs: object(),
    )
    assert sync_launcher.launch_auto_sync(tmp_path / "vault") is True
    assert calls == []


@pytest.mark.parametrize("failure", ["relative", "missing", "prerequisites"])
def test_find_bash_does_not_log_pinned_path_for_unusable_bash(
    failure: str, monkeypatch, tmp_path: Path
):
    sentinel = "codex-private-bash-sentinel"
    pinned = (
        Path("relative") / sentinel
        if failure == "relative"
        else tmp_path / sentinel
    )
    debug = tmp_path / "debug.log"
    monkeypatch.setenv("SYMBIOSIS_BRAIN_DEBUG_LOG", str(debug))
    monkeypatch.setattr(sync_launcher, "load_codex_config", lambda: _config(pinned))
    if failure == "missing":
        monkeypatch.setattr(Path, "exists", lambda self: False)
    elif failure == "prerequisites":
        monkeypatch.setattr(Path, "exists", lambda self: self == pinned)
        monkeypatch.setattr(
            sync_launcher, "verify_bash_prerequisites", lambda _path: False
        )

    assert sync_launcher.find_bash() is None
    assert sentinel not in debug.read_text(encoding="utf-8")


def test_verify_bash_prerequisites_checks_required_utilities(monkeypatch, tmp_path: Path):
    calls: list[tuple] = []
    bash = tmp_path / "bash"

    def fake_run(*args, **kwargs):
        calls.append((args, kwargs))
        return subprocess.CompletedProcess(args[0], 0, "", "")

    monkeypatch.setattr(sync_launcher.subprocess, "run", fake_run)

    assert sync_launcher.verify_bash_prerequisites(bash) is True
    assert calls
    command = calls[0][0][0]
    assert command[0] == bash
    assert command[1:] == ["-lc", "command -v git timeout find date"]


def test_verify_bash_prerequisites_rejects_failed_probe(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(
        sync_launcher.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 1, "", "missing timeout"),
    )

    assert sync_launcher.verify_bash_prerequisites(tmp_path / "bash") is False


def test_run_manual_sync_uses_packaged_script_and_vault_env(
    monkeypatch, tmp_path: Path
):
    bash = tmp_path / "bash"
    script = tmp_path / "brain-sync.sh"
    script.write_text("#!/bin/sh\n", encoding="utf-8")
    vault = tmp_path / "vault"
    calls: list[tuple] = []

    monkeypatch.setattr(sync_launcher, "find_bash", lambda: bash)
    monkeypatch.setattr(sync_launcher, "packaged_sync_script", lambda: script)

    def fake_run(*args, **kwargs):
        calls.append((args, kwargs))
        return subprocess.CompletedProcess(args[0], 7)

    monkeypatch.setattr(sync_launcher.subprocess, "run", fake_run)
    monkeypatch.setenv("SYMBIOSIS_BRAIN_VAULT", "poison")

    assert sync_launcher.run_manual_sync(vault) == 7
    args, kwargs = calls[0]
    assert args[0] == [bash, script, "manual"]
    assert kwargs["env"]["SYMBIOSIS_BRAIN_VAULT"] == str(vault)
    assert kwargs["encoding"] == "utf-8"
    assert kwargs["errors"] == "replace"
    assert os.environ["SYMBIOSIS_BRAIN_VAULT"] == "poison"


def test_run_manual_sync_reports_missing_bash(monkeypatch, tmp_path: Path, capsys):
    monkeypatch.setattr(sync_launcher, "find_bash", lambda: None)

    assert sync_launcher.run_manual_sync(tmp_path / "vault") != 0
    assert "Git Bash" in capsys.readouterr().err


def test_run_manual_sync_does_not_echo_oserror_path(
    monkeypatch, tmp_path: Path, capsys
):
    sentinel = tmp_path / "codex-private-oserror-sentinel" / "bash.exe"
    script = tmp_path / "brain-sync.sh"
    monkeypatch.setattr(sync_launcher, "find_bash", lambda: sentinel)
    monkeypatch.setattr(sync_launcher, "packaged_sync_script", lambda: script)

    def fail_run(*args, **kwargs):
        raise OSError(f"cannot execute {sentinel}")

    monkeypatch.setattr(sync_launcher.subprocess, "run", fail_run)

    assert sync_launcher.run_manual_sync(tmp_path / "vault") != 0
    assert str(sentinel) not in capsys.readouterr().err


def test_launch_auto_sync_detaches_all_standard_handles_and_returns_fast(
    monkeypatch, tmp_path: Path
):
    bash = tmp_path / "bash"
    script = tmp_path / "brain-sync.sh"
    vault = tmp_path / "vault"
    calls: list[tuple] = []

    monkeypatch.setattr(sync_launcher, "_is_windows", lambda: False)
    monkeypatch.setattr(sync_launcher, "find_bash", lambda: bash)
    monkeypatch.setattr(sync_launcher, "packaged_sync_script", lambda: script)

    def fake_popen(*args, **kwargs):
        calls.append((args, kwargs))
        return object()

    monkeypatch.setattr(sync_launcher.subprocess, "Popen", fake_popen)
    started = time.monotonic()
    assert sync_launcher.launch_auto_sync(vault) is True
    assert time.monotonic() - started < 2

    args, kwargs = calls[0]
    assert args[0] == [bash, script, "auto"]
    assert kwargs["stdin"] is subprocess.DEVNULL
    assert kwargs["stdout"] is subprocess.DEVNULL
    assert kwargs["stderr"] is subprocess.DEVNULL
    assert kwargs["close_fds"] is True
    assert kwargs["start_new_session"] is True
    assert kwargs["env"]["SYMBIOSIS_BRAIN_VAULT"] == str(vault)


def test_launch_auto_sync_supervised_posix_keeps_worker_process_group(
    monkeypatch,
    tmp_path: Path,
):
    bash = tmp_path / "bash"
    script = tmp_path / "brain-sync.sh"
    vault = tmp_path / "vault"
    calls: list[dict[str, object]] = []

    monkeypatch.setattr(sync_launcher, "_is_windows", lambda: False)
    monkeypatch.setattr(sync_launcher, "find_bash", lambda: bash)
    monkeypatch.setattr(sync_launcher, "packaged_sync_script", lambda: script)
    monkeypatch.setattr(
        sync_launcher.subprocess,
        "Popen",
        lambda *args, **kwargs: calls.append(kwargs) or object(),
    )

    assert sync_launcher.launch_auto_sync(vault, detach=False) is True

    assert "start_new_session" not in calls[0]
    assert calls[0]["stdin"] is subprocess.DEVNULL
    assert calls[0]["stdout"] is subprocess.DEVNULL
    assert calls[0]["stderr"] is subprocess.DEVNULL
    assert calls[0]["close_fds"] is True


def test_launch_auto_sync_uses_windows_detached_flags(monkeypatch, tmp_path: Path):
    bash = tmp_path / "bash.exe"
    script = tmp_path / "brain-sync.sh"
    script.write_text("#!/bin/sh\n", encoding="utf-8")
    calls: list[dict] = []

    monkeypatch.setattr(sync_launcher, "_is_windows", lambda: True)
    monkeypatch.setattr(sync_launcher, "find_bash", lambda: bash)
    monkeypatch.setattr(sync_launcher, "packaged_sync_script", lambda: script)
    monkeypatch.setattr(
        sync_launcher.subprocess,
        "CREATE_NO_WINDOW",
        0x08000000,
        raising=False,
    )
    monkeypatch.setattr(
        sync_launcher.subprocess,
        "DETACHED_PROCESS",
        0x00000008,
        raising=False,
    )
    monkeypatch.setattr(
        sync_launcher.subprocess,
        "Popen",
        lambda *args, **kwargs: calls.append(kwargs) or object(),
    )

    assert sync_launcher.launch_auto_sync(tmp_path / "vault") is True
    assert calls[0]["creationflags"] == 0x08000008
    assert "start_new_session" not in calls[0]


def test_launch_auto_sync_supervised_windows_uses_no_window_without_detach(
    monkeypatch,
    tmp_path: Path,
):
    bash = tmp_path / "bash.exe"
    script = tmp_path / "brain-sync.sh"
    script.write_text("#!/bin/sh\n", encoding="utf-8")
    calls: list[dict[str, object]] = []

    monkeypatch.setattr(sync_launcher, "_is_windows", lambda: True)
    monkeypatch.setattr(sync_launcher, "find_bash", lambda: bash)
    monkeypatch.setattr(sync_launcher, "packaged_sync_script", lambda: script)
    monkeypatch.setattr(
        sync_launcher.subprocess,
        "CREATE_NO_WINDOW",
        0x08000000,
        raising=False,
    )
    monkeypatch.setattr(
        sync_launcher.subprocess,
        "DETACHED_PROCESS",
        0x00000008,
        raising=False,
    )
    monkeypatch.setattr(
        sync_launcher.subprocess,
        "Popen",
        lambda *args, **kwargs: calls.append(kwargs) or object(),
    )

    assert sync_launcher.launch_auto_sync(
        tmp_path / "vault",
        detach=False,
    ) is True

    assert calls[0]["creationflags"] == 0x08000000
    assert "start_new_session" not in calls[0]


def test_launch_auto_sync_missing_bash_logs_debug(monkeypatch, tmp_path: Path):
    debug = tmp_path / "debug.log"
    monkeypatch.setenv("SYMBIOSIS_BRAIN_DEBUG_LOG", str(debug))
    monkeypatch.setattr(sync_launcher, "find_bash", lambda: None)

    assert sync_launcher.launch_auto_sync(tmp_path / "vault") is False
    assert "Git Bash" in debug.read_text(encoding="utf-8")


def test_run_cli_manual_prefers_explicit_vault_and_returns_sync_error(
    monkeypatch, tmp_path: Path
):
    explicit = tmp_path / "explicit"
    env_vault = tmp_path / "env"
    calls: list[Path] = []
    monkeypatch.setenv("SYMBIOSIS_BRAIN_VAULT", str(env_vault))
    monkeypatch.setattr(
        sync_launcher,
        "run_manual_sync",
        lambda vault: calls.append(vault) or 9,
    )

    assert sync_launcher.run_cli(["manual", "--vault", str(explicit)]) == 9
    assert calls == [explicit.resolve()]


def test_run_cli_manual_resolves_non_default_configured_vault(
    monkeypatch, tmp_path: Path
):
    codex_home = tmp_path / "codex"
    vault = tmp_path / "configured-vault"
    codex_home.mkdir()
    (codex_home / "symbiosis-brain.json").write_text(
        json.dumps({"vault_path": str(vault)}), encoding="utf-8"
    )
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    monkeypatch.delenv("SYMBIOSIS_BRAIN_VAULT", raising=False)
    calls: list[Path] = []
    monkeypatch.setattr(
        sync_launcher,
        "run_manual_sync",
        lambda selected: calls.append(selected) or 0,
    )

    assert sync_launcher.run_cli(["manual"]) == 0
    assert calls == [vault.resolve()]


def test_run_cli_auto_ignores_poisoned_vault_environment(
    monkeypatch, tmp_path: Path
):
    codex_home = tmp_path / "codex"
    pinned = tmp_path / "pinned-vault"
    poisoned = tmp_path / "poisoned-vault"
    codex_home.mkdir()
    (codex_home / "symbiosis-brain.json").write_text(
        json.dumps({"vault_path": str(pinned)}), encoding="utf-8"
    )
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    monkeypatch.setenv("SYMBIOSIS_BRAIN_VAULT", str(poisoned))
    calls: list[Path] = []
    monkeypatch.setattr(
        sync_launcher,
        "launch_auto_sync",
        lambda selected: calls.append(selected) or True,
    )

    assert sync_launcher.run_cli(["auto"]) == 0
    assert calls == [pinned.resolve()]


def test_run_cli_rejects_unknown_mode(capsys):
    assert sync_launcher.run_cli(["later"]) != 0
    assert "manual" in capsys.readouterr().err


def test_both_console_paths_dispatch_packaged_sync(monkeypatch):
    import symbiosis_brain.__main__ as module_main
    import symbiosis_brain.install_cli as install_cli

    calls: list[list[str]] = []
    monkeypatch.setattr(
        sync_launcher,
        "run_cli",
        lambda argv: calls.append(argv) or 4,
    )

    monkeypatch.setattr(sys, "argv", ["python", "packaged-sync", "manual"])
    with pytest.raises(SystemExit) as exc:
        module_main.main()
    assert exc.value.code == 4

    monkeypatch.setattr(sys, "argv", ["symbiosis-brain", "packaged-sync", "manual"])
    with pytest.raises(SystemExit) as exc:
        install_cli.main()
    assert exc.value.code == 4
    assert calls == [["manual"], ["manual"]]
