"""SQLite build health checks.

Upstream SQLite carried a WAL-Reset race from 3.7.0 through 3.51.2: with WAL
journaling and two or more connections (threads or processes) writing and
checkpointing at the same time, a reset of the write-ahead log could drop or
corrupt committed pages. Fixed upstream in 3.51.3 (2026-03-13), backported to
3.50.7 and 3.44.6.

Symbiosis Brain meets the preconditions by construction — storage.py turns on
journal_mode=WAL and wal_autocheckpoint on every connection, and two Claude Code
windows mean two `serve` processes on one brain.db — but the stock CPython that
carries the fix is 3.15 (2026-10-01). The decision is to detect and report, never
to patch the interpreter's sqlite3.dll and never to swap the driver: doctor shows
the version, doctor runs PRAGMA quick_check on every run (owner decision 3),
`doctor --deep` adds the thorough PRAGMA integrity_check, and serve logs the version.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

# (lowest safe version, first version that is out of this range again)
_SAFE_RANGES: tuple[tuple[tuple[int, int, int], tuple[int, int, int] | None], ...] = (
    ((3, 51, 3), None),        # upstream fix and everything after it
    ((3, 50, 7), (3, 51, 0)),  # backport on the 3.50.x branch
    ((3, 44, 6), (3, 45, 0)),  # backport on the 3.44.x branch
)


def _parse(version: str) -> tuple[int, int, int] | None:
    parts = str(version).strip().split(".")
    if len(parts) < 2:
        return None
    try:
        nums = [int(p) for p in parts[:3]]
    except ValueError:
        return None
    while len(nums) < 3:
        nums.append(0)
    return nums[0], nums[1], nums[2]


def sqlite_ok(version: str) -> bool:
    """True when this SQLite build is NOT subject to the WAL-Reset race.

    Safe: >= 3.51.3, or 3.50.7 <= v < 3.51.0, or 3.44.6 <= v < 3.45.0 (backports).
    An unrecognised version string returns True — a false alarm is worse than no
    alarm for something that is parked on purpose.
    """
    parsed = _parse(version)
    if parsed is None:
        return True
    for low, high in _SAFE_RANGES:
        if parsed >= low and (high is None or parsed < high):
            return True
    return False


def sqlite_warning(version: str) -> str | None:
    """One warning line for doctor and for serve.log, or None when the build is fine."""
    if sqlite_ok(version):
        return None
    return (
        f"{version} — vulnerable to WAL-Reset (<3.51.3, backported to 3.50.7); "
        f"fix expected with CPython 3.15+. No corruption seen so far — "
        f"run `symbiosis-brain doctor --deep` to verify the database."
    )


def _pragma_check(db_path: Path, pragma: str) -> tuple[bool, str]:
    """Run `PRAGMA {pragma}` on a separate read-only connection.

    Returns (ok, first line of the output). Never raises: a missing file, a bad
    path or a locked database all come back as (False, reason). `mode=ro` also
    guarantees this never creates the database it was asked to inspect.
    """
    conn = None
    try:
        uri = Path(db_path).expanduser().resolve().as_uri() + "?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=5)
        rows = conn.execute(f"PRAGMA {pragma}").fetchall()
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
    if not rows:
        return False, f"{pragma} returned no rows"
    first = str(rows[0][0])
    return first.strip().lower() == "ok", first


def quick_check(db_path: Path) -> tuple[bool, str]:
    """Run `PRAGMA quick_check` on a separate read-only connection.

    Returns (ok, first line of the output). Never raises: a missing file, a bad
    path or a locked database all come back as (False, reason). `mode=ro` also
    guarantees this never creates the database it was asked to inspect.
    """
    return _pragma_check(db_path, "quick_check")


def integrity_check(db_path: Path) -> tuple[bool, str]:
    """Run `PRAGMA integrity_check` — the thorough sibling of quick_check.

    quick_check skips index-vs-table cross-references; integrity_check walks them,
    which is why it is the one behind `--deep`. Same contract: never raises,
    read-only connection, returns (ok, first line).
    """
    return _pragma_check(db_path, "integrity_check")
