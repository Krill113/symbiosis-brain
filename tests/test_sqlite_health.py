"""SQLite build health: WAL-Reset version detector and PRAGMA quick_check.

The race itself (upstream 3.7.0…3.51.2) is NOT reproduced here — it needs two
concurrent writers hitting a WAL reset at the same moment. Lens D says so
explicitly: don't try. What is testable is the decision layer around it.
"""
import logging
import sqlite3

import pytest

from symbiosis_brain import sqlite_health


@pytest.mark.parametrize("version, expected", [
    ("3.44.5", False),   # below the 3.44.x backport
    ("3.44.6", True),    # 3.44.x backport
    ("3.50.4", False),   # what this machine ships today
    ("3.50.7", True),    # 3.50.x backport
    ("3.51.2", False),   # last vulnerable upstream build
    ("3.51.3", True),    # upstream fix
    ("3.53.4", True),    # what CPython 3.15 will carry
    ("weird", True),     # unparseable → never cry wolf
])
def test_sqlite_ok_matrix(version, expected):
    assert sqlite_health.sqlite_ok(version) is expected


def test_sqlite_warning_names_version_and_target():
    """Additive (beyond 00-plan): the warning is what a user actually reads, so
    pin its contents. The lead pinned three elements by name (00-plan 11.4): the
    running version, `vulnerable to WAL-Reset (<3.51.3)`, and that the fix is
    expected with CPython 3.15+ — plus the way to check the DB."""
    assert sqlite_health.sqlite_warning("3.51.3") is None

    warning = sqlite_health.sqlite_warning("3.50.4")
    assert warning is not None
    assert "3.50.4" in warning
    assert "3.51.3" in warning
    assert "WAL-Reset" in warning
    assert "3.15" in warning
    assert "--deep" in warning
    assert "\n" not in warning          # one logical line for the log and for doctor


def test_quick_check_ok_on_fresh_db(tmp_path):
    db = tmp_path / "brain.db"
    conn = sqlite3.connect(str(db))
    conn.execute("CREATE TABLE t(a)")
    conn.execute("INSERT INTO t VALUES (1)")
    conn.commit()
    conn.close()

    ok, detail = sqlite_health.quick_check(db)
    assert ok is True
    assert detail.strip().lower() == "ok"


def test_quick_check_fails_open_on_missing_file(tmp_path):
    missing = tmp_path / "nope.db"

    ok, detail = sqlite_health.quick_check(missing)

    assert ok is False
    assert detail                        # a readable reason, not a raised exception
    assert not missing.exists()          # read-only: never creates the database


def test_init_logs_sqlite_version_and_warning(tmp_vault, monkeypatch, caplog):
    """Additive (beyond 00-plan): the serve-side half of D1a. Without this the
    WARN in <vault>/.index/serve.log has no automated cover at all."""
    monkeypatch.setattr(
        "symbiosis_brain.search._embed", lambda texts: [[0.1] * 384 for _ in texts])
    monkeypatch.setattr(sqlite3, "sqlite_version", "3.50.4")

    from symbiosis_brain import server

    try:
        with caplog.at_level(logging.INFO, logger="symbiosis-brain"):
            server._init(tmp_vault)
        messages = [r.getMessage() for r in caplog.records]
    finally:
        if server._storage is not None:
            server._storage._conn.close()

    assert any(m == "SQLite 3.50.4" for m in messages), messages
    assert any("WAL-Reset" in m for m in messages), messages
