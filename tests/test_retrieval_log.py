"""Retrieval log (Stage 2, CP-2): DDL, migration, writer, rotation, switch.

Everything here is synthetic: made-up note paths, a made-up client
("testclient/9.9.9") and tmp_path vaults only. No live vault, no live
~/.claude, no live brain.db (CLAUDE.md, §11.2).
"""
from __future__ import annotations

import re
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from symbiosis_brain import retrieval_log
from symbiosis_brain.retrieval_log import LogContext
from symbiosis_brain.storage import Storage


@pytest.fixture(autouse=True)
def _clean_logger_state(monkeypatch):
    """The writer keeps process-wide state on purpose (§2.4 п. 4): one cached
    connection, a sticky `_disabled` and a skip counter that must survive a
    single event. In a test SESSION that state would leak from case to case,
    so every test starts and ends from zero. Touching the privates here is
    deliberate — the spec defines no public reset and must not grow one.

    The env switch is deleted, not merely re-read: `is_enabled` caches it once
    per process (I-5), and a developer who keeps SYMBIOSIS_BRAIN_RETRIEVAL_LOG=off
    in their own shell would otherwise turn this whole file green-by-silence.
    The two cases that DO exercise the switch set it themselves with
    monkeypatch.setenv, which runs after this fixture and wins."""
    monkeypatch.delenv(retrieval_log.ENV_SWITCH, raising=False)

    def _reset():
        retrieval_log.close()
        retrieval_log._disabled = False
        retrieval_log._warned = False
        retrieval_log._skipped = 0
        retrieval_log._skipped_pending = 0
        retrieval_log._env_enabled = None
    _reset()
    yield
    _reset()


def _ctx(db: Path, **kw) -> LogContext:
    """A server-side context with synthetic client id."""
    kw.setdefault("source", "mcp_search")
    kw.setdefault("client", "testclient/9.9.9")
    return LogContext(db_path=db, **kw)


def _rows(db: Path, sql: str, params: tuple = ()) -> list[tuple]:
    conn = sqlite3.connect(str(db))
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


# ---------- Task 2.1 / 2.2: DDL + migration (I-1, I-2, I-3) ----------

def test_migration_creates_both_tables_and_three_indexes(db_path: Path):
    storage = Storage(db_path)
    try:
        tables = set(storage.list_tables())
        assert "retrieval_event" in tables
        assert "retrieval_hit" in tables
        assert storage.get_schema_version("retrieval_log") == 2
        idx = {r[0] for r in _rows(
            db_path,
            "SELECT name FROM sqlite_master WHERE type='index' AND name LIKE 'idx_retrieval%'",
        )}
        assert idx == {
            "idx_retrieval_event_ts",
            "idx_retrieval_event_source",
            "idx_retrieval_hit_path",
        }
    finally:
        storage.close()


def test_retrieval_event_has_every_column_the_ddl_promises(db_path: Path):
    storage = Storage(db_path)
    try:
        cols = [r[1] for r in _rows(db_path, "PRAGMA table_info(retrieval_event)")]
    finally:
        storage.close()
    assert cols == [
        "id", "ts", "session_id", "origin", "source", "tool", "query", "scope",
        "mode", "fts_mode", "n_returned", "dedup_dropped", "vec_enabled",
        "latency_ms", "e2e_ms", "client",
        "query_kind",   # 2026-09-04, Plan B: last, so ALTER … ADD COLUMN on an old db lands in the same place
    ]


def test_migration_is_idempotent_and_keeps_existing_rows(db_path: Path, tmp_vault: Path):
    storage = Storage(db_path)
    storage.upsert_note(
        path="wiki/alpha.md", title="Alpha", content="Body", note_type="wiki",
        scope="global", tags=[], frontmatter={},
    )
    storage.close()

    storage = Storage(db_path)          # second open: migration must be a no-op
    try:
        assert storage.get_schema_version("retrieval_log") == 2
        assert len(storage.list_notes()) == 1
    finally:
        storage.close()


def test_ddl_is_five_separate_statements(db_path: Path):
    """I-2: a list of statements, never one script — `executescript` breaks the
    migration's BEGIN IMMEDIATE with an implicit COMMIT (§2.3)."""
    from symbiosis_brain.retrieval_log import RETRIEVAL_LOG_STATEMENTS
    assert isinstance(RETRIEVAL_LOG_STATEMENTS, tuple)
    assert len(RETRIEVAL_LOG_STATEMENTS) == 5
    assert sum(1 for s in RETRIEVAL_LOG_STATEMENTS if "CREATE TABLE" in s) == 2
    assert sum(1 for s in RETRIEVAL_LOG_STATEMENTS if "CREATE INDEX" in s) == 3
    # Точка с запятой ищется ВНЕ `--`-комментариев: три комментария DDL несут ';'
    # внутри русского текста (`tool`, `fts_mode`, `e2e_ms`), и это не разделитель
    # выражений. Смысл утверждения — «ни одно выражение не склеено со следующим»,
    # а дословность DDL по I-1/I-2 не подгоняется под тест.
    assert all(";" not in re.sub(r"--[^\n]*", "", s) for s in RETRIEVAL_LOG_STATEMENTS)


def test_detect_origin_is_unknown_without_a_payload(monkeypatch):
    """CP-2 ships the function and its base cases; the real signals are CP-3's
    task (§2.5, Р6). CLAUDE_CODE_CHILD_SESSION is cleared here for the same
    reason CP-3's own detect_origin tests clear it (tests/test_hook_telemetry.py):
    the test RUNNER can itself be a subagent, in which case that env var is
    already '1' in the real environment and would silently flip 'main' to
    'subagent' below — a CP-3 discovery, since CP-2's stub never read it."""
    monkeypatch.delenv("CLAUDE_CODE_CHILD_SESSION", raising=False)
    assert retrieval_log.detect_origin(None) == "unknown"
    assert retrieval_log.detect_origin({}) == "main"


# ---------- Task 2.4 / 2.5: writer, NOT NULL columns, fail-open ----------

def _one_event(db: Path) -> sqlite3.Row:
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute("SELECT * FROM retrieval_event ORDER BY id").fetchall()
        assert len(rows) == 1, f"expected exactly one event, got {len(rows)}"
        return rows[0]
    finally:
        conn.close()


# ---------- Plan B (2026-09-04): a service prompt gets a kind, not a shell exit ----------
# A prompt-hook query that the harness injected (a cross-session message, a
# task notification) is not a human prompt, yet it MUST still be logged: an
# early exit in the shell hook would mean no search, no row, and no denominator
# for any later recount. So the row is written and LABELLED, and the label comes
# from ONE list — the seven prefixes the 04.09 recount used verbatim, so the
# stored labels reproduce its published numbers (61 of 1113 baseline events).

def test_classify_query_names_each_of_the_seven_service_prefixes():
    from symbiosis_brain.retrieval_log import SERVICE_QUERY_PREFIXES, classify_query
    assert SERVICE_QUERY_PREFIXES == (
        "<cross-session-message", "<task-notification>", "<agent-message",
        "<teammate-message", "<system-reminder>", "<local-command-",
        "[SYSTEM NOTIFICATION",
    )
    assert classify_query('<cross-session-message from="x">hi') == "service:cross-session"
    assert classify_query("<task-notification>\n<task-id>1</task-id>") == "service:task-notification"
    assert classify_query('<agent-message from="a">r') == "service:agent-message"
    assert classify_query("<teammate-message from=\"b\">r") == "service:teammate-message"
    assert classify_query("<system-reminder>x</system-reminder>") == "service:system-reminder"
    assert classify_query("<local-command-stdout>x") == "service:local-command"
    assert classify_query("[SYSTEM NOTIFICATION - NOT USER INPUT]\n<task-notification>") \
        == "service:system-notification"


def test_classify_query_calls_everything_else_human_and_matches_only_at_the_start():
    from symbiosis_brain.retrieval_log import classify_query
    assert classify_query("почини баг в роутинге") == "human"
    assert classify_query("  \n<cross-session-message from=\"x\">") == "service:cross-session"
    assert classify_query("a <cross-session-message> quoted mid-sentence") == "human"
    assert classify_query("") == "human"
    assert classify_query(None) == "human"


def test_query_kind_for_labels_prompt_sources_only():
    """`hook_prompt` and `legacy_gist` carry a prompt; every other source carries
    a tool input or an agent's own query, where 'human' would be a lie — those
    get 'n/a', so that NULL keeps exactly one meaning: written before the column."""
    from symbiosis_brain.retrieval_log import query_kind_for
    assert query_kind_for("hook_prompt", "<task-notification>x") == "service:task-notification"
    assert query_kind_for("hook_prompt", "что мы решили по TTL") == "human"
    assert query_kind_for("legacy_gist", "hello") == "human"
    assert query_kind_for("mcp_search", "<task-notification>x") == "n/a"
    assert query_kind_for("hook_pre_action", "wiki/alpha.md") == "n/a"
    assert query_kind_for("mcp_read", "wiki/alpha.md") == "n/a"


# The 16-column table exactly as HEAD 0581b9c created it — a log written before
# the column existed. The live base is such a table (2838 events on 04.09).
_V1_EVENT_DDL = """CREATE TABLE retrieval_event (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, session_id TEXT,
    origin TEXT NOT NULL DEFAULT 'unknown', source TEXT NOT NULL, tool TEXT,
    query TEXT NOT NULL DEFAULT '', scope TEXT, mode TEXT, fts_mode TEXT,
    n_returned INTEGER NOT NULL DEFAULT 0, dedup_dropped INTEGER NOT NULL DEFAULT 0,
    vec_enabled INTEGER NOT NULL DEFAULT 0, latency_ms INTEGER, e2e_ms INTEGER,
    client TEXT)"""
_V1_HIT_DDL = """CREATE TABLE retrieval_hit (event_id INTEGER NOT NULL, rank INTEGER NOT NULL,
    note_path TEXT NOT NULL, score REAL, in_both INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (event_id, rank))"""


def _v1_db(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), isolation_level=None)
    conn.execute(_V1_EVENT_DDL)
    conn.execute(_V1_HIT_DDL)
    conn.executemany(
        "INSERT INTO retrieval_event (ts, source, query) VALUES (?, ?, ?)",
        [
            ("2026-09-03T10:00:00+00:00", "hook_prompt", '<cross-session-message from="x">do'),
            ("2026-09-03T10:01:00+00:00", "hook_prompt", "почини баг"),
            ("2026-09-03T10:02:00+00:00", "mcp_search", "<task-notification>not a prompt source"),
            ("2026-09-03T10:03:00+00:00", "legacy_gist", "[SYSTEM NOTIFICATION - NOT USER INPUT]"),
        ],
    )
    return conn


def _kinds(conn) -> list:
    return [r[0] for r in conn.execute("SELECT query_kind FROM retrieval_event ORDER BY id")]


def test_upgrade_to_v2_adds_the_column_and_labels_every_old_row(tmp_path: Path):
    """Runs inside the CALLER's transaction (storage's migration wraps it in
    BEGIN IMMEDIATE); returns how many rows it labelled."""
    from symbiosis_brain.retrieval_log import upgrade_to_v2
    conn = _v1_db(tmp_path / "old.db")
    try:
        conn.execute("BEGIN IMMEDIATE")
        labelled = upgrade_to_v2(conn)
        conn.execute("COMMIT")
        cols = [r[1] for r in conn.execute("PRAGMA table_info(retrieval_event)")]
        assert len(cols) == 17 and cols[-1] == "query_kind"
        assert labelled == 4
        assert _kinds(conn) == [
            "service:cross-session", "human", "n/a", "service:system-notification",
        ]
    finally:
        conn.close()


def test_upgrade_to_v2_is_idempotent_and_touches_only_null_rows(tmp_path: Path):
    """A second run must not fail on the existing column, must label the rows an
    OLD process wrote after the column arrived (they come in as NULL), and must
    leave every already-labelled row alone."""
    from symbiosis_brain.retrieval_log import upgrade_to_v2
    conn = _v1_db(tmp_path / "old.db")
    try:
        conn.execute("BEGIN IMMEDIATE")
        upgrade_to_v2(conn)
        conn.execute("COMMIT")
        conn.execute(
            "INSERT INTO retrieval_event (ts, source, query) VALUES"
            " ('2026-09-04T00:00:00+00:00', 'hook_prompt', '<agent-message from=\"a\">r')"
        )
        conn.execute("UPDATE retrieval_event SET query_kind='human' WHERE id=1")
        conn.execute("BEGIN IMMEDIATE")
        labelled = upgrade_to_v2(conn)
        conn.execute("COMMIT")
        assert labelled == 1
        assert _kinds(conn) == [
            "human", "human", "n/a", "service:system-notification", "service:agent-message",
        ]
    finally:
        conn.close()


def test_storage_migration_upgrades_an_existing_v1_log_to_v2(db_path: Path):
    """The one schema authority is storage's `_migrate_retrieval_log`: below
    version 2 it calls upgrade_to_v2 and stamps 2. Emulated by winding a fresh
    db back to v1 (DROP COLUMN + version 1 + rows an old process left as NULL)
    and reopening."""
    Storage(db_path).close()
    conn = sqlite3.connect(str(db_path), isolation_level=None)
    try:
        conn.execute("ALTER TABLE retrieval_event DROP COLUMN query_kind")
        conn.execute("UPDATE schema_version SET version = 1 WHERE key = 'retrieval_log'")
        conn.executemany(
            "INSERT INTO retrieval_event (ts, source, query) VALUES (?, ?, ?)",
            [("2026-09-03T10:00:00+00:00", "hook_prompt", '<cross-session-message from="x">do'),
             ("2026-09-03T10:01:00+00:00", "mcp_search", "q")],
        )
    finally:
        conn.close()

    storage = Storage(db_path)              # reopen: the migration must run
    try:
        assert storage.get_schema_version("retrieval_log") == retrieval_log.RETRIEVAL_LOG_VERSION == 2
    finally:
        storage.close()
    cols = [r[1] for r in _rows(db_path, "PRAGMA table_info(retrieval_event)")]
    assert cols[-1] == "query_kind"
    kinds = [r[0] for r in _rows(db_path, "SELECT query_kind FROM retrieval_event ORDER BY id")]
    assert kinds == ["service:cross-session", "n/a"]


def test_rotate_labels_rows_an_old_writer_left_unlabelled(db_path: Path):
    """A writer older than the column — a `serve` not yet restarted, or a hook
    in the seconds between the two saves of the rollout — inserts without
    query_kind and leaves NULL behind. upgrade_to_v2 runs once, so those rows
    would stay NULL forever; the daily rotation labels them instead, and NULL
    never outlives a day of serve uptime."""
    Storage(db_path).close()
    conn = sqlite3.connect(str(db_path), isolation_level=None)
    try:
        conn.execute(
            "INSERT INTO retrieval_event (ts, source, query) VALUES"
            " ('2099-01-01T00:00:00+00:00', 'hook_prompt', '<task-notification>x')"
        )
    finally:
        conn.close()
    assert retrieval_log.rotate(db_path) == 0          # nothing is old enough to delete
    assert _rows(db_path, "SELECT query_kind FROM retrieval_event") == [("service:task-notification",)]


def test_record_writes_the_query_kind_of_a_service_prompt(db_path: Path):
    Storage(db_path).close()
    retrieval_log.record(
        _ctx(db_path, source="hook_prompt", client="hook", session_id="s1", origin="main"),
        query='<cross-session-message from="x">do the thing', scope=None, mode="gist",
        fts_mode="all", hits=[], latency_ms=1, vec_enabled=False,
    )
    assert _one_event(db_path)["query_kind"] == "service:cross-session"


def test_record_labels_a_human_prompt_and_a_server_search_differently(db_path: Path):
    Storage(db_path).close()
    for ctx in (_ctx(db_path, source="hook_prompt", client="hook"), _ctx(db_path)):
        retrieval_log.record(
            ctx, query="почини баг в роутинге", scope=None, mode="gist", fts_mode="all",
            hits=[], latency_ms=1, vec_enabled=False,
        )
    kinds = [r[0] for r in _rows(db_path, "SELECT query_kind FROM retrieval_event ORDER BY id")]
    assert kinds == ["human", "n/a"]


def test_record_writes_the_event_and_every_hit_row(db_path: Path):
    Storage(db_path).close()
    hits = [
        {"path": "wiki/alpha.md", "_score": 0.031, "_in_both": True},
        {"path": "patterns/beta.md", "_score": 0.017, "_in_both": False},
    ]
    retrieval_log.record(
        _ctx(db_path), query="alpha beta", scope="demo", mode="gist",
        fts_mode="all", hits=hits, latency_ms=7, vec_enabled=True,
        dedup_dropped=1,
    )
    ev = _one_event(db_path)
    assert ev["source"] == "mcp_search"
    assert ev["query"] == "alpha beta"
    assert ev["scope"] == "demo"
    assert ev["mode"] == "gist"
    assert ev["fts_mode"] == "all"
    assert ev["n_returned"] == 2
    assert ev["dedup_dropped"] == 1
    assert ev["vec_enabled"] == 1
    assert ev["latency_ms"] == 7
    assert ev["e2e_ms"] is None          # server path: no bash timestamp (§2.5)
    assert ev["session_id"] is None      # stdio MCP has no session id (§2.5)
    assert ev["origin"] == "unknown"     # no channel on the server side
    assert ev["client"] == "testclient/9.9.9"

    rows = _rows(
        db_path,
        "SELECT rank, note_path, score, in_both FROM retrieval_hit"
        " WHERE event_id=? ORDER BY rank",
        (ev["id"],),
    )
    assert [r[0] for r in rows] == [0, 1]
    assert [r[1] for r in rows] == ["wiki/alpha.md", "patterns/beta.md"]
    assert rows[0][2] == pytest.approx(0.031)
    assert [r[3] for r in rows] == [1, 0]


def test_record_caps_the_query_at_two_thousand_chars(db_path: Path):
    Storage(db_path).close()
    retrieval_log.record(
        _ctx(db_path), query="x" * 5000, scope=None, mode="gist",
        fts_mode="all", hits=[], latency_ms=1, vec_enabled=False,
    )
    ev = _one_event(db_path)
    assert len(ev["query"]) == retrieval_log.QUERY_LOG_MAX_CHARS == 2000


def test_record_read_writes_exactly_one_hit_row(db_path: Path):
    Storage(db_path).close()
    retrieval_log.record_read(
        _ctx(db_path, source="mcp_read"), note_path="wiki/alpha.md", latency_ms=2,
    )
    ev = _one_event(db_path)
    assert ev["mode"] == "read"
    assert ev["query"] == "wiki/alpha.md"     # I-4 table: query = the path
    assert ev["n_returned"] == 1
    assert ev["vec_enabled"] == 0             # nothing vectorial was asked
    assert ev["fts_mode"] is None
    rows = _rows(db_path, "SELECT rank, note_path, score, in_both FROM retrieval_hit")
    assert rows == [(0, "wiki/alpha.md", None, 0)]


def test_record_context_writes_no_hit_rows(db_path: Path):
    Storage(db_path).close()
    retrieval_log.record_context(
        _ctx(db_path, source="mcp_context"), entity="Alpha", n_returned=4, latency_ms=3,
    )
    ev = _one_event(db_path)
    assert ev["mode"] == "context"
    assert ev["query"] == "Alpha"
    assert ev["n_returned"] == 4
    assert ev["vec_enabled"] == 0
    assert _rows(db_path, "SELECT COUNT(*) FROM retrieval_hit") == [(0,)]


def test_env_switch_off_writes_nothing(db_path: Path, monkeypatch):
    Storage(db_path).close()
    monkeypatch.setenv("SYMBIOSIS_BRAIN_RETRIEVAL_LOG", "off")
    retrieval_log._env_enabled = None          # the value is cached per process
    retrieval_log.record(
        _ctx(db_path), query="q", scope=None, mode="gist", fts_mode="all",
        hits=[{"path": "wiki/a.md", "_score": 1.0, "_in_both": False}],
        latency_ms=1, vec_enabled=False,
    )
    assert _rows(db_path, "SELECT COUNT(*) FROM retrieval_event") == [(0,)]
    assert not retrieval_log.is_enabled()


def test_any_other_value_of_the_env_switch_means_on(db_path: Path, monkeypatch):
    monkeypatch.setenv("SYMBIOSIS_BRAIN_RETRIEVAL_LOG", "yes-please")
    retrieval_log._env_enabled = None
    assert retrieval_log.is_enabled() is True


def test_structural_error_disables_the_writer_and_counts_the_skip(tmp_path: Path, caplog):
    """A db path that is a DIRECTORY: sqlite cannot open it, and it will not
    open next time either — that is the structural case, §2.4 п. 4."""
    broken = tmp_path / "brain.db"
    broken.mkdir()
    ctx = _ctx(broken)
    with caplog.at_level("WARNING", logger="symbiosis-brain.retrieval_log"):
        retrieval_log.record(
            ctx, query="q", scope=None, mode="gist", fts_mode="all", hits=[],
            latency_ms=1, vec_enabled=False,
        )
        retrieval_log.record(
            ctx, query="q2", scope=None, mode="gist", fts_mode="all", hits=[],
            latency_ms=1, vec_enabled=False,
        )
    assert retrieval_log._disabled is True
    assert retrieval_log.skipped_count() == 1      # the second call is a no-op
    warnings = [r for r in caplog.records
                if r.levelname == "WARNING" and r.name == "symbiosis-brain.retrieval_log"]
    assert len(warnings) == 1                      # exactly one warning, ever


def test_busy_lock_costs_one_retry_a_skip_and_no_self_disable(db_path: Path, monkeypatch):
    """SQLITE_BUSY is transient, so it must NOT disable the writer (§2.4 п. 4).
    busy_timeout is shrunk to 50 ms so the test costs ~0.1 s instead of 4 s."""
    Storage(db_path).close()
    monkeypatch.setattr(retrieval_log, "_BUSY_TIMEOUT_MS", 50)
    holder = sqlite3.connect(str(db_path), isolation_level=None)
    holder.execute("PRAGMA busy_timeout=5000")
    holder.execute("BEGIN IMMEDIATE")
    holder.execute(
        "INSERT OR REPLACE INTO schema_version (key, version) VALUES ('probe', 1)"
    )
    try:
        retrieval_log.record(
            _ctx(db_path), query="blocked", scope=None, mode="gist",
            fts_mode="all", hits=[], latency_ms=1, vec_enabled=False,
        )
    finally:
        holder.execute("COMMIT")
        holder.close()
    assert retrieval_log._disabled is False        # transient, not structural
    assert retrieval_log.skipped_count() == 1
    assert _rows(db_path, "SELECT COUNT(*) FROM retrieval_event") == [(0,)]


def test_pending_skips_ride_along_with_the_next_successful_insert(db_path: Path):
    """Flush point (а): no extra transaction, no extra lock (§2.4 п. 4)."""
    Storage(db_path).close()
    retrieval_log._skipped = 3
    retrieval_log._skipped_pending = 3
    retrieval_log.record(
        _ctx(db_path), query="ok", scope=None, mode="gist", fts_mode="all",
        hits=[], latency_ms=1, vec_enabled=False,
    )
    storage = Storage(db_path)
    try:
        assert storage.get_schema_version(retrieval_log.SKIPPED_TOTAL_KEY) == 3
    finally:
        storage.close()
    assert retrieval_log._skipped_pending == 0
    assert _rows(db_path, "SELECT COUNT(*) FROM retrieval_event") == [(1,)]


def test_close_flushes_the_pending_delta(db_path: Path):
    """Flush point (в): the hook entry points call close() in `finally`."""
    Storage(db_path).close()
    retrieval_log.record(
        _ctx(db_path), query="warm the connection", scope=None, mode="gist",
        fts_mode="all", hits=[], latency_ms=1, vec_enabled=False,
    )
    retrieval_log._skipped = 2
    retrieval_log._skipped_pending = 2
    retrieval_log.close()
    storage = Storage(db_path)
    try:
        assert storage.get_schema_version(retrieval_log.SKIPPED_TOTAL_KEY) == 2
    finally:
        storage.close()


def test_persistent_skip_counter_is_readable_from_another_process(db_path: Path):
    """The whole point of persisting it: `brain-cli report` is a DIFFERENT
    process whose own skipped_count() is structurally 0 (I-4, I-26)."""
    Storage(db_path).close()
    retrieval_log._skipped = 5
    retrieval_log._skipped_pending = 5
    retrieval_log.record(
        _ctx(db_path), query="ok", scope=None, mode="gist", fts_mode="all",
        hits=[], latency_ms=1, vec_enabled=False,
    )
    retrieval_log.close()
    script = (
        "import sys;"
        "from pathlib import Path;"
        "from symbiosis_brain.storage import Storage;"
        "s = Storage(Path(sys.argv[1]));"
        "print(s.get_schema_version('retrieval_log_skipped_total'));"
        "s.close()"
    )
    proc = subprocess.run(
        # -X utf8 pins the CHILD's encoder. Passing encoding= alone fixes only
        # the parent's decoder, and this child is not our CLI — it has no
        # stdout.reconfigure, so without -X utf8 it writes in the machine's
        # locale and the pair only agrees by luck on ASCII output.
        [sys.executable, "-X", "utf8", "-c", script, str(db_path)],
        capture_output=True, text=True, timeout=120, encoding="utf-8")
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "5"


# ---------- Task 2.6: rotation (I-11) ----------

def _insert_event_with_ts(db: Path, ts: str, path: str) -> None:
    conn = sqlite3.connect(str(db), isolation_level=None)
    try:
        eid = conn.execute(
            "INSERT INTO retrieval_event (ts, source, query) VALUES (?, 'mcp_search', 'q')",
            (ts,),
        ).lastrowid
        conn.execute(
            "INSERT INTO retrieval_hit (event_id, rank, note_path) VALUES (?, 0, ?)",
            (eid, path),
        )
    finally:
        conn.close()


def test_rotate_deletes_by_age_and_keeps_fresh_events(db_path: Path):
    from datetime import datetime, timedelta, timezone
    Storage(db_path).close()
    old = (datetime.now(timezone.utc) - timedelta(days=120)).isoformat()
    fresh = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
    _insert_event_with_ts(db_path, old, "wiki/old.md")
    _insert_event_with_ts(db_path, fresh, "wiki/fresh.md")

    deleted = retrieval_log.rotate(db_path, days=90)

    assert deleted == 1
    assert _rows(db_path, "SELECT COUNT(*) FROM retrieval_event") == [(1,)]
    assert _rows(db_path, "SELECT note_path FROM retrieval_hit") == [("wiki/fresh.md",)]


def test_rotate_never_raises_and_returns_zero_on_a_missing_db(tmp_path: Path):
    assert retrieval_log.rotate(tmp_path / "nope" / "brain.db", days=90) == 0


def test_rotate_uses_its_own_connection_and_closes_it(db_path: Path):
    """I-11: rotate opens and closes its own connection, so it must not leave
    the module-level writer connection behind — and must not leave ITS OWN
    handle open either. The unlink() is what turns "closes it" from prose into
    a test: on Windows an open sqlite handle makes it raise PermissionError."""
    Storage(db_path).close()
    retrieval_log.rotate(db_path, days=90)
    assert retrieval_log._conn is None
    db_path.unlink()                # PermissionError, если ручка утекла
    assert not db_path.exists()


# ---------- Task 2.6: the daily gate lives in the CALLER (I-11) ----------

@pytest.fixture
def clean_server():
    import symbiosis_brain.server as server_mod
    yield server_mod
    if server_mod._storage is not None:
        server_mod._storage.close()
    for attr in ("_storage", "_search", "_sync", "_graph", "_temporal",
                 "_vault_path", "_linter"):
        setattr(server_mod, attr, None)


def test_init_rotates_the_log_at_most_once_a_day(tmp_vault: Path, clean_server, monkeypatch):
    from datetime import datetime, timezone
    calls = []

    def counting_rotate(db_path, *, days=90):
        calls.append((Path(db_path), days))
        return 0

    monkeypatch.setattr(retrieval_log, "rotate", counting_rotate)

    clean_server._init(tmp_vault)
    assert len(calls) == 1
    assert calls[0][0] == tmp_vault / ".index" / "brain.db"
    assert calls[0][1] == retrieval_log.RETENTION_DAYS
    today = datetime.now(timezone.utc).date().isoformat()
    assert clean_server._storage.get_schema_version("retrieval_log_rotated_at") == today

    clean_server._storage.close()
    for attr in ("_storage", "_search", "_sync", "_graph", "_temporal",
                 "_vault_path", "_linter"):
        setattr(clean_server, attr, None)

    clean_server._init(tmp_vault)
    assert len(calls) == 1          # same day → the gate holds


def test_init_survives_a_throwing_rotation(tmp_vault: Path, clean_server, monkeypatch):
    """try/except is on the CALL SITE for a reason: an exception out of _init
    is swallowed by _background_init (server.py:976-984) and would silently
    skip the rest of the vec-index maintenance (§2.6)."""
    def boom(db_path, *, days=90):
        raise RuntimeError("rotation exploded")

    monkeypatch.setattr(retrieval_log, "rotate", boom)
    clean_server._init(tmp_vault)                 # must not raise
    assert clean_server._storage is not None


# ---------- Task 2.10: provenance slice 1 (I-14, §3.3) ----------

def test_client_id_reads_name_and_version_from_client_info():
    class _Info:
        name = "testclient"
        version = "9.9.9"

    class _Params:
        clientInfo = _Info()

    class _Session:
        client_params = _Params()

    class _Ctx:
        session = _Session()

    class _App:
        request_context = _Ctx()

    from symbiosis_brain.provenance import client_id
    assert client_id(_App()) == "testclient/9.9.9"


def test_client_id_is_unknown_outside_a_request_context():
    """`Server.request_context` raises LookupError outside a request
    (mcp/server/lowlevel/server.py:240-244) — a self-reported label must never
    become a crash (§3.3)."""
    class _App:
        @property
        def request_context(self):
            raise LookupError("outside of a request")

    from symbiosis_brain.provenance import client_id
    assert client_id(_App()) == "unknown/unknown"


def test_client_id_is_unknown_when_client_params_is_none():
    class _Session:
        client_params = None

    class _Ctx:
        session = _Session()

    class _App:
        request_context = _Ctx()

    from symbiosis_brain.provenance import client_id
    assert client_id(_App()) == "unknown/unknown"


# ---------- Task 2.8 / 2.9: write points (I-7, §2.1) ----------

def _server_with_two_notes(server_mod, tmp_vault: Path):
    """Wire the module globals by hand instead of calling _init: the write
    points are what we test here, not the startup path."""
    import asyncio

    from symbiosis_brain.graph import GraphTraverser
    from symbiosis_brain.search import SearchEngine
    from symbiosis_brain.storage import Storage
    from symbiosis_brain.sync import VaultSync
    from symbiosis_brain.temporal import TemporalManager

    (tmp_vault / "wiki" / "alpha.md").write_text(
        "---\ntitle: Alpha\ntype: wiki\nscope: global\ngist: alpha gist\ntags: []\n---\n\n"
        "Alpha body about widgets.\n",
        encoding="utf-8",
    )
    (tmp_vault / "wiki" / "beta.md").write_text(
        "---\ntitle: Beta\ntype: wiki\nscope: global\ngist: beta gist\ntags: []\n---\n\n"
        "Beta body about widgets.\n",
        encoding="utf-8",
    )
    server_mod._storage = Storage(tmp_vault / ".index" / "brain.db")
    server_mod._search = SearchEngine(server_mod._storage)
    server_mod._sync = VaultSync(tmp_vault, server_mod._storage)
    server_mod._graph = GraphTraverser(server_mod._storage)
    server_mod._temporal = TemporalManager(server_mod._storage)
    server_mod._vault_path = tmp_vault
    server_mod._linter = None
    server_mod._ready = asyncio.Event()
    server_mod._ready.set()
    server_mod._sync.sync_all()
    return server_mod._storage.db_path


async def test_brain_search_logs_one_mcp_search_event(tmp_vault: Path, clean_server):
    db = _server_with_two_notes(clean_server, tmp_vault)
    await clean_server.call_tool("brain_search", {"query": "widgets", "mode": "gist"})
    ev = _one_event(db)
    assert ev["source"] == "mcp_search"
    assert ev["query"] == "widgets"
    assert ev["mode"] == "gist"
    # §2.9 — закон: в журнал уезжает ЭФФЕКТИВНЫЙ режим, то есть 'any' | 'all' |
    # 'fallback_any', и НИКОГДА 'all_then_any'. Точное значение на этом пути
    # меняет CP-1 (сегодня 'all' по Р1, после CP-1 — 'any' на серверном вызове),
    # и пинят его тесты CP-1 (cp-01 §3, ШАГ 1). Здесь утверждение смысловое,
    # чтобы законный сдвиг лексики не красил чужой чекпоинт (00-plan §0.8:
    # правок существующих тестов ровно четыре, и эта в их число не входит).
    # Литералы, а не константы `FTS_MODE_*`: их заводит CP-1, а CP-2 идёт до него.
    assert ev["fts_mode"] in ("any", "all", "fallback_any")
    assert ev["session_id"] is None
    assert ev["origin"] == "unknown"
    assert ev["client"] == "unknown/unknown"  # no MCP request context in tests
    assert ev["n_returned"] >= 1
    assert ev["latency_ms"] is not None
    hits = _rows(db, "SELECT rank, note_path FROM retrieval_hit ORDER BY rank")
    assert len(hits) == ev["n_returned"]


async def test_brain_read_logs_exactly_one_hit(tmp_vault: Path, clean_server):
    db = _server_with_two_notes(clean_server, tmp_vault)
    await clean_server.call_tool("brain_read", {"path": "wiki/alpha.md"})
    ev = _one_event(db)
    assert ev["source"] == "mcp_read"
    assert ev["mode"] == "read"
    assert ev["n_returned"] == 1
    assert _rows(db, "SELECT note_path FROM retrieval_hit") == [("wiki/alpha.md",)]


async def test_brain_read_miss_is_logged_with_zero_returned(tmp_vault: Path, clean_server):
    """The early exit (server.py:555-556) is exactly the signal the log exists
    for: 'searched and found nothing' must not look like 'did not search'."""
    db = _server_with_two_notes(clean_server, tmp_vault)
    out = await clean_server.call_tool("brain_read", {"path": "wiki/nope.md"})
    assert "not found" in out[0].text.lower()
    ev = _one_event(db)
    assert ev["source"] == "mcp_read"
    assert ev["mode"] == "read"
    assert ev["query"] == "wiki/nope.md"
    assert ev["n_returned"] == 0
    assert _rows(db, "SELECT COUNT(*) FROM retrieval_hit") == [(0,)]


async def test_brain_context_logs_without_any_hit_rows(tmp_vault: Path, clean_server):
    db = _server_with_two_notes(clean_server, tmp_vault)
    storage = clean_server._storage
    for n in ("Alpha", "Beta"):
        storage.upsert_entity(n, "concept")
    storage.upsert_relation("Alpha", "Beta", "uses")
    await clean_server.call_tool("brain_context", {"entity": "Alpha", "depth": 1})
    ev = _one_event(db)
    assert ev["source"] == "mcp_context"
    assert ev["mode"] == "context"
    assert ev["query"] == "Alpha"
    assert ev["n_returned"] >= 1
    assert _rows(db, "SELECT COUNT(*) FROM retrieval_hit") == [(0,)]


async def test_brain_context_unknown_entity_logs_zero(tmp_vault: Path, clean_server):
    db = _server_with_two_notes(clean_server, tmp_vault)
    await clean_server.call_tool("brain_context", {"entity": "NoSuchThing"})
    ev = _one_event(db)
    assert ev["source"] == "mcp_context"
    assert ev["n_returned"] == 0


async def test_brain_list_is_never_logged(tmp_vault: Path, clean_server):
    """§2.1: brain_list hands over the whole vault in one list — logging it
    would bury every real retrieval under 1466 rows."""
    db = _server_with_two_notes(clean_server, tmp_vault)
    await clean_server.call_tool("brain_list", {})
    assert _rows(db, "SELECT COUNT(*) FROM retrieval_event") == [(0,)]


def test_search_fills_stats_with_exactly_two_keys(tmp_vault: Path, clean_server):
    """I-7: `stats` carries fts_mode and vec_enabled and NOTHING else — a third
    key is a private channel the spec refuses to define."""
    _server_with_two_notes(clean_server, tmp_vault)
    stats: dict = {}
    clean_server._search.search(query="widgets", limit=3, mode="gist", stats=stats)
    assert set(stats) == {"fts_mode", "vec_enabled"}
    # То же, что и выше: значение — эффективный режим (§2.9), точную букву пинит
    # CP-1. Литералы вместо `FTS_MODE_*` — константы появятся только в CP-1.
    assert stats["fts_mode"] in ("any", "all", "fallback_any")
    assert isinstance(stats["vec_enabled"], bool)


def test_search_fills_stats_even_on_an_empty_result(tmp_vault: Path, clean_server):
    _server_with_two_notes(clean_server, tmp_vault)
    stats: dict = {}
    hits = clean_server._search.search(query="zzzznothingmatches", limit=3,
                                       mode="gist", stats=stats)
    assert hits == []
    assert set(stats) == {"fts_mode", "vec_enabled"}


def test_search_without_log_ctx_writes_nothing(tmp_vault: Path, clean_server):
    """The default keeps today's behaviour: nobody logs unless asked (I-7)."""
    db = _server_with_two_notes(clean_server, tmp_vault)
    clean_server._search.search(query="widgets", limit=3, mode="gist")
    assert _rows(db, "SELECT COUNT(*) FROM retrieval_event") == [(0,)]


async def test_search_still_returns_hits_when_the_log_is_broken(
        tmp_vault: Path, clean_server, monkeypatch):
    """Fail-open, principle 1 §1.3: a telemetry error costs a log line, never a
    result. The writer is pointed at a path it cannot open."""
    _server_with_two_notes(clean_server, tmp_vault)
    broken = tmp_vault / "not-a-db"
    broken.mkdir()
    monkeypatch.setattr(clean_server._storage, "db_path", broken, raising=False)
    out = await clean_server.call_tool("brain_search", {"query": "widgets", "mode": "gist"})
    assert "wiki/alpha.md" in out[0].text or "wiki/beta.md" in out[0].text
    assert retrieval_log._disabled is True
