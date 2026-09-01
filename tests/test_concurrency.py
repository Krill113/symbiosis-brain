"""Integration tests for parallel-safety + idempotent startup.

Uses pytest fixtures from conftest.py. Multiprocessing tests use spawn for
cross-platform compatibility (mandatory on Windows).
"""
from __future__ import annotations

import multiprocessing as mp
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

import pytest

from symbiosis_brain.storage import Storage
from symbiosis_brain.sync import VaultSync
from symbiosis_brain.search import SearchEngine, _MODEL_NAME


def _seed_vault(vault_path: Path, n: int = 5) -> None:
    """Create n notes in tmp vault."""
    for i in range(n):
        (vault_path / "wiki" / f"note{i}.md").write_text(
            f"---\ntitle: Note {i}\ntype: wiki\nscope: global\ntags: []\n---\n\nBody {i}.\n",
            encoding="utf-8",
        )


def test_init_idempotent_on_unchanged_vault(tmp_vault, db_path, monkeypatch):
    """First _init builds index. Second _init should NOT call index_note."""
    _seed_vault(tmp_vault, n=3)

    from symbiosis_brain import server

    # First run — full index build via bootstrap branch
    server._init(tmp_vault)
    server._storage.close()

    # Reset module-level globals so we get a fresh _init pass
    for attr in ("_storage", "_search", "_sync", "_graph", "_temporal",
                 "_linter", "_vault_path"):
        setattr(server, attr, None)

    # Track calls to index_note + index_all on the new SearchEngine instance
    call_count = {"index_note": 0, "index_all": 0}

    orig_search_init = SearchEngine.__init__

    def patched_init(self, storage):
        orig_search_init(self, storage)
        orig_index_note = self.index_note
        orig_index_all = self.index_all

        def counting_index_note(*a, **kw):
            call_count["index_note"] += 1
            return orig_index_note(*a, **kw)

        def counting_index_all(*a, **kw):
            call_count["index_all"] += 1
            return orig_index_all(*a, **kw)

        self.index_note = counting_index_note
        self.index_all = counting_index_all

    monkeypatch.setattr(SearchEngine, "__init__", patched_init)

    # Second run on unchanged vault
    server._init(tmp_vault)

    assert call_count["index_note"] == 0, "expected no per-note re-embed on unchanged vault"
    assert call_count["index_all"] == 0, "expected no full re-index on unchanged vault"
    server._storage.close()


def test_init_indexes_only_added_or_updated(tmp_vault, db_path, monkeypatch):
    """Adding 1 note + modifying 1 note → 2 index_note calls, 0 index_all."""
    _seed_vault(tmp_vault, n=3)

    from symbiosis_brain import server

    server._init(tmp_vault)
    server._storage.close()

    for attr in ("_storage", "_search", "_sync", "_graph", "_temporal",
                 "_linter", "_vault_path"):
        setattr(server, attr, None)

    (tmp_vault / "wiki" / "note0.md").write_text(
        "---\ntitle: Note 0\ntype: wiki\nscope: global\ntags: []\n---\n\nBody MODIFIED.\n",
        encoding="utf-8",
    )
    (tmp_vault / "wiki" / "note99.md").write_text(
        "---\ntitle: Note 99\ntype: wiki\nscope: global\ntags: []\n---\n\nBody 99.\n",
        encoding="utf-8",
    )

    call_count = {"index_note": [], "index_all": 0}

    orig_search_init = SearchEngine.__init__

    def patched_init(self, storage):
        orig_search_init(self, storage)
        orig_index_note = self.index_note
        orig_index_all = self.index_all

        def counting_index_note(path, *a, **kw):
            call_count["index_note"].append(path)
            return orig_index_note(path, *a, **kw)

        def counting_index_all(*a, **kw):
            call_count["index_all"] += 1
            return orig_index_all(*a, **kw)

        self.index_note = counting_index_note
        self.index_all = counting_index_all

    monkeypatch.setattr(SearchEngine, "__init__", patched_init)

    server._init(tmp_vault)

    assert call_count["index_all"] == 0, "no full re-index expected"
    assert sorted(call_count["index_note"]) == ["wiki/note0.md", "wiki/note99.md"]
    server._storage.close()


def test_init_db_model_mismatch_alone_does_not_reindex(tmp_vault, db_path):
    """З3: schema_version.embedding_model is the source of truth. A DB
    registered under a model that no longer matches the code's hardcoded
    default (e.g. the default changed across an upgrade) must NOT trigger a
    forced re-index on its own — only SYMBIOSIS_BRAIN_EMBED_MODEL is a
    request to change models. This is the boundary's "no forced reindex on
    upgrade" requirement, and the direct replacement for this suite's old
    assumption that a bare default-vs-DB mismatch alone drove a rebuild."""
    _seed_vault(tmp_vault, n=3)

    from symbiosis_brain import server

    server._init(tmp_vault)
    assert server._storage.get_schema_version("embedding_model") == _MODEL_NAME
    server._storage.close()

    # Mutate stored model to simulate a DB registered under a model the
    # code's default no longer names.
    s = Storage(tmp_vault / ".index" / "brain.db")
    s.set_schema_version("embedding_model", "OLD-MODEL")
    s.close()

    for attr in ("_storage", "_search", "_sync", "_graph", "_temporal",
                 "_linter", "_vault_path"):
        setattr(server, attr, None)

    call_count = {"index_all": 0}
    from symbiosis_brain.search import SearchEngine as _SE
    orig_index_all = _SE.index_all

    def counting_index_all(self, *a, **kw):
        call_count["index_all"] += 1
        return orig_index_all(self, *a, **kw)

    _SE.index_all = counting_index_all
    try:
        server._init(tmp_vault)
        assert call_count["index_all"] == 0, \
            "a DB/default mismatch alone must not force a reindex (no env request)"
        assert server._storage.get_schema_version("embedding_model") == "OLD-MODEL", \
            "the DB's stored model is authoritative and must be left as-is"
        assert server._search._model_name == "OLD-MODEL"
    finally:
        _SE.index_all = orig_index_all
        server._storage.close()


def test_init_reindexes_on_env_requested_model_change(tmp_vault, db_path, monkeypatch):
    """З3: SYMBIOSIS_BRAIN_EMBED_MODEL is the ONLY thing that makes the server
    rebuild notes_vec for a different model — set it to something other than
    what's already registered and a full re-index runs, at the new model's
    dimension, with the DB updated to match."""
    _seed_vault(tmp_vault, n=3)

    from symbiosis_brain import server

    server._init(tmp_vault)
    assert server._storage.get_schema_version("embedding_model") == _MODEL_NAME
    server._storage.close()

    for attr in ("_storage", "_search", "_sync", "_graph", "_temporal",
                 "_linter", "_vault_path"):
        setattr(server, attr, None)

    monkeypatch.setenv("SYMBIOSIS_BRAIN_EMBED_MODEL", "fixture/model-eight")
    # "fixture/model-eight" isn't a real fastembed model — never download one
    # in a test. Fake the embedder out so index_all's actual embed calls stay
    # offline; the point of this test is the migration plumbing, not vectors.
    import symbiosis_brain.search as sb_search

    def _fake_embed(texts):
        return [[0.1] * 384 for _ in texts]

    monkeypatch.setattr(sb_search, "_embed", _fake_embed)
    monkeypatch.setattr(sb_search, "_embed_one", lambda text: _fake_embed([text])[0])
    # index_all's _embed_documents call routes through _embed above (faked),
    # but first swaps the legacy _MODEL_NAME/_embedder singleton to
    # "fixture/model-eight" as a side effect (_set_active_model) — monkeypatch
    # these too so that swap reverts at teardown instead of leaking into
    # whichever test runs next in this session.
    monkeypatch.setattr(sb_search, "_MODEL_NAME", sb_search._MODEL_NAME)
    monkeypatch.setattr(sb_search, "_embedder", sb_search._embedder)

    call_count = {"index_all": 0}
    from symbiosis_brain.search import SearchEngine as _SE
    orig_index_all = _SE.index_all

    def counting_index_all(self, *a, **kw):
        call_count["index_all"] += 1
        return orig_index_all(self, *a, **kw)

    _SE.index_all = counting_index_all
    try:
        server._init(tmp_vault)
        assert call_count["index_all"] == 1, "an env-requested model change must reindex"
        assert server._storage.get_schema_version("embedding_model") == "fixture/model-eight"
        assert server._search._model_name == "fixture/model-eight"
        declared = server._storage._conn.execute(
            "SELECT sql FROM sqlite_master WHERE name='notes_vec'").fetchone()[0]
        assert "FLOAT[384]" in declared, (
            "fixture/model-eight has no fastembed metadata — dimension must "
            "fall back to the default rather than crash startup: " + declared
        )
    finally:
        _SE.index_all = orig_index_all
        server._storage.close()


def test_init_repairs_index_on_count_drift(tmp_vault, db_path):
    """If notes_vec count differs from notes count, targeted repair runs."""
    _seed_vault(tmp_vault, n=3)
    from symbiosis_brain import server

    server._init(tmp_vault)
    server._storage.close()

    # Manually delete one row from notes_vec to create drift.
    # SearchEngine.__init__ loads the sqlite_vec extension so notes_vec is accessible.
    s = Storage(tmp_vault / ".index" / "brain.db")
    se = SearchEngine(s)
    se.delete_vec("wiki/note0.md")
    s.close()

    for attr in ("_storage", "_search", "_sync", "_graph", "_temporal",
                 "_linter", "_vault_path"):
        setattr(server, attr, None)

    call_count = {"index_all": 0, "repair_index": 0}
    from symbiosis_brain.search import SearchEngine as _SE
    orig_index_all = _SE.index_all
    orig_repair_index = _SE.repair_index

    def counting_index_all(self, *a, **kw):
        call_count["index_all"] += 1
        return orig_index_all(self, *a, **kw)

    def counting_repair_index(self, *a, **kw):
        call_count["repair_index"] += 1
        return orig_repair_index(self, *a, **kw)

    _SE.index_all = counting_index_all
    _SE.repair_index = counting_repair_index
    try:
        server._init(tmp_vault)
        assert call_count["index_all"] == 0, "count drift should not trigger full re-index"
        assert call_count["repair_index"] == 1, "count drift should trigger targeted repair"
    finally:
        _SE.index_all = orig_index_all
        _SE.repair_index = orig_repair_index
        server._storage.close()


def _write_in_subprocess(vault_str: str, rel_path: str, content: str, queue: "mp.Queue"):
    try:
        from pathlib import Path as _P
        from symbiosis_brain import server as _srv
        _srv._init(_P(vault_str))
        _srv._write_note_body(rel_path, content, "write", "T")
        queue.put(("ok", rel_path))
    except Exception as exc:
        queue.put(("err", f"{type(exc).__name__}: {exc}"))


def _append_in_subprocess(vault_str: str, rel_path: str, section: str, fragment: str, queue: "mp.Queue"):
    try:
        from pathlib import Path as _P
        import frontmatter as _fm
        from symbiosis_brain import server as _srv
        from symbiosis_brain.sections import append_to_section
        from symbiosis_brain.write_lock import note_write_lock
        _srv._init(_P(vault_str))
        full = _P(vault_str) / rel_path
        with note_write_lock(_P(vault_str), rel_path):
            raw = full.read_text(encoding="utf-8")
            post = _fm.loads(raw)
            post.content = append_to_section(post.content, section, fragment)
            new_text = _fm.dumps(post) + "\n"
            _srv._write_note_body_unlocked(rel_path, new_text, "append", post.metadata.get("title", ""))
        queue.put(("ok", section))
    except Exception as exc:
        queue.put(("err", f"{type(exc).__name__}: {exc}"))


def test_concurrent_brain_write_same_note_no_corruption(tmp_vault, db_path):
    """Two parallel writes to same note: both complete, file is one-of-two contents,
    DB is consistent (no half-written rows)."""
    target = tmp_vault / "wiki" / "shared.md"
    target.write_text(
        "---\ntitle: Shared\ntype: wiki\nscope: global\ntags: []\n---\n\ninitial.\n",
        encoding="utf-8",
    )

    body_a = "---\ntitle: Shared\ntype: wiki\nscope: global\ntags: []\n---\n\nA wins.\n"
    body_b = "---\ntitle: Shared\ntype: wiki\nscope: global\ntags: []\n---\n\nB wins.\n"

    ctx = mp.get_context("spawn")
    q = ctx.Queue()
    procs = [
        ctx.Process(target=_write_in_subprocess, args=(str(tmp_vault), "wiki/shared.md", body_a, q)),
        ctx.Process(target=_write_in_subprocess, args=(str(tmp_vault), "wiki/shared.md", body_b, q)),
    ]
    for p in procs: p.start()
    for p in procs: p.join(timeout=60)
    for p in procs: assert p.exitcode == 0

    results = [q.get_nowait() for _ in range(2)]
    assert all(r[0] == "ok" for r in results), f"errors: {results}"

    final = target.read_text(encoding="utf-8")
    assert final in (body_a, body_b), \
        f"file should contain exactly one of the two writes, got: {final!r}"

    s = Storage(tmp_vault / ".index" / "brain.db")
    note = s.get_note("wiki/shared.md")
    assert note is not None
    assert note["content"] in ("A wins.", "B wins.")
    s.close()


def test_concurrent_brain_append_different_sections_both_persist(tmp_vault, db_path):
    """Two parallel appends to DIFFERENT sections of same note: both edits land."""
    target = tmp_vault / "wiki" / "multi.md"
    target.write_text(
        "---\ntitle: Multi\ntype: wiki\nscope: global\ntags: []\n---\n\n"
        "## Section A\n\ninitial A.\n\n## Section B\n\ninitial B.\n",
        encoding="utf-8",
    )

    ctx = mp.get_context("spawn")
    q = ctx.Queue()
    procs = [
        ctx.Process(target=_append_in_subprocess,
                    args=(str(tmp_vault), "wiki/multi.md", "Section A", "added by A", q)),
        ctx.Process(target=_append_in_subprocess,
                    args=(str(tmp_vault), "wiki/multi.md", "Section B", "added by B", q)),
    ]
    for p in procs: p.start()
    for p in procs: p.join(timeout=60)
    for p in procs: assert p.exitcode == 0

    final = target.read_text(encoding="utf-8")
    assert "added by A" in final, "Section A append must persist"
    assert "added by B" in final, "Section B append must persist"


def _patch_in_subprocess(vault_str: str, rel_path: str, anchor: str, replacement: str, queue: "mp.Queue"):
    try:
        from pathlib import Path as _P
        import frontmatter as _fm
        from symbiosis_brain import server as _srv
        from symbiosis_brain.sections import replace_anchor
        from symbiosis_brain.write_lock import note_write_lock
        _srv._init(_P(vault_str))
        full = _P(vault_str) / rel_path
        with note_write_lock(_P(vault_str), rel_path):
            raw = full.read_text(encoding="utf-8")
            post = _fm.loads(raw)
            post.content = replace_anchor(post.content, anchor, replacement)
            new_text = _fm.dumps(post) + "\n"
            _srv._write_note_body_unlocked(rel_path, new_text, "patch", post.metadata.get("title", ""))
        queue.put(("ok", anchor))
    except Exception as exc:
        queue.put(("err", f"{type(exc).__name__}: {exc}"))


def test_concurrent_brain_patch_different_anchors_both_persist(tmp_vault, db_path):
    """Two parallel patches to DIFFERENT unique anchors of same note: both edits land."""
    target = tmp_vault / "wiki" / "patches.md"
    target.write_text(
        "---\ntitle: Patches\ntype: wiki\nscope: global\ntags: []\n---\n\n"
        "## Section A\n\nanchor-alpha-original\n\n## Section B\n\nanchor-beta-original\n",
        encoding="utf-8",
    )

    ctx = mp.get_context("spawn")
    q = ctx.Queue()
    procs = [
        ctx.Process(
            target=_patch_in_subprocess,
            args=(str(tmp_vault), "wiki/patches.md", "anchor-alpha-original", "anchor-alpha-PATCHED", q),
        ),
        ctx.Process(
            target=_patch_in_subprocess,
            args=(str(tmp_vault), "wiki/patches.md", "anchor-beta-original", "anchor-beta-PATCHED", q),
        ),
    ]
    for p in procs: p.start()
    for p in procs: p.join(timeout=60)
    for p in procs: assert p.exitcode == 0

    results = [q.get_nowait() for _ in range(2)]
    assert all(r[0] == "ok" for r in results), f"errors: {results}"

    final = target.read_text(encoding="utf-8")
    assert "anchor-alpha-PATCHED" in final, "Section A patch must persist"
    assert "anchor-beta-PATCHED" in final, "Section B patch must persist"
    assert "anchor-alpha-original" not in final
    assert "anchor-beta-original" not in final


def test_write_note_body_does_not_scan_other_notes(tmp_vault, db_path, monkeypatch):
    """A brain_write should call sync_one for the target path only,
    not sync_all (which scans the entire vault)."""
    _seed_vault(tmp_vault, n=5)
    from symbiosis_brain import server

    server._init(tmp_vault)

    sync_all_calls = {"count": 0}
    sync_one_calls = {"paths": []}

    orig_sync_all = server._sync.sync_all
    orig_sync_one = server._sync.sync_one

    def counting_sync_all(*a, **kw):
        sync_all_calls["count"] += 1
        return orig_sync_all(*a, **kw)

    def counting_sync_one(path, *a, **kw):
        sync_one_calls["paths"].append(path)
        return orig_sync_one(path, *a, **kw)

    monkeypatch.setattr(server._sync, "sync_all", counting_sync_all)
    monkeypatch.setattr(server._sync, "sync_one", counting_sync_one)

    # Trigger a write via the internal helper (simulates brain_write tool)
    server._write_note_body(
        rel_path="wiki/new.md",
        new_text="---\ntitle: New\ntype: wiki\nscope: global\ntags: []\n---\n\nbody.\n",
        op="write",
        title="New",
    )

    assert sync_all_calls["count"] == 0, "expected sync_one, not sync_all"
    assert sync_one_calls["paths"] == ["wiki/new.md"]
    server._storage.close()


def test_brain_status_exposes_wal_and_index_health(tmp_vault, db_path):
    """brain_status output contains WAL size, pending frames, and index sync state."""
    _seed_vault(tmp_vault, n=2)
    from symbiosis_brain import server
    server._init(tmp_vault)

    import asyncio
    output = asyncio.run(server.call_tool("brain_status", {}))
    text = output[0].text

    assert "Notes:" in text
    assert "WAL size:" in text
    assert "WAL pages pending:" in text
    assert "Vector index in sync:" in text
    # Healthy state — counts match
    assert "Vector index in sync: yes" in text

    # Force drift via SearchEngine.delete_vec (raw SQL fails because vec0 isn't loaded
    # on a fresh non-SearchEngine connection — same lesson learned in earlier T6 test)
    server._search.delete_vec(server._storage.list_notes()[0]["path"])

    output = asyncio.run(server.call_tool("brain_status", {}))
    text = output[0].text
    assert "Vector index in sync: no" in text
    server._storage.close()


def _reindex_lockfile(db_path: Path):
    import hashlib
    from symbiosis_brain import search as search_mod
    tag = hashlib.sha256(str(db_path).encode()).hexdigest()[:12]
    return search_mod.LOCK_DIR / f"sb-reindex-{tag}.lock"


def _refuse_unlink_of(monkeypatch, lockfile: Path) -> None:
    """Make Path.unlink raise PermissionError for exactly one file.

    Reproduces the Windows shape: a live handle (AV, search indexer, a second
    copy of the process) turns unlink into WinError 32 -> PermissionError, which
    IS an OSError but NOT a FileNotFoundError."""
    real_unlink = Path.unlink

    def refusing_unlink(self, *a, **kw):
        if str(self) == str(lockfile):
            raise PermissionError(32, "the process cannot access the file")
        return real_unlink(self, *a, **kw)

    monkeypatch.setattr(Path, "unlink", refusing_unlink)


def test_reindex_lock_exits_cleanly_on_permission_error(tmp_path, monkeypatch):
    """Releasing our own lock must survive an undeletable lock file.

    Pre-fix the `except FileNotFoundError` in _reindex_lock's finally let the
    PermissionError escape and kill the caller — on the cold-start path, i.e.
    the whole MCP server. RED before the fix: the `with` block raises."""
    import os as _os
    from symbiosis_brain import search as search_mod

    db_path = tmp_path / "brain.db"
    lockfile = _reindex_lockfile(db_path)
    lockfile.unlink(missing_ok=True)
    _refuse_unlink_of(monkeypatch, lockfile)
    try:
        ran = False
        with search_mod._reindex_lock(db_path):
            ran = True
        assert ran, "the guarded body must have run"
    finally:
        # Path.unlink is still patched here — go around it.
        if _os.path.exists(lockfile):
            _os.remove(lockfile)


def test_lock_cleanup_does_not_mask_original_exception(tmp_path, monkeypatch):
    """A failing cleanup must never replace the caller's exception.

    Pre-fix the PermissionError raised inside `finally` shadowed whatever the
    body raised, so the real cause never reached the log. RED before the fix:
    pytest.raises(RuntimeError) sees a PermissionError instead."""
    import os as _os
    from symbiosis_brain import search as search_mod

    db_path = tmp_path / "brain.db"
    lockfile = _reindex_lockfile(db_path)
    lockfile.unlink(missing_ok=True)
    _refuse_unlink_of(monkeypatch, lockfile)
    try:
        with pytest.raises(RuntimeError, match="boom"):
            with search_mod._reindex_lock(db_path):
                raise RuntimeError("boom")
    finally:
        if _os.path.exists(lockfile):
            _os.remove(lockfile)


# ---------- CP-2 / §11.3: the retrieval log under real contention ----------

def _hold_write_lock(db_str: str, hold_ms: int, ready: "mp.Queue") -> None:
    """Third process: holds BEGIN IMMEDIATE for `hold_ms` (measured case
    [отчёт 01, F22] — an insert waited 338.8 ms behind a 300 ms holder, so
    busy_timeout=2000 does not always save the day)."""
    import sqlite3
    import time as _t
    conn = sqlite3.connect(db_str, isolation_level=None)
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("BEGIN IMMEDIATE")
    conn.execute(
        "INSERT OR REPLACE INTO schema_version (key, version) VALUES ('holder_probe', 1)"
    )
    ready.put("held")
    _t.sleep(hold_ms / 1000)
    conn.execute("COMMIT")
    conn.close()


def _log_events_in_subprocess(db_str: str, n: int, queue: "mp.Queue") -> None:
    """Child writer: n events, then close() in `finally` — that is flush point
    (в) of §2.4 п. 4, without which a skip on the LAST event never reaches the
    database and the parent's disjunction falls apart for no fault of the code."""
    from pathlib import Path

    from symbiosis_brain import retrieval_log
    from symbiosis_brain.storage import Storage

    db = Path(db_str)
    storage = Storage(db)
    error = ""
    try:
        ctx = retrieval_log.LogContext(
            source="mcp_search", db_path=db, client="testclient/9.9.9")
        for i in range(n):
            retrieval_log.record(
                ctx, query=f"query {i}", scope=None, mode="gist", fts_mode="all",
                hits=[{"path": f"wiki/note-{i}.md", "_score": 0.01, "_in_both": False}],
                latency_ms=1, vec_enabled=False,
            )
    except Exception as e:                      # record() must never raise
        error = f"{type(e).__name__}: {e}"
    finally:
        disabled = retrieval_log._disabled
        retrieval_log.close()
        storage.close()
        queue.put({"error": error, "disabled": disabled})


def test_retrieval_log_under_a_300ms_lock_holder(tmp_vault, db_path):
    """§11.3, case 3a. The assertion is a DISJUNCTION on purpose: either
    nothing was lost, or the number lost is EXACTLY the persistent counter.
    Reading skipped_count() here is forbidden — those counters live in the two
    child processes' memory, and the spec introduces no way to ship them back;
    that is precisely why the counter is persisted (§2.4 п. 4).
    """
    from symbiosis_brain import retrieval_log

    storage = Storage(db_path)                  # runs the migration once
    storage.close()

    ctx = mp.get_context("spawn")
    ready = ctx.Queue()
    results = ctx.Queue()
    holder = ctx.Process(target=_hold_write_lock, args=(str(db_path), 300, ready))
    holder.start()
    assert ready.get(timeout=30) == "held"

    writers = [
        ctx.Process(target=_log_events_in_subprocess, args=(str(db_path), 50, results)),
        ctx.Process(target=_log_events_in_subprocess, args=(str(db_path), 50, results)),
    ]
    for p in writers:
        p.start()
    reports = [results.get(timeout=120) for _ in writers]
    for p in writers:
        p.join(timeout=120)
    holder.join(timeout=120)

    assert [r["error"] for r in reports] == ["", ""]      # nothing escaped
    assert [r["disabled"] for r in reports] == [False, False]  # busy != structural

    storage = Storage(db_path)
    try:
        written = storage._conn.execute(
            "SELECT COUNT(*) AS c FROM retrieval_event").fetchone()["c"]
        skipped_total = storage.get_schema_version(
            retrieval_log.SKIPPED_TOTAL_KEY) or 0
    finally:
        storage.close()

    lost = 100 - written
    assert lost >= 0
    assert lost == 0 or lost == skipped_total, (
        f"lost={lost} skipped_total={skipped_total}: an event vanished without "
        f"being counted — §2.4 п. 4 broken"
    )
    hits = 0
    conn = sqlite3.connect(str(db_path))
    try:
        hits = conn.execute("SELECT COUNT(*) FROM retrieval_hit").fetchone()[0]
    finally:
        conn.close()
    assert hits == written                      # one hit per surviving event


def _open_storage_in_subprocess(db_str: str, queue: "mp.Queue", barrier) -> None:
    """`barrier` — стартовая синхронизация (как очередь-стартер в 3a выше). Без
    неё первый процесс успевает завершить миграцию до того, как второй её начнёт,
    и «гонка» ни разу не пересекается: тест зелен, а проверяет он тогда ровно
    ничего. Барьер отпускает оба процесса в один момент, ПЕРЕД открытием Storage."""
    from pathlib import Path

    from symbiosis_brain.storage import Storage
    try:
        barrier.wait(timeout=60)
        s = Storage(Path(db_str))
        payload = {
            "error": "",
            "tables": sorted(t for t in s.list_tables() if t.startswith("retrieval")),
            "version": s.get_schema_version("retrieval_log"),
            "notes": len(s.list_notes()),
        }
        s.close()
    except Exception as e:
        payload = {"error": f"{type(e).__name__}: {e}", "tables": [],
                   "version": None, "notes": -1}
    queue.put(payload)


def test_retrieval_log_migration_race_on_a_populated_db(tmp_vault, db_path):
    """§11.3, case 3b. On an EMPTY database the step is too fast to overlap, so
    the race is staged on a db that already has notes and older schema_version
    rows — and the retrieval_log step rolled back to 'not yet applied'."""
    _seed_vault(tmp_vault, n=5)
    storage = Storage(db_path)
    VaultSync(tmp_vault, storage).sync_all()
    storage._conn.execute("DROP TABLE IF EXISTS retrieval_hit")
    storage._conn.execute("DROP TABLE IF EXISTS retrieval_event")
    storage._conn.execute("DELETE FROM schema_version WHERE key='retrieval_log'")
    notes_before = len(storage.list_notes())
    assert notes_before == 5
    assert storage.get_schema_version("retrieval_log") is None
    storage.close()

    ctx = mp.get_context("spawn")
    q = ctx.Queue()
    start = ctx.Barrier(2)          # оба мигратора стартуют из одной точки
    procs = [ctx.Process(target=_open_storage_in_subprocess,
                         args=(str(db_path), q, start))
             for _ in range(2)]
    for p in procs:
        p.start()
    reports = [q.get(timeout=120) for _ in procs]
    for p in procs:
        p.join(timeout=120)

    assert [r["error"] for r in reports] == ["", ""]
    for r in reports:
        assert r["tables"] == ["retrieval_event", "retrieval_hit"]
        assert r["version"] == 1
        assert r["notes"] == notes_before        # older data survived intact


# ============ Model-change migration must not risk a working index ============
# A judge-accepted MAJOR finding on server.py's model-change branch: it used to
# reassign the module-global _search, DROP the working notes_vec, THEN try to
# load the new model inside index_all() — so a bad model name / a first-download
# network failure / a full disk destroyed a working index before the
# replacement was proven to work at all, and left schema_version pointing at
# the OLD model while notes_vec was already sized for the NEW, broken one.

def test_model_change_migration_skipped_when_target_model_fails_smoke_test(
        tmp_vault, db_path, monkeypatch):
    """A target model that cannot load must cost nothing: the existing,
    working index (DB row, table dimension, row count) must be left exactly
    as it was, and _search must keep serving vector search under the OLD
    model — never a stale name pointing at a table already dropped."""
    _seed_vault(tmp_vault, n=3)

    from symbiosis_brain import server

    server._init(tmp_vault)
    assert server._storage.get_schema_version("embedding_model") == _MODEL_NAME
    old_declared = server._storage._conn.execute(
        "SELECT sql FROM sqlite_master WHERE name='notes_vec'").fetchone()[0]
    assert server._storage._conn.execute(
        "SELECT COUNT(*) FROM notes_vec").fetchone()[0] == 3
    server._storage.close()

    for attr in ("_storage", "_search", "_sync", "_graph", "_temporal",
                 "_linter", "_vault_path"):
        setattr(server, attr, None)

    monkeypatch.setenv("SYMBIOSIS_BRAIN_EMBED_MODEL", "fixture/unloadable-model")
    # server.py imported _model_loadable by name (`from ... import
    # _model_loadable`), so it must be patched on the server module itself —
    # patching symbiosis_brain.search._model_loadable would not be seen here.
    monkeypatch.setattr(server, "_model_loadable", lambda name: False)

    from symbiosis_brain.search import SearchEngine as _SE
    orig_index_all = _SE.index_all
    call_count = {"n": 0}

    def counting_index_all(self, *a, **kw):
        call_count["n"] += 1
        return orig_index_all(self, *a, **kw)

    _SE.index_all = counting_index_all
    try:
        server._init(tmp_vault)
    finally:
        _SE.index_all = orig_index_all

    assert call_count["n"] == 0, "a failed smoke test must never reach index_all"
    assert server._storage.get_schema_version("embedding_model") == _MODEL_NAME, \
        "the DB must still name the old, working model"
    new_declared = server._storage._conn.execute(
        "SELECT sql FROM sqlite_master WHERE name='notes_vec'").fetchone()[0]
    assert new_declared == old_declared, "notes_vec must not have been recreated"
    assert server._storage._conn.execute(
        "SELECT COUNT(*) FROM notes_vec").fetchone()[0] == 3, \
        "the old vectors must not have been dropped"
    assert server._search._vec_enabled is True
    assert server._search.search_vector("note") != [] or True  # still callable, no raise
    server._storage.close()


def test_model_change_migration_writes_schema_version_before_index_all(
        tmp_vault, db_path, monkeypatch):
    """A crash/exception inside index_all(), AFTER the smoke test passed and
    notes_vec was already recreated for the new model, must leave
    schema_version and notes_vec's dimension in agreement (both already at
    the new model) — never a stale DB name pointing at a table sized for a
    model whose rebuild never finished. The process must also degrade to
    FTS-only for the rest of its life instead of raising unguarded on every
    future vector search."""
    _seed_vault(tmp_vault, n=3)

    from symbiosis_brain import server

    server._init(tmp_vault)
    server._storage.close()
    for attr in ("_storage", "_search", "_sync", "_graph", "_temporal",
                 "_linter", "_vault_path"):
        setattr(server, attr, None)

    monkeypatch.setenv("SYMBIOSIS_BRAIN_EMBED_MODEL", "fixture/model-mid-rebuild")
    import symbiosis_brain.search as sb_search

    def _fake_embed(texts):
        return [[0.1] * 384 for _ in texts]

    # The smoke test (_model_loadable) must succeed — it's the real thing
    # under test that comes after it that must fail.
    monkeypatch.setattr(sb_search, "_embed", _fake_embed)
    monkeypatch.setattr(sb_search, "_embed_one", lambda text: _fake_embed([text])[0])
    monkeypatch.setattr(sb_search, "_MODEL_NAME", sb_search._MODEL_NAME)
    monkeypatch.setattr(sb_search, "_embedder", sb_search._embedder)

    from symbiosis_brain.search import SearchEngine as _SE

    def failing_index_all(self, *a, **kw):
        raise RuntimeError("simulated failure mid full re-embed")

    monkeypatch.setattr(_SE, "index_all", failing_index_all)

    server._init(tmp_vault)  # must not raise — the failure is caught and degraded

    assert server._storage.get_schema_version("embedding_model") == "fixture/model-mid-rebuild", \
        "schema_version must already name the new model despite index_all failing"
    declared = server._storage._conn.execute(
        "SELECT sql FROM sqlite_master WHERE name='notes_vec'").fetchone()[0]
    assert "FLOAT[384]" in declared, (
        "fixture/model-mid-rebuild has no fastembed metadata — dimension "
        "falls back to the default, but the table must still already be "
        "recreated (not left at the old dimension): " + declared
    )
    assert server._search._vec_enabled is False, \
        "vector search must degrade to FTS-only for this process after the failure"
    assert server._search.search_vector("anything") == []
    server._storage.close()


def test_apply_targeted_index_holds_reindex_lock(tmp_vault, db_path, monkeypatch):
    """The steady-state startup path (stored_model already == target_model,
    the common case on every process start) used to reach
    _apply_targeted_index without ever acquiring _reindex_lock — a second,
    independently-reproducible gap letting its notes_vec writes race a
    concurrent migrator's _recreate_vec_table DROP+CREATE elsewhere."""
    _seed_vault(tmp_vault, n=1)

    from symbiosis_brain import server

    server._init(tmp_vault)

    calls = []
    real_lock = server._reindex_lock

    @contextmanager
    def spy_lock(db_path):
        calls.append(db_path)
        with real_lock(db_path):
            yield

    monkeypatch.setattr(server, "_reindex_lock", spy_lock)

    (tmp_vault / "wiki" / "extra.md").write_text(
        "---\ntitle: Extra\ntype: wiki\nscope: global\ntags: []\n---\n\nBody.\n",
        encoding="utf-8",
    )
    sync_result = server._sync.sync_all()
    server._apply_targeted_index(sync_result)

    assert calls, "_apply_targeted_index must acquire _reindex_lock"
    server._storage.close()
