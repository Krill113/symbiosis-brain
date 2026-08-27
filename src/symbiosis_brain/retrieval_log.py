"""Retrieval log (Stage 2): what memory actually surfaced, on every path.

Two tables in <vault>/.index/brain.db (I-1, I-2). This module owns the writer,
and its rules are all consequences of a measurement, not taste:

* its OWN connection with its OWN pragmas (§2.4 п. 1) — busy_timeout is 2 s
  against Storage's 30 s (storage.py:19) because the log gives up first, and
  wal_autocheckpoint=0 keeps the number of checkpointing connections at one
  (§12, risk 16: a second checkpointing writer is the WAL-Reset precondition);
* one short BEGIN IMMEDIATE transaction per event, never inside someone
  else's (§2.4 п. 2);
* no batching at all — serve force-exits through os._exit(0)
  (server.py:1028-1030) and a buffer would die with it (§2.4 п. 3);
* fail-open with a COUNTED skip (§2.4 п. 4): SQLITE_BUSY costs one retry and a
  counter, only a structural error disables the writer for the process, and
  the counter is persisted so `brain-cli report` — a different process — can
  still say how incomplete the log is.

Nothing here raises: `record`, `record_read`, `record_context` and `rotate`
swallow everything. Telemetry may cost a log line, never a recall.
"""
from __future__ import annotations

import logging
import os
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

logger = logging.getLogger("symbiosis-brain.retrieval_log")

QUERY_LOG_MAX_CHARS = 2000
"""Hard cap on the logged query text (§12, risk 4: volume + privacy)."""

RETENTION_DAYS = 90
_BUSY_TIMEOUT_MS = 2000

SKIPPED_TOTAL_KEY = "retrieval_log_skipped_total"
"""schema_version key holding the cumulative, cross-process skip count."""

ROTATED_AT_KEY = "retrieval_log_rotated_at"
"""schema_version key with the ISO date of the last rotation. Written by the
CALLER (server._init), never here — see I-11."""

ENV_SWITCH = "SYMBIOSIS_BRAIN_RETRIEVAL_LOG"


# --- I-1 / I-2: DDL as five separate statements, never one script ------------
# `executescript` does an implicit COMMIT and would tear the migration's
# BEGIN IMMEDIATE apart (measured, §2.3). Kept here, next to the writer, so the
# columns and the INSERTs below cannot drift apart; storage.py imports the
# tuple for the migration (retrieval_log imports nothing from the package, so
# the direction is cycle-free).
RETRIEVAL_LOG_STATEMENTS: tuple[str, ...] = (
    """CREATE TABLE IF NOT EXISTS retrieval_event (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts            TEXT    NOT NULL,                      -- ISO-8601 UTC, datetime.now(timezone.utc).isoformat()
    session_id    TEXT,                                  -- NULL для серверных путей: сервер sid не знает
    origin        TEXT    NOT NULL DEFAULT 'unknown',    -- 'main' | 'subagent' | 'unknown'
    source        TEXT    NOT NULL,                      -- см. таблицу §2.1
    tool          TEXT,                                  -- tool_name, спровоцировавший PreToolUse-путь; иначе NULL
    query         TEXT    NOT NULL DEFAULT '',           -- полный текст, обрезанный до QUERY_LOG_MAX_CHARS
    scope         TEXT,
    mode          TEXT,                                  -- 'preview' | 'gist' | 'read' | 'context'
    fts_mode      TEXT,                                  -- ЭФФЕКТИВНЫЙ режим: 'any' | 'all' | 'fallback_any'; запрошенный 'all_then_any' сюда не пишется никогда (§2.9)
    n_returned    INTEGER NOT NULL DEFAULT 0,
    dedup_dropped INTEGER NOT NULL DEFAULT 0,            -- сколько хитов срезал SeenStore
    vec_enabled   INTEGER NOT NULL DEFAULT 0,
    latency_ms    INTEGER,                               -- время самого поиска
    e2e_ms        INTEGER,                               -- хук-пути: от точки ПЕРЕД запуском `uv run` до момента формирования блока в python (§2.8); НЕ включает матчер action-rules и пролог bash
    client        TEXT                                   -- 'claude-code/2.1.246' (сервер) | 'hook' (CLI-пути)
)""",
    "CREATE INDEX IF NOT EXISTS idx_retrieval_event_ts     ON retrieval_event(ts)",
    "CREATE INDEX IF NOT EXISTS idx_retrieval_event_source ON retrieval_event(source)",
    """CREATE TABLE IF NOT EXISTS retrieval_hit (
    event_id  INTEGER NOT NULL,
    rank      INTEGER NOT NULL,                          -- 0-based, порядок выдачи агенту
    note_path TEXT    NOT NULL,                          -- текст, БЕЗ FK на notes
    score     REAL,                                      -- post-boost RRF (_score)
    in_both   INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (event_id, rank)
)""",
    "CREATE INDEX IF NOT EXISTS idx_retrieval_hit_path ON retrieval_hit(note_path)",
)

_INSERT_EVENT = (
    "INSERT INTO retrieval_event ("
    " ts, session_id, origin, source, tool, query, scope, mode, fts_mode,"
    " n_returned, dedup_dropped, vec_enabled, latency_ms, e2e_ms, client"
    ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
)

_INSERT_HIT = (
    "INSERT INTO retrieval_hit (event_id, rank, note_path, score, in_both)"
    " VALUES (?, ?, ?, ?, ?)"
)

# Atomic increment without a read (§2.4 п. 4). UPSERT needs SQLite >= 3.24;
# the live build is 3.50.4 and serve logs its version on every start
# (server.py:104).
_UPSERT_SKIPPED = (
    "INSERT INTO schema_version (key, version) VALUES (?, ?)"
    " ON CONFLICT(key) DO UPDATE SET version = version + excluded.version"
)


@dataclass(frozen=True)
class LogContext:
    """Everything about an event that the writer cannot work out on its own.

    `session_id` and `origin` have no channel at all on the serve side (§2.5):
    an stdio MCP session has no session id, and nothing tells it whether the
    caller is a subagent. Guessing 'main' there would be a lie in the data, so
    the defaults stay NULL / 'unknown'.
    """

    source: str                      # 'mcp_search'|'mcp_read'|'mcp_context'|'hook_prompt'|'hook_pre_action'|'legacy_gist'
    db_path: Path
    session_id: str | None = None
    origin: str = "unknown"          # 'main'|'subagent'|'unknown'
    tool: str | None = None
    client: str | None = None
    started_at: float | None = None  # epoch seconds, for e2e_ms


# --- process-wide state (§2.4 п. 4) -----------------------------------------
_conn: sqlite3.Connection | None = None
_conn_path: str | None = None
_disabled = False
_warned = False
_skipped = 0
_skipped_pending = 0
_env_enabled: bool | None = None


def is_enabled(config=None) -> bool:
    """Both switches at once (§2.7): env `off` beats whatever the file says.

    `config` is a PreActionConfig on the hook paths and None in `serve`, which
    has no config file. The env value is read once per process (I-5); anything
    other than `off` means `on`.
    """
    global _env_enabled
    if _env_enabled is None:
        _env_enabled = os.environ.get(ENV_SWITCH, "on").strip().lower() != "off"
    if not _env_enabled:
        return False
    if config is None:
        return True
    # CP-3 adds the field (I-6); the default keeps CP-2 honest either way.
    return bool(getattr(config, "retrieval_log_enabled", True))


def detect_origin(payload: dict | None) -> str:
    """'main' | 'subagent' | 'unknown' for a hook-path event (§2.5).

    Signals, checked in order of evidence:

    1. `CLAUDE_CODE_CHILD_SESSION=1` — measured [замер лида, §2.5]: the
       environment of a process spawned by a subagent carries it.
    2. a non-empty `agent_id` in the hook payload — measured live in the CP-3
       preflight (review/preflight-step-b/README.md, 2026-08-27, done by the
       owner before this checkpoint). §2.5's ORIGINAL hypothesis for signal 2
       — a `subagents` path segment inside `transcript_path` — was measured
       FALSE there: `transcript_path` and `session_id` are ALWAYS the PARENT
       session's, for a tool call from an Agent-tool subagent and from a
       workflow-subagent alike (the hook DOES run for both; it just never
       sees its own transcript). Per 00-plan §2 Р6 an unconfirmed hypothesis
       is struck, not patched — and the same preflight named the signal that
       actually works. `agent_type` (e.g. 'workflow-subagent') is
       informational only: no DDL column exists for it and none is added on
       this checkpoint.

    NOT by session_id or transcript_path: both are the PARENT's, always (same
    preflight). No payload at all means no channel, and that is 'unknown',
    not a guessed 'main' (the legacy `_gist_search` path, §2.1).
    """
    if not isinstance(payload, dict):
        return "unknown"
    if os.environ.get("CLAUDE_CODE_CHILD_SESSION") == "1":
        return "subagent"
    agent_id = payload.get("agent_id")
    if isinstance(agent_id, str) and agent_id:
        return "subagent"
    return "main"


def skipped_count() -> int:
    """Events THIS process dropped since start (in-memory, §2.4 п. 4).

    Goes into coverage['skipped_process'] and nowhere else: in `brain-cli
    report` it is structurally always 0, which is why the persistent counter
    in schema_version exists (I-4, I-26).
    """
    return _skipped


# --- writer ------------------------------------------------------------------

def record(ctx: LogContext, *, query: str, scope: str | None, mode: str | None,
           fts_mode: str | None, hits: list[dict], latency_ms: int,
           vec_enabled: bool, dedup_dropped: int = 0) -> None:
    """One surfaced result set. Never raises.

    `hits` is the list the AGENT saw, not the raw pool (§2.9): rank is the
    0-based index in it, `_score` and `_in_both` are read straight off each
    hit. `fts_mode` is the EFFECTIVE mode ('any'|'all'|'fallback_any'); the
    requested 'all_then_any' never reaches the column (I-4).
    `vec_enabled` has no default on purpose — the column is NOT NULL and each
    path has exactly one legal source for it (I-4, I-7, I-8).
    """
    if not is_enabled():
        return
    _write_event(
        ctx,
        query=query, scope=scope, mode=mode, fts_mode=fts_mode,
        n_returned=len(hits or []), dedup_dropped=dedup_dropped,
        vec_enabled=vec_enabled, latency_ms=latency_ms, hits=_hit_rows(hits),
    )


def record_read(ctx: LogContext, *, note_path: str, latency_ms: int) -> None:
    """brain_read on an EXISTING note: exactly one hit, no search behind it.

    The NOT NULL columns are filled by the I-4 table: query = the requested
    path, mode = 'read', n_returned = 1, vec_enabled = 0 (the vector half was
    never asked anything), one hit row with score NULL and in_both 0.
    A MISS is not a read and does not come here — the branch logs it through
    `record(..., hits=[])` so that "searched and found nothing" stays
    distinguishable from "did not search" (I-4, server.py:555-556).
    """
    if not is_enabled():
        return
    _write_event(
        ctx, query=note_path, scope=None, mode="read", fts_mode=None,
        n_returned=1, dedup_dropped=0, vec_enabled=False, latency_ms=latency_ms,
        hits=[(0, note_path, None, 0)],
    )


def record_context(ctx: LogContext, *, entity: str, n_returned: int,
                   latency_ms: int) -> None:
    """brain_context: no hit rows at all — it returns a graph, not notes
    (§2.1, row 3). n_returned carries the neighbour count, including 0 for the
    early exit on an unknown entity (server.py:742-743)."""
    if not is_enabled():
        return
    _write_event(
        ctx, query=entity, scope=None, mode="context", fts_mode=None,
        n_returned=n_returned, dedup_dropped=0, vec_enabled=False,
        latency_ms=latency_ms, hits=[],
    )


def close() -> None:
    """Flush point (в) of §2.4 п. 4, plus connection teardown.

    The hook entry points call this in `finally`; `serve` cannot rely on it
    (os._exit(0), server.py:1028-1030), which is exactly why point (а) — the
    delta riding along with the next successful insert — carries the weight.
    """
    global _conn, _conn_path, _skipped_pending
    conn, path = _conn, _conn_path
    _conn, _conn_path = None, None
    if conn is not None:
        if _skipped_pending:
            try:
                conn.execute(_UPSERT_SKIPPED, (SKIPPED_TOTAL_KEY, _skipped_pending))
                _skipped_pending = 0
            except Exception:
                logger.debug("retrieval log: skip-counter flush failed", exc_info=True)
        try:
            conn.close()
        except Exception:
            pass
    if _skipped_pending and path:
        _flush_skipped(Path(path))


# --- internals ---------------------------------------------------------------

def _hit_rows(hits) -> list[tuple]:
    """(rank, note_path, score, in_both) per hit. A malformed hit is skipped,
    never fatal — rank stays the true position in the emitted list."""
    rows: list[tuple] = []
    for rank, h in enumerate(hits or []):
        if not isinstance(h, dict):
            continue
        raw = h.get("_score")
        try:
            score = float(raw) if raw is not None else None
        except (TypeError, ValueError):
            score = None
        rows.append((rank, str(h.get("path") or ""), score, 1 if h.get("_in_both") else 0))
    return rows


def _connect(db_path: Path) -> sqlite3.Connection:
    """The writer's own connection, cached per process and per db file.

    check_same_thread=False: the process is multi-threaded and the write comes
    from the same threads as the search (server.py:979). isolation_level=None:
    we drive the transaction ourselves. busy_timeout is 2 s against Storage's
    30 s — the log gives up first. wal_autocheckpoint=0 keeps checkpointing to
    the single Storage connection (§12, risk 16).
    """
    global _conn, _conn_path
    key = str(db_path)
    if _conn is not None and _conn_path == key:
        return _conn
    if _conn is not None:
        try:
            _conn.close()
        except Exception:
            pass
        _conn, _conn_path = None, None
    conn = sqlite3.connect(key, check_same_thread=False, isolation_level=None)
    conn.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
    conn.execute("PRAGMA wal_autocheckpoint=0")
    _conn, _conn_path = conn, key
    return conn


def _e2e_ms(started_at: float | None) -> int | None:
    """Hook paths only: from just before `uv run` to the moment the block is
    built (§2.8). NULL whenever the caller had nothing usable to pass."""
    if not started_at:
        return None
    try:
        delta = time.time() - float(started_at)
    except (TypeError, ValueError):
        return None
    return int(delta * 1000) if delta >= 0 else None


def _is_busy(exc: sqlite3.OperationalError) -> bool:
    msg = str(exc).lower()
    return "locked" in msg or "busy" in msg


def _warn_once(fmt: str, *args) -> None:
    """First refusal of any kind warns exactly once, the rest is debug
    (§2.4 п. 4). Without the warning an incomplete log reads as a complete one."""
    global _warned
    if not _warned:
        _warned = True
        logger.warning(fmt, *args)
    else:
        logger.debug(fmt, *args)


def _count_skip(exc: BaseException) -> None:
    global _skipped, _skipped_pending
    _skipped += 1
    _skipped_pending += 1
    _warn_once("retrieval log: event skipped (%s)", exc)


def _disable(db_path: Path | None, exc: BaseException) -> None:
    """Structural error: it will not pass next time either, so stop trying for
    this process (§2.4 п. 4) — but count the loss and try to persist it."""
    global _disabled, _skipped, _skipped_pending
    _skipped += 1
    _skipped_pending += 1
    _disabled = True
    _warn_once("retrieval log disabled for this process: %s", exc)
    _flush_skipped(db_path)          # flush point (б), best-effort


def _flush_skipped(db_path: Path | None) -> None:
    """Points (б) and (в): push the delta out on a connection of its own.

    Deliberately NOT the cached one — at point (б) it may be the very handle
    that just failed. `schema_version` usually survives a failure that only
    touches the log tables, so this is worth one attempt; any error is
    swallowed and the delta is kept, never lost (§2.4 п. 4).
    """
    global _skipped_pending
    pending = _skipped_pending
    if not pending or db_path is None:
        return
    conn = None
    try:
        conn = sqlite3.connect(str(db_path), check_same_thread=False,
                               isolation_level=None)
        conn.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
        conn.execute("PRAGMA wal_autocheckpoint=0")
        conn.execute(_UPSERT_SKIPPED, (SKIPPED_TOTAL_KEY, pending))
        _skipped_pending -= pending
    except Exception:
        logger.debug("retrieval log: skip-counter flush failed", exc_info=True)
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def _rollback(conn: sqlite3.Connection) -> None:
    try:
        conn.execute("ROLLBACK")
    except Exception:
        pass


def _write_event(ctx: LogContext, *, query, scope, mode, fts_mode, n_returned,
                 dedup_dropped, vec_enabled, latency_ms, hits) -> None:
    """One event + its hits + (when non-empty) the skip delta, in ONE short
    BEGIN IMMEDIATE transaction (§2.4 п. 2, п. 4-а). Never raises."""
    global _skipped_pending
    if _disabled:
        return
    params = (
        datetime.now(timezone.utc).isoformat(),
        ctx.session_id or None,
        ctx.origin or "unknown",
        ctx.source,
        ctx.tool,
        (query or "")[:QUERY_LOG_MAX_CHARS],
        scope,
        mode,
        fts_mode,
        int(n_returned or 0),
        int(dedup_dropped or 0),
        1 if vec_enabled else 0,
        int(latency_ms) if latency_ms is not None else None,
        _e2e_ms(ctx.started_at),
        ctx.client,
    )
    for attempt in (0, 1):
        pending = 0
        try:
            conn = _connect(ctx.db_path)
            conn.execute("BEGIN IMMEDIATE")
            try:
                event_id = conn.execute(_INSERT_EVENT, params).lastrowid
                for rank, note_path, score, in_both in hits:
                    conn.execute(_INSERT_HIT, (event_id, rank, note_path, score, in_both))
                pending = _skipped_pending
                if pending:
                    conn.execute(_UPSERT_SKIPPED, (SKIPPED_TOTAL_KEY, pending))
                conn.execute("COMMIT")
            except Exception:
                _rollback(conn)
                raise
            if pending:
                _skipped_pending -= pending      # only after a successful COMMIT
            return
        except sqlite3.OperationalError as exc:
            if _is_busy(exc):
                # ONE retry, immediately: the transaction costs 1.22 ms and
                # busy_timeout has already done its 2 s ([отчёт 01, F27, F22]).
                if attempt == 0:
                    continue
                _count_skip(exc)
                return
            _disable(ctx.db_path, exc)           # no such table, bad schema, …
            return
        except Exception as exc:
            _disable(ctx.db_path, exc)
            return


def rotate(db_path: Path, *, days: int = RETENTION_DAYS) -> int:
    """Delete events older than `days`; return how many EVENTS went (I-11).

    Its own connection, opened and closed here, with the same pragmas as the
    writer (§2.4 п. 1). Deletes by AGE, never by row count. Never raises — the
    caller wraps it in try/except too, and both guards are needed: this one
    keeps the failure local, the caller's keeps _init alive (§2.6).
    Knows nothing about Storage or schema_version: the daily gate and the
    `retrieval_log_rotated_at` stamp belong to the caller.
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    conn = None
    try:
        conn = sqlite3.connect(str(db_path), check_same_thread=False,
                               isolation_level=None)
        conn.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
        conn.execute("PRAGMA wal_autocheckpoint=0")
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute(
                "DELETE FROM retrieval_hit  WHERE event_id IN"
                " (SELECT id FROM retrieval_event WHERE ts < ?)",
                (cutoff,),
            )
            deleted = conn.execute(
                "DELETE FROM retrieval_event WHERE ts < ?", (cutoff,)
            ).rowcount
            conn.execute("COMMIT")
        except Exception:
            _rollback(conn)
            raise
        return max(int(deleted or 0), 0)
    except Exception:
        logger.debug("retrieval log rotation failed", exc_info=True)
        return 0
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
