import os
import time
from pathlib import Path

import pytest

# Module under test does not exist yet — import will fail at collection.
from symbiosis_brain.write_lock import note_write_lock, _lock_path_for


@pytest.fixture(autouse=True)
def isolated_lock_dir(tmp_path, monkeypatch):
    monkeypatch.setattr("symbiosis_brain.write_lock.LOCK_DIR", tmp_path)
    yield


def test_lock_acquire_and_release_creates_then_removes(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    with note_write_lock(vault, "wiki/n.md"):
        lockfile = _lock_path_for(vault, "wiki/n.md")
        assert lockfile.exists()
    # After context exit, lock removed
    assert not lockfile.exists()


def test_lock_id_differs_by_vault(tmp_path):
    v1 = tmp_path / "v1"
    v2 = tmp_path / "v2"
    v1.mkdir()
    v2.mkdir()
    p1 = _lock_path_for(v1, "wiki/same.md")
    p2 = _lock_path_for(v2, "wiki/same.md")
    assert p1 != p2


def test_lock_id_differs_by_path(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    p1 = _lock_path_for(vault, "wiki/a.md")
    p2 = _lock_path_for(vault, "wiki/b.md")
    assert p1 != p2


def test_concurrent_acquire_serializes(tmp_path):
    """Two threads acquiring same lock must serialize."""
    import threading

    vault = tmp_path / "vault"
    vault.mkdir()

    order: list[str] = []

    def worker(name: str, hold_s: float):
        with note_write_lock(vault, "wiki/n.md", timeout_s=10):
            order.append(f"{name}:enter")
            time.sleep(hold_s)
            order.append(f"{name}:exit")

    t1 = threading.Thread(target=worker, args=("A", 0.3))
    t2 = threading.Thread(target=worker, args=("B", 0.0))
    t1.start()
    time.sleep(0.05)  # bias t1 to acquire first; either order is acceptable
    t2.start()
    t1.join(); t2.join()

    # Whichever thread got the lock first must enter+exit BEFORE the other enters
    # (no interleaving). Both orderings are valid lock semantics.
    assert order in (
        ["A:enter", "A:exit", "B:enter", "B:exit"],
        ["B:enter", "B:exit", "A:enter", "A:exit"],
    ), f"unexpected interleaving: {order!r}"


def test_stale_lock_is_reclaimed(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    lockfile = _lock_path_for(vault, "wiki/n.md")
    # Pre-create a stale lock (mtime 120s old)
    lockfile.parent.mkdir(parents=True, exist_ok=True)
    lockfile.write_text("99999\n0\n")
    old = time.time() - 120
    os.utime(lockfile, (old, old))

    # Acquire with timeout_s=60 should succeed (stale)
    with note_write_lock(vault, "wiki/n.md", timeout_s=60):
        contents = lockfile.read_text()
        assert str(os.getpid()) in contents
    assert not lockfile.exists()


def test_lock_timeout_raises(tmp_path):
    """If lock is held longer than timeout_s, second acquire raises TimeoutError."""
    import threading

    vault = tmp_path / "vault"
    vault.mkdir()

    holder_done = threading.Event()

    def holder():
        with note_write_lock(vault, "wiki/n.md", timeout_s=10):
            holder_done.wait(timeout=5)

    t = threading.Thread(target=holder)
    t.start()
    time.sleep(0.1)  # let holder acquire

    with pytest.raises(TimeoutError):
        with note_write_lock(vault, "wiki/n.md", timeout_s=1):
            pass

    holder_done.set()
    t.join()
