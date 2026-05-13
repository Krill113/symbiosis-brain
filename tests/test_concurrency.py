"""Integration tests for parallel-safety + idempotent startup.

Uses pytest fixtures from conftest.py. Multiprocessing tests use spawn for
cross-platform compatibility (mandatory on Windows).
"""
from __future__ import annotations

import multiprocessing as mp
import time
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


def test_init_full_reindex_on_model_drift(tmp_vault, db_path):
    """If schema_version[embedding_model] differs from current, full re-index runs."""
    _seed_vault(tmp_vault, n=3)

    from symbiosis_brain import server

    server._init(tmp_vault)
    assert server._storage.get_schema_version("embedding_model") == _MODEL_NAME
    server._storage.close()

    # Mutate stored model to simulate upgrade
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
        assert call_count["index_all"] == 1, "model drift should trigger full re-index"
        assert server._storage.get_schema_version("embedding_model") == _MODEL_NAME
    finally:
        _SE.index_all = orig_index_all
        server._storage.close()


def test_init_full_reindex_on_count_drift(tmp_vault, db_path):
    """If notes_vec count differs from notes count, full re-index runs."""
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

    call_count = {"index_all": 0}
    from symbiosis_brain.search import SearchEngine as _SE
    orig_index_all = _SE.index_all

    def counting_index_all(self, *a, **kw):
        call_count["index_all"] += 1
        return orig_index_all(self, *a, **kw)

    _SE.index_all = counting_index_all
    try:
        server._init(tmp_vault)
        assert call_count["index_all"] == 1, "count drift should trigger full re-index"
    finally:
        _SE.index_all = orig_index_all
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
