import os
import time
from pathlib import Path
import pytest
from symbiosis_brain.onboard_lock import (
    acquire_lock,
    release_lock,
    is_locked,
    LOCK_DIR,
)


@pytest.fixture(autouse=True)
def isolated_lock_dir(tmp_path, monkeypatch):
    monkeypatch.setattr("symbiosis_brain.onboard_lock.LOCK_DIR", tmp_path)
    yield


def test_acquire_when_no_lock_returns_true(tmp_path):
    assert acquire_lock("foo", timeout_s=30) is True
    assert is_locked("foo") is True


def test_release_removes_lock(tmp_path):
    acquire_lock("foo", timeout_s=30)
    release_lock("foo")
    assert is_locked("foo") is False


def test_acquire_when_fresh_lock_returns_false(tmp_path):
    acquire_lock("foo", timeout_s=30)
    # Simulate second process trying to acquire
    assert acquire_lock("foo", timeout_s=30) is False


def test_acquire_when_stale_lock_returns_true(tmp_path):
    acquire_lock("foo", timeout_s=30)
    # Make lock 60s old
    lockfile = tmp_path / "symbiosis-brain-onboard-foo.lock"
    old_mtime = time.time() - 60
    os.utime(lockfile, (old_mtime, old_mtime))
    # New caller should grab it
    assert acquire_lock("foo", timeout_s=30) is True
    # Verify the lockfile was rewritten with current PID, not appended
    contents = lockfile.read_text()
    assert str(os.getpid()) in contents
    assert contents.count("\n") == 2  # exactly PID + timestamp lines


def test_release_no_lock_is_noop(tmp_path):
    # Should not raise
    release_lock("nonexistent")


def test_two_scopes_independent(tmp_path):
    assert acquire_lock("foo", timeout_s=30) is True
    assert acquire_lock("bar", timeout_s=30) is True
    assert is_locked("foo") and is_locked("bar")
    release_lock("foo")
    assert not is_locked("foo")
    assert is_locked("bar")
