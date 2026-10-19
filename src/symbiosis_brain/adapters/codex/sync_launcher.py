"""Packaged manual and detached auto-sync launcher for Codex."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from pathlib import PureWindowsPath
from typing import Mapping

from symbiosis_brain import pre_action_config
from symbiosis_brain.adapters.codex.config import (
    load_codex_config,
    resolve_codex_home,
)
from symbiosis_brain.install_lib import packaged_dir

_SYNC_SCRIPT = "brain-sync.sh"
_REQUIRED_UTILITIES = "git timeout find date"
_VAULT_KEYS = ("vault_path", "vault")
_MANIFEST_NAMES = (
    "symbiosis-brain.json",
    ".symbiosis-brain/install-manifest.json",
    ".symbiosis-brain/manifest.json",
    ".symbiosis-brain/install.json",
)


def _is_windows() -> bool:
    return os.name == "nt"


def _debug(message: str) -> None:
    pre_action_config._debug_log(f"sync launcher: {message}")


def packaged_sync_script() -> Path:
    """Return the packaged ``brain-sync.sh`` for editable and wheel installs."""
    package_init = Path(__file__).resolve().parents[2] / "__init__.py"
    return packaged_dir(str(package_init), "hooks") / _SYNC_SCRIPT


def verify_bash_prerequisites(bash: Path) -> bool:
    """Verify the shell utilities required by the existing sync script."""
    try:
        result = subprocess.run(
            [bash, "-lc", f"command -v {_REQUIRED_UTILITIES}"],
            check=False,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0 and not result.stderr.strip()


def find_bash(*, verify_prerequisites: bool = True) -> Path | None:
    """Return the verified executable pinned in protected Codex config.

    Runtime deliberately does not consult ``PATH`` or ``GIT_BASH``.  Setup and
    doctor own discovering and persisting the path; this function only verifies
    that pinned runtime input still exists and has the required shell tools.
    """
    try:
        pinned = load_codex_config().sync_bash_path
    except Exception as exc:
        _debug(f"Codex config unavailable: {type(exc).__name__}")
        return None

    if pinned is None:
        _debug("Git Bash is not pinned in Codex config")
        return None
    pinned = Path(pinned)
    is_absolute = (
        PureWindowsPath(os.fspath(pinned)).is_absolute()
        if _is_windows()
        else pinned.is_absolute()
    )
    if not is_absolute:
        _debug("pinned Git Bash is not absolute")
        return None
    if not pinned.exists():
        _debug("pinned Git Bash is missing")
        return None
    if verify_prerequisites and not verify_bash_prerequisites(pinned):
        _debug("pinned Git Bash prerequisites are unavailable")
        return None
    return pinned


def _sync_environment(vault: Path) -> dict[str, str]:
    env = os.environ.copy()
    env["SYMBIOSIS_BRAIN_VAULT"] = os.fspath(vault)
    return env


def _unavailable_message() -> str:
    return (
        "packaged sync unavailable: Git Bash is not pinned, missing, "
        "or required prerequisites (git, timeout, find, date) are unavailable"
    )


def run_manual_sync(vault: Path) -> int:
    """Run the packaged sync script in the foreground."""
    bash = find_bash()
    if bash is None:
        print(_unavailable_message(), file=sys.stderr)
        return 1
    try:
        result = subprocess.run(
            [bash, packaged_sync_script(), "manual"],
            env=_sync_environment(vault),
            check=False,
            encoding="utf-8",
            errors="replace",
        )
    except (OSError, subprocess.SubprocessError) as exc:
        _debug(f"manual sync launch failed: {type(exc).__name__}")
        print(
            f"packaged sync unavailable: launch failed ({type(exc).__name__})",
            file=sys.stderr,
        )
        return 1
    return result.returncode


def launch_auto_sync(vault: Path, *, detach: bool = True) -> bool:
    """Start auto-sync without waiting for Git.

    Hook workers pass ``detach=False`` so timeout/error cleanup still owns the
    child process group or Windows Job.  Standalone callers retain the detached
    behavior by default.
    """
    try:
        bash = find_bash(verify_prerequisites=False)
    except TypeError:
        # Compatibility with test seams and older embedders that replaced the
        # zero-argument helper; the normal production path never takes this.
        bash = find_bash()
    if bash is None:
        _debug(_unavailable_message())
        return False
    script = packaged_sync_script()

    kwargs: dict[str, object] = {
        "env": _sync_environment(vault),
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "close_fds": True,
    }
    if _is_windows():
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        if detach:
            creationflags |= getattr(subprocess, "DETACHED_PROCESS", 0)
        kwargs["creationflags"] = creationflags
    elif detach:
        kwargs["start_new_session"] = True

    try:
        subprocess.Popen(
            [bash, script, "auto"],
            **kwargs,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        _debug(f"auto sync launch failed: {type(exc).__name__}")
        return False
    return True


def _read_json(path: Path) -> Mapping[str, object] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _configured_vault() -> Path | None:
    home = resolve_codex_home()
    for relative in _MANIFEST_NAMES:
        data = _read_json(home / relative)
        if not data:
            continue
        for key in _VAULT_KEYS:
            value = data.get(key)
            if isinstance(value, str) and value and "\x00" not in value:
                return Path(value).expanduser().resolve(strict=False)
    return None


def _legacy_vault() -> Path:
    return (Path.home() / "symbiosis-brain-vault").resolve(strict=False)


def _resolve_vault(mode: str, explicit: str | None) -> Path:
    if explicit:
        return Path(explicit).expanduser().resolve(strict=False)
    if mode == "manual":
        configured = os.environ.get("SYMBIOSIS_BRAIN_VAULT", "").strip()
        if configured:
            return Path(configured).expanduser().resolve(strict=False)
    return _configured_vault() or _legacy_vault()


def run_cli(argv: list[str]) -> int:
    """Run ``packaged-sync manual|auto [--vault PATH]``."""
    parser = argparse.ArgumentParser(prog="symbiosis-brain packaged-sync")
    parser.add_argument("mode", choices=("manual", "auto"))
    parser.add_argument("--vault")
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return int(exc.code)

    vault = _resolve_vault(args.mode, args.vault)
    if args.mode == "manual":
        return run_manual_sync(vault)
    return 0 if launch_auto_sync(vault) else 1
