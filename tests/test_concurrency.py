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
