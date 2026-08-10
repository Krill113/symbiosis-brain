from pathlib import Path

import pytest

from symbiosis_brain.search import SearchEngine
from symbiosis_brain.storage import Storage
from symbiosis_brain.sync import VaultSync

FAKE_VEC = [0.1] * 384


def _fake_embed(texts):
    return [FAKE_VEC for _ in texts]


@pytest.fixture
def engine(tmp_vault: Path, db_path: Path, monkeypatch):
    monkeypatch.setattr("symbiosis_brain.search._embed", _fake_embed)
    storage = Storage(db_path)
    search = SearchEngine(storage)
    sync = VaultSync(tmp_vault, storage)
    for i in range(3):
        (tmp_vault / "wiki" / f"n{i}.md").write_text(
            f"---\ntitle: Note {i}\ntype: wiki\n---\n\nBody {i}.\n", encoding="utf-8"
        )
    sync.sync_all()
    for i in range(3):
        note = storage.get_note(f"wiki/n{i}.md")
        search.index_note(f"wiki/n{i}.md", f"{note['title']}\n{note['content']}")
    return storage, search


def _counts(storage):
    n = storage._conn.execute("SELECT COUNT(*) FROM notes").fetchone()[0]
    v = storage._conn.execute("SELECT COUNT(*) FROM notes_vec").fetchone()[0]
    return n, v


def test_repair_embeds_only_missing(engine, monkeypatch):
    storage, search = engine
    storage._conn.execute("DELETE FROM notes_vec WHERE path='wiki/n1.md'")
    storage._conn.commit()

    calls = []
    def counting_embed(texts):
        calls.append(list(texts))
        return [FAKE_VEC for _ in texts]
    monkeypatch.setattr("symbiosis_brain.search._embed", counting_embed)

    result = search.repair_index()

    assert result == {"embedded": 1, "orphans_deleted": 0}
    assert _counts(storage) == (3, 3)
    assert sum(len(c) for c in calls) == 1  # exactly one text embedded


def test_repair_deletes_orphan_vec_rows(engine):
    storage, search = engine
    storage._conn.execute("DELETE FROM notes WHERE path='wiki/n2.md'")
    storage._conn.commit()

    result = search.repair_index()

    assert result == {"embedded": 0, "orphans_deleted": 1}
    assert _counts(storage) == (2, 2)


def test_repair_noop_when_in_sync(engine, monkeypatch):
    storage, search = engine

    def exploding_embed(texts):
        raise AssertionError("must not embed when index is in sync")
    monkeypatch.setattr("symbiosis_brain.search._embed", exploding_embed)

    result = search.repair_index()

    assert result == {"embedded": 0, "orphans_deleted": 0}


def test_embed_passes_capped_batch_size(monkeypatch):
    from symbiosis_brain import search as search_mod

    captured = {}

    class StubEmbedder:
        def embed(self, texts, **kwargs):
            captured.update(kwargs)
            import numpy as np
            for _ in texts:
                yield np.array(FAKE_VEC, dtype=np.float32)

    monkeypatch.setattr(search_mod, "_get_embedder", lambda: StubEmbedder())
    search_mod._embed(["one", "two"])

    assert captured.get("batch_size") == search_mod._EMBED_BATCH_SIZE
    assert search_mod._EMBED_BATCH_SIZE <= 32


def test_repair_rechecks_after_lock(engine, monkeypatch):
    """If another process repaired the index while we waited on the lock,
    repair_index must do nothing (double-checked locking)."""
    storage, search = engine
    storage._conn.execute("DELETE FROM notes_vec WHERE path='wiki/n1.md'")
    storage._conn.commit()

    real_dirty = search.is_index_dirty
    def dirty_then_heal():
        # simulate the concurrent holder finishing its repair while we waited:
        # equalize the counts (drop the note whose vec row we deleted) so the
        # post-acquire re-check sees a clean index
        storage._conn.execute("DELETE FROM notes WHERE path='wiki/n1.md'")
        storage._conn.commit()
        return real_dirty()
    monkeypatch.setattr(search, "is_index_dirty", dirty_then_heal)

    def exploding_embed(texts):
        raise AssertionError("must not embed — index healed while waiting")
    monkeypatch.setattr("symbiosis_brain.search._embed", exploding_embed)

    result = search.repair_index()
    assert result == {"embedded": 0, "orphans_deleted": 0}


def test_reindex_lock_released_on_error(engine, monkeypatch):
    from symbiosis_brain import search as search_mod
    storage, search = engine
    storage._conn.execute("DELETE FROM notes_vec WHERE path='wiki/n1.md'")
    storage._conn.commit()

    def exploding_embed(texts):
        raise RuntimeError("boom")
    monkeypatch.setattr("symbiosis_brain.search._embed", exploding_embed)

    with pytest.raises(RuntimeError):
        search.repair_index()

    import hashlib
    tag = hashlib.sha256(str(storage.db_path).encode()).hexdigest()[:12]
    assert not (search_mod.LOCK_DIR / f"sb-reindex-{tag}.lock").exists()


def test_init_decision_single_rebuild_after_model_cleared(tmp_vault: Path, db_path: Path, monkeypatch):
    """Two _init passes with the model marker cleared between them must
    trigger exactly one full rebuild — single-process approximation of two
    processes racing the reindex lock's decision block. Pass 2's post-acquire
    re-check (is_index_dirty()) must see the index already correct and skip
    index_all(), the double-checked-locking behaviour Task 4 adds."""
    for i in range(3):
        (tmp_vault / "wiki" / f"note{i}.md").write_text(
            f"---\ntitle: Note {i}\ntype: wiki\nscope: global\ntags: []\n---\n\nBody {i}.\n",
            encoding="utf-8",
        )
    monkeypatch.setattr("symbiosis_brain.search._embed", _fake_embed)

    from symbiosis_brain import server
    from symbiosis_brain.search import _MODEL_NAME

    call_count = {"index_all": 0}
    orig_index_all = SearchEngine.index_all

    def counting_index_all(self, *a, **kw):
        call_count["index_all"] += 1
        return orig_index_all(self, *a, **kw)

    SearchEngine.index_all = counting_index_all
    try:
        # Pass 1: fresh vault, model unregistered -> bootstrap rebuild.
        server._init(tmp_vault)
        assert server._storage.get_schema_version("embedding_model") == _MODEL_NAME
        server._storage.close()

        for attr in ("_storage", "_search", "_sync", "_graph", "_temporal",
                     "_linter", "_vault_path"):
            setattr(server, attr, None)

        # Clear the marker as if this pass never saw the registration —
        # approximates a second racer's outer read seeing stored_model=None.
        s = Storage(tmp_vault / ".index" / "brain.db")
        s._conn.execute("DELETE FROM schema_version WHERE key='embedding_model'")
        s._conn.commit()
        s.close()

        for attr in ("_storage", "_search", "_sync", "_graph", "_temporal",
                     "_linter", "_vault_path"):
            setattr(server, attr, None)

        # Pass 2: model marker gone again, but the index is already correct —
        # the lock's post-acquire re-check must skip the rebuild.
        server._init(tmp_vault)
        assert server._storage.get_schema_version("embedding_model") == _MODEL_NAME

        assert call_count["index_all"] == 1, "expected exactly one full rebuild across both passes"
    finally:
        SearchEngine.index_all = orig_index_all
        if server._storage is not None:
            server._storage.close()
        for attr in ("_storage", "_search", "_sync", "_graph", "_temporal",
                     "_linter", "_vault_path"):
            setattr(server, attr, None)
