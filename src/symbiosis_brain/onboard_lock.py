"""Onboarding lockfile — prevents concurrent brain-project-init sessions
from creating duplicate artifacts for the same scope.

Spec: /tmp/symbiosis-brain-onboard-<scope>.lock holds PID + timestamp.
Stale (older than timeout_s) locks are reclaimed.
"""
import os
import tempfile
import time
from pathlib import Path

LOCK_DIR = Path(tempfile.gettempdir())


def _lockfile(scope: str) -> Path:
    return LOCK_DIR / f"symbiosis-brain-onboard-{scope}.lock"


def is_locked(scope: str) -> bool:
    """Returns True if a lockfile exists for `scope`, regardless of staleness.

    Note: `acquire_lock` may still succeed by reclaiming a stale lock —
    use `is_locked` only as a non-authoritative check. For ownership,
    call `acquire_lock` and inspect its return value.
    """
    return _lockfile(scope).exists()


def acquire_lock(scope: str, timeout_s: int = 30) -> bool:
    """Try to acquire onboarding lock for `scope`.

    Returns True if locked successfully (caller owns onboarding).
    Returns False if a fresh lock exists (caller should wait or skip).
    Reclaims stale lock (older than timeout_s).

    Raises:
        OSError: If LOCK_DIR is unwritable (disk full, permission denied,
            network share offline). Caller should not silently degrade —
            these are hard failures distinct from "another process holds lock".
    """
    lockfile = _lockfile(scope)
    if lockfile.exists():
        age = time.time() - lockfile.stat().st_mtime
        if age <= timeout_s:
            return False
        # Stale — reclaim
        try:
            lockfile.unlink()
        except FileNotFoundError:
            pass  # racy delete by another reclaimer — fine, fall through
    try:
        fd = os.open(str(lockfile), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    except FileExistsError:
        return False  # another process won the race
    with os.fdopen(fd, "w") as f:
        f.write(f"{os.getpid()}\n{int(time.time())}\n")
    return True


def release_lock(scope: str) -> None:
    """Release the onboarding lock. No-op if missing."""
    try:
        _lockfile(scope).unlink()
    except FileNotFoundError:
        pass
