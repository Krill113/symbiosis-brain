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


def test_init_lock_recheck_prevents_stale_rebuild(tmp_vault: Path, monkeypatch):
    """_init's post-acquire re-read of stored_model must be trusted over the
    outer (pre-lock) read — not just re-fetched and ignored.

    Simulates another process registering the model while we waited on the
    lock: the first get_schema_version("embedding_model") call (outer,
    pre-lock) returns None; the second (inner, post-lock) returns the
    current model. The vault is fresh, so notes_vec starts empty — the index
    is "dirty" by construction, no extra setup needed.

    Pre-fix code reads stored_model exactly ONCE and rebuilds whenever it's
    None and the index is dirty, so it would call index_all() here — RED on
    that code. The fixed code re-reads inside the lock, sees the model
    already registered, and falls through to the targeted incremental path
    instead, which indexes the notes via index_note() without ever calling
    index_all().
    """
    for i in range(3):
        (tmp_vault / "wiki" / f"note{i}.md").write_text(
            f"---\ntitle: Note {i}\ntype: wiki\nscope: global\ntags: []\n---\n\nBody {i}.\n",
            encoding="utf-8",
        )
    monkeypatch.setattr("symbiosis_brain.search._embed", _fake_embed)

    from symbiosis_brain import server
    from symbiosis_brain.search import _MODEL_NAME

    reads = {"n": 0}
    orig_get_schema_version = Storage.get_schema_version

    def racing_get_schema_version(self, key):
        if key != "embedding_model":
            return orig_get_schema_version(self, key)
        reads["n"] += 1
        return None if reads["n"] == 1 else _MODEL_NAME

    monkeypatch.setattr(Storage, "get_schema_version", racing_get_schema_version)

    call_count = {"index_all": 0}
    orig_index_all = SearchEngine.index_all

    def counting_index_all(self, *a, **kw):
        call_count["index_all"] += 1
        return orig_index_all(self, *a, **kw)

    monkeypatch.setattr(SearchEngine, "index_all", counting_index_all)

    try:
        server._init(tmp_vault)

        assert call_count["index_all"] == 0, \
            "post-acquire re-read must see the model already registered and skip the rebuild"
        assert reads["n"] >= 2, \
            "decision block must re-read stored_model inside the lock, not trust the outer read"
        assert not server._search.is_index_dirty(), \
            "execution must have reached the targeted incremental path and indexed the notes"
    finally:
        if server._storage is not None:
            server._storage.close()
        for attr in ("_storage", "_search", "_sync", "_graph", "_temporal",
                     "_linter", "_vault_path"):
            setattr(server, attr, None)


def test_reindex_lock_wait_timeout_proceeds_unguarded(tmp_path, monkeypatch):
    """If the lock is held (fresh, not stale) and we hit the wait deadline,
    we must proceed without waiting forever — and must NOT touch the
    holder's lock file, since that file represents someone else's live
    critical section, not ours to remove."""
    import hashlib
    from symbiosis_brain import search as search_mod

    db_path = tmp_path / "brain.db"
    tag = hashlib.sha256(str(db_path).encode()).hexdigest()[:12]
    lockfile = search_mod.LOCK_DIR / f"sb-reindex-{tag}.lock"
    lockfile.write_text("999999\n0\n", encoding="utf-8")  # fresh mtime, held by "another process"
    try:
        monkeypatch.setattr(search_mod, "_REINDEX_LOCK_WAIT_S", 0)
        ran = False
        with search_mod._reindex_lock(db_path):
            ran = True
        assert ran, "body must still run (unguarded proceed) on wait-timeout"
        assert lockfile.exists(), "a give-up waiter must not delete the holder's live lock file"
    finally:
        lockfile.unlink(missing_ok=True)


def test_reindex_lock_breaks_stale_lock_and_acquires(tmp_path):
    """A lock file older than the staleness threshold is abandoned — break
    it and actually acquire the lock (not just proceed unguarded)."""
    import hashlib
    import os as _os
    from symbiosis_brain import search as search_mod

    db_path = tmp_path / "brain.db"
    tag = hashlib.sha256(str(db_path).encode()).hexdigest()[:12]
    lockfile = search_mod.LOCK_DIR / f"sb-reindex-{tag}.lock"
    lockfile.write_text("111111\n0\n", encoding="utf-8")
    _os.utime(lockfile, (0, 0))  # epoch 0 -> far past the staleness threshold
    try:
        ran = False
        with search_mod._reindex_lock(db_path):
            ran = True
            # real acquisition: the (re-created) lock file exists while held
            assert lockfile.exists()
        assert ran
        # acquired path: our own finally released it, so it's gone afterward
        assert not lockfile.exists()
    finally:
        lockfile.unlink(missing_ok=True)
