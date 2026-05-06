"""Per-note file lock — serializes concurrent writes to the same note across
processes (and threads within the same process). Mirrors `onboard_lock.py`
pattern: atomic O_EXCL creation + stale-lock reclamation by mtime.

Hash key is `(vault_path.resolve(), rel_path)` so:
- Different vaults don't collide on the same rel_path.
- Different notes in the same vault do NOT block each other.
- Same note across processes serializes.
"""
from __future__ import annotations

import hashlib
import os
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

LOCK_DIR = Path(tempfile.gettempdir())


def _lock_id(vault_path: Path, rel_path: str) -> str:
    raw = f"{Path(vault_path).resolve()}::{rel_path}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:16]


def _lock_path_for(vault_path: Path, rel_path: str) -> Path:
    return LOCK_DIR / f"sb-write-{_lock_id(vault_path, rel_path)}.lock"


@contextmanager
def note_write_lock(
    vault_path: Path,
    rel_path: str,
    timeout_s: int = 60,
    poll_s: float = 0.05,
):
    """Acquire a per-note write lock; yield; release on exit (even on exception).

    Raises TimeoutError if the lock cannot be acquired within `timeout_s`.
    Stale locks (mtime older than `timeout_s`) are reclaimed.
    """
    lockfile = _lock_path_for(vault_path, rel_path)
    deadline = time.time() + timeout_s
    while True:
        try:
            fd = os.open(str(lockfile), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            # Check deadline first — if time is up, give up regardless of staleness.
            if time.time() >= deadline:
                raise TimeoutError(
                    f"Could not acquire write lock for {rel_path} within {timeout_s}s "
                    f"(held by another process at {lockfile})"
                )
            # Reclaim only if demonstrably stale (2× timeout to avoid racing with
            # a live holder whose hold time approaches timeout_s).
            try:
                age = time.time() - lockfile.stat().st_mtime
            except FileNotFoundError:
                continue  # racy unlink — retry acquire
            if age >= timeout_s * 2:
                try:
                    lockfile.unlink()
                except FileNotFoundError:
                    pass
                continue  # try again immediately
            time.sleep(poll_s)
            continue

        # Acquired
        with os.fdopen(fd, "w") as f:
            f.write(f"{os.getpid()}\n{int(time.time())}\n")
        try:
            yield
        finally:
            try:
                lockfile.unlink()
            except FileNotFoundError:
                pass
        return
