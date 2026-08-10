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
