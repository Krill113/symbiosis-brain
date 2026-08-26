"""Locate a usable bash for tests that spawn the hook scripts.

Never a bare "bash": on a Windows GitHub runner that resolves to the WSL stub
in %SystemRoot% (exit 1, empty stderr), and on a dev box whose PATH lacks
Git\\bin it resolves to nothing at all. Probe Git-for-Windows locations by
absolute path and health-check each candidate before returning it.

Shared by test_brain_save_trigger_routing.py and test_action_rules.py.
"""
import os
import shutil
import subprocess

import pytest


def _bash_works(path):
    """One-shot health probe: the bash must see POSIX coreutils (M2)."""
    try:
        return subprocess.run(
            [path, "-c", "command -v sed >/dev/null"],
            capture_output=True, timeout=15,
        ).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _find_bash(
    which=shutil.which,
    exists=os.path.exists,
    environ=os.environ,
    windows=(os.name == "nt"),
    works=None,
):
    """Absolute path to a usable bash, or None. Never a bare name."""
    if not windows:
        return which("bash")
    if works is None:
        works = _bash_works
    roots = []
    git = which("git")
    if git:
        roots.append(os.path.dirname(os.path.dirname(os.path.abspath(git))))
    for var, sub in (
        ("ProgramFiles", ("Git",)),
        ("ProgramFiles(x86)", ("Git",)),
        ("LocalAppData", ("Programs", "Git")),
    ):
        root = environ.get(var)
        if root:
            roots.append(os.path.join(root, *sub))
    candidates = [os.path.join(r, "bin", "bash.exe") for r in roots] \
               + [os.path.join(r, "usr", "bin", "bash.exe") for r in roots]
    for cand in candidates:
        if exists(cand) and works(cand):
            return cand
    found = which("bash.exe") or which("bash")
    if found:
        found = os.path.abspath(found)  # bare-name/cwd results become absolute (M5)
        windir = environ.get("SystemRoot") or environ.get("windir") or "C:\\Windows"
        windir_prefix = os.path.join(os.path.normcase(os.path.abspath(windir)), "")
        if not os.path.normcase(found).startswith(windir_prefix) and works(found):
            return found
    return None


_BASH = _find_bash()


def require_tool(path, name, hint):
    """M3 rule for every external binary these tests spawn: on a CI runner an
    unresolved tool is a FAILURE, because a skip there makes a whole slice of Windows
    coverage disappear with nobody noticing; on a dev box it stays a skip.
    Returns the path so callers can use it inline.
    """
    if path is None:
        if os.environ.get("CI") or os.environ.get("SB_REQUIRE_BASH"):
            pytest.fail(
                f"{name} unresolved on a CI runner - Windows coverage would silently vanish"
            )
        pytest.skip(hint)
    return path


def _bash():
    """Absolute bash path for spawning; fails on CI, skips locally, when absent."""
    return require_tool(
        _BASH, "bash",
        "bash not found: probed git-derived and standard Git-for-Windows "
        "locations (bin before usr/bin, health-checked) and PATH minus "
        "%SystemRoot%. Install Git for Windows.",
    )
