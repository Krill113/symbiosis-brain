"""A moved note keeps its birth and last-edit stamps; a re-sync of unchanged
bytes keeps updated_at too.

`notes.path` is the primary key and Storage used to have no move primitive, so
both rename routes were DELETE + INSERT: `brain_rename` dropped the old row
and re-inserted under the new path, and `sync_all` after a move made on disk
behind our back inserted the new path and dropped the old one without ever
pairing the two. `upsert_note` stamped created_at AND updated_at from one `now`
on INSERT, and nothing read the old row before it was dropped — so a move
rewrote the note's age. On the live vault the 02.09 layout migration stamped
1525 notes into one 28-minute window exactly this way.

updated_at means "the content changed", not "the row was written". The second
half of the same defect was the full reindex: it NULLed every content_hash to
force re-ingest, and the re-upsert restamped updated_at for the whole vault at
once (4 distinct values across 1632 notes, measured).

The clock is controlled through Storage._now: the note is born at T_BORN, the
move happens at T_MOVED, and the moved row must still say T_BORN. Without the
clock swap a fast machine could pass by accident inside one microsecond.

The two stamps are separate cases on purpose: created_at must survive a move
under any reading of the field; updated_at survives because the owner chose
"content changed" (2026-09-04). The on-disk move changes the basename as well
as the directory, so a pairing that matched by file name instead of by content
would NOT pass — that heuristic is the wrong one.

Known limit, on purpose: a move combined with an edit in the same sync is not
paired — the bytes differ, so there is no evidence it is the same note — and
it is re-born with fresh stamps. Only a pure move keeps them.
"""
from contextlib import contextmanager
from pathlib import Path

import pytest

from symbiosis_brain.refactor import brain_rename
from symbiosis_brain.search import SearchEngine
from symbiosis_brain.storage import Storage
from symbiosis_brain.sync import VaultSync

T_BORN = "2026-01-01T00:00:00+00:00"
T_MOVED = "2026-06-01T12:00:00+00:00"

OLD = "wiki/n.md"
NEW = "global/wiki/n-moved.md"
NOTE = "---\ntitle: N\ntype: wiki\nscope: global\ngist: x\n---\n# N\n\nbody [[wiki/other]]\n"


def _vault_with_note_born_at(tmp_path: Path, monkeypatch) -> tuple[Path, Storage, VaultSync]:
    vault = tmp_path / "vault"
    (vault / "wiki").mkdir(parents=True)
    storage = Storage(tmp_path / "test.db")
    sync = VaultSync(vault_path=vault, storage=storage)
    (vault / OLD).write_text(NOTE, encoding="utf-8")
    monkeypatch.setattr(storage, "_now", lambda: T_BORN)
    sync.sync_all()
    born = storage.get_note(OLD)
    # Sanity on the clock swap itself — if this fails the test is broken, not the code.
    assert born["created_at"] == T_BORN and born["updated_at"] == T_BORN
    return vault, storage, sync


def _move_on_disk(vault: Path, old: str, new: str) -> None:
    """git mv / Obsidian / a script — not our tools."""
    (vault / new).parent.mkdir(parents=True, exist_ok=True)
    (vault / old).rename(vault / new)


def _force_reindex(storage: Storage) -> None:
    """What a migration does: drop the done-marker so the next sync_all rebuilds."""
    storage._conn.execute("DELETE FROM schema_version WHERE key='wikilink_normalization_reindex'")
    storage._conn.commit()
    assert storage.needs_full_reindex()


# --- route (a): brain_rename ---------------------------------------------------

@pytest.mark.parametrize("stamp", ["created_at", "updated_at"])
def test_brain_rename_keeps_stamp(tmp_path, monkeypatch, stamp):
    vault, storage, sync = _vault_with_note_born_at(tmp_path, monkeypatch)
    monkeypatch.setattr(storage, "_now", lambda: T_MOVED)

    brain_rename(OLD, NEW, storage=storage, sync=sync, vault_path=vault)

    moved = storage.get_note(NEW)
    assert moved is not None
    assert moved[stamp] == T_BORN
    # Moved, not duplicated: an implementation that inserted a copy and kept
    # the old row would satisfy the stamp check and fail here.
    assert storage.get_note(OLD) is None
    assert storage.count_notes() == 1


def test_brain_rename_replaces_a_stale_row_at_the_new_path(tmp_path, monkeypatch):
    """A DB row at the destination with no file behind it (drift from a crash
    between an unlink and its delete_note) is invisible to the file check in
    brain_rename. A plain UPDATE of the path would hit UNIQUE and raise AFTER
    the file had already moved — disk and DB apart. The move replaces the
    ghost, and the ghost's edges go with it: _sync_wikilinks starts from a
    clean source."""
    vault, storage, sync = _vault_with_note_born_at(tmp_path, monkeypatch)
    monkeypatch.setattr(storage, "_now", lambda: T_MOVED)
    storage.upsert_note(path=NEW, title="ghost", content="ghost",
                        note_type="wiki", scope="global")
    storage.upsert_relation(from_name="global/wiki/n-moved", to_name="broken:ghost",
                            relation_type="references", source_note=NEW,
                            raw_target="ghost", broken=True)
    assert not (vault / NEW).exists()

    brain_rename(OLD, NEW, storage=storage, sync=sync, vault_path=vault)

    moved = storage.get_note(NEW)
    assert moved["created_at"] == T_BORN and moved["updated_at"] == T_BORN
    assert storage.get_note(OLD) is None
    assert storage.count_notes() == 1
    raws = [r["raw_target"] for r in storage.get_relations("global/wiki/n-moved", "outgoing")]
    assert raws == ["wiki/other"]


def test_brain_rename_moves_vector_without_calling_the_model(tmp_path, monkeypatch):
    """The embedding is a function of the text, and the text did not change:
    the vector row is re-keyed by copying the stored blob, not by embedding
    again. vec0 refuses UPDATE on its primary key, so copy + delete is the
    only shape — and it must not touch the model."""
    vault, storage, sync = _vault_with_note_born_at(tmp_path, monkeypatch)
    monkeypatch.setattr("symbiosis_brain.search._embed",
                        lambda texts: [[0.25] * 384 for _ in texts])
    search = SearchEngine(storage)
    note = storage.get_note(OLD)
    search.index_note(OLD, f"{note['title']}\n{note['content']}")
    before = bytes(storage._conn.execute(
        "SELECT embedding FROM notes_vec WHERE path=?", (OLD,)).fetchone()[0])

    def forbidden(texts):
        raise AssertionError("a rename must not call the embedder")
    monkeypatch.setattr("symbiosis_brain.search._embed", forbidden)

    brain_rename(OLD, NEW, storage=storage, sync=sync, vault_path=vault, search=search)

    rows = {r[0]: bytes(r[1]) for r in
            storage._conn.execute("SELECT path, embedding FROM notes_vec").fetchall()}
    assert set(rows) == {NEW}
    assert rows[NEW] == before


# --- route (b): moved on disk, then sync_all ------------------------------------

@pytest.mark.parametrize("stamp", ["created_at", "updated_at"])
def test_sync_all_keeps_stamp_for_note_moved_on_disk(tmp_path, monkeypatch, stamp):
    vault, storage, sync = _vault_with_note_born_at(tmp_path, monkeypatch)
    _move_on_disk(vault, OLD, NEW)
    monkeypatch.setattr(storage, "_now", lambda: T_MOVED)

    sync.sync_all()

    assert storage.get_note(OLD) is None
    moved = storage.get_note(NEW)
    assert moved is not None
    assert moved[stamp] == T_BORN


def test_sync_all_replaces_a_row_an_older_process_inserted_at_the_new_path(tmp_path, monkeypatch):
    """Mixed versions during a rollout: a serve still on the old code inserts
    the new path (fresh stamps) after our Pass 0 has read the DB and before it
    re-keys the old row. The re-key replaces that row instead of tripping over
    UNIQUE; the old process's later delete_note(old) finds nothing to delete."""
    vault, storage, sync = _vault_with_note_born_at(tmp_path, monkeypatch)
    _move_on_disk(vault, OLD, NEW)
    monkeypatch.setattr(storage, "_now", lambda: T_MOVED)
    from symbiosis_brain.sync import _content_hash
    real_move = storage.move_note

    def old_process_inserts_first(old, new):
        storage.upsert_note(path=new, title="N", content="# N\n\nbody [[wiki/other]]\n",
                            note_type="wiki", scope="global", content_hash=_content_hash(NOTE))
        assert storage.get_note(new)["created_at"] == T_MOVED
        real_move(old, new)
    monkeypatch.setattr(storage, "move_note", old_process_inserts_first)

    result = sync.sync_all()

    assert result.moved == [(OLD, NEW)]
    row = storage.get_note(NEW)
    assert row["created_at"] == T_BORN and row["updated_at"] == T_BORN
    assert storage.get_note(OLD) is None
    assert storage.count_notes() == 1


def test_sync_all_does_not_pair_empty_bodied_notes(tmp_path, monkeypatch):
    """A pairing is a claim of identity. Equal bytes are evidence when there
    are bytes; two empty-bodied stubs share a hash and share nothing else, so
    the newcomer is born fresh rather than inheriting a stranger's dates."""
    stub = "---\ntitle: Stub\ntype: wiki\nscope: global\ngist: x\n---\n"
    vault = tmp_path / "vault"
    (vault / "wiki").mkdir(parents=True)
    storage = Storage(tmp_path / "test.db")
    sync = VaultSync(vault_path=vault, storage=storage)
    (vault / "wiki" / "a.md").write_text(stub, encoding="utf-8")
    monkeypatch.setattr(storage, "_now", lambda: T_BORN)
    sync.sync_all()
    (vault / "wiki" / "a.md").unlink()
    (vault / "global" / "wiki").mkdir(parents=True)
    (vault / "global" / "wiki" / "b.md").write_text(stub, encoding="utf-8")
    monkeypatch.setattr(storage, "_now", lambda: T_MOVED)

    result = sync.sync_all()

    assert result.moved == []
    assert result.added == ["global/wiki/b.md"] and result.removed == ["wiki/a.md"]
    assert storage.get_note("global/wiki/b.md")["created_at"] == T_MOVED


def test_sync_all_reports_the_move_and_rebuilds_its_links(tmp_path, monkeypatch):
    """A paired move is neither an add nor a removal, and its outgoing edges are
    re-resolved under the new path: after a change of scope a scope-prefix
    shorthand may point somewhere else, so the old edges cannot be kept."""
    vault, storage, sync = _vault_with_note_born_at(tmp_path, monkeypatch)
    _move_on_disk(vault, OLD, NEW)

    result = sync.sync_all()

    assert result.moved == [(OLD, NEW)]
    assert result.added == [] and result.removed == []
    by_source = {r["source_note"] for r in
                 storage._conn.execute("SELECT source_note FROM relations").fetchall()}
    assert by_source == {NEW}


@pytest.mark.parametrize("gone, appeared", [
    pytest.param(["wiki/a.md", "wiki/b.md"], ["global/wiki/a2.md", "global/wiki/b2.md"], id="2:2"),
    pytest.param(["wiki/a.md", "wiki/b.md"], ["global/wiki/a2.md"], id="2:1"),
    pytest.param(["wiki/a.md"], ["global/wiki/a2.md", "global/wiki/b2.md"], id="1:2"),
])
def test_sync_all_leaves_ambiguous_pairs_alone(tmp_path, monkeypatch, gone, appeared):
    """Identical notes vanish and identical files appear, and the counts do not
    match one-to-one: there is no honest way to say which became which, so
    nothing is paired — the newcomers are re-born with fresh stamps, which is
    at least true. Pairing is strictly 1:1 by content; no guessing by name, no
    partial matches.

    The pairing condition is a conjunction — exactly one gone AND exactly one
    appeared — so the 2:2 case alone proves nothing about either half: a guard
    that dropped one of them would still refuse 2:2. The 2:1 and 1:2 cases
    each falsify one half on its own."""
    vault = tmp_path / "vault"
    (vault / "wiki").mkdir(parents=True)
    storage = Storage(tmp_path / "test.db")
    sync = VaultSync(vault_path=vault, storage=storage)
    for p in gone:
        (vault / p).write_text(NOTE, encoding="utf-8")
    monkeypatch.setattr(storage, "_now", lambda: T_BORN)
    sync.sync_all()
    for p in gone:
        (vault / p).unlink()
    for p in appeared:  # same bytes as every note that vanished
        (vault / p).parent.mkdir(parents=True, exist_ok=True)
        (vault / p).write_text(NOTE, encoding="utf-8")
    monkeypatch.setattr(storage, "_now", lambda: T_MOVED)

    result = sync.sync_all()

    assert result.moved == []
    assert sorted(result.added) == sorted(appeared)
    assert sorted(result.removed) == sorted(gone)
    for p in appeared:
        assert storage.get_note(p)["created_at"] == T_MOVED


def test_server_targeted_index_finds_moved_note_by_its_new_path(tmp_vault, monkeypatch):
    """The vector index lives on sync_all's RESULT, not inside it: the server
    mirrors the diff into notes_vec. Once a move is reported as a move — not
    as removed + added — the old loop would touch nothing, and the vector
    would silently stay under the old path until the next repair_index. After
    the diff is applied the note must answer a vector query under its new
    path, the old path must hold no vector, and the model must not run."""
    from symbiosis_brain import server
    monkeypatch.setattr("symbiosis_brain.search._embed",
                        lambda texts: [[0.25] * 384 for _ in texts])
    (tmp_vault / OLD).write_text(NOTE, encoding="utf-8")
    server._init(tmp_vault)
    try:
        conn = server._storage._conn
        before = bytes(conn.execute(
            "SELECT embedding FROM notes_vec WHERE path=?", (OLD,)).fetchone()[0])

        def forbidden(texts):
            raise AssertionError("a move must not call the embedder")
        monkeypatch.setattr("symbiosis_brain.search._embed", forbidden)
        _move_on_disk(tmp_vault, OLD, NEW)

        result = server._sync.sync_all()
        server._apply_targeted_index(result)

        hit = conn.execute(
            "SELECT path FROM notes_vec WHERE embedding MATCH ? AND k=1", (before,)).fetchone()
        assert hit is not None and hit[0] == NEW
        assert conn.execute(
            "SELECT COUNT(*) FROM notes_vec WHERE path=?", (OLD,)).fetchone()[0] == 0
    finally:
        server._storage.close()


# --- the second half of the defect: re-sync and reindex --------------------------

def test_sync_one_restamps_updated_at_when_content_changes(tmp_path, monkeypatch):
    """The guard on the new meaning: updated_at still moves when the bytes do.
    Green before the fix as well — its job is to make "never restamp" fail."""
    vault, storage, sync = _vault_with_note_born_at(tmp_path, monkeypatch)
    (vault / OLD).write_text(NOTE + "\nmore\n", encoding="utf-8")
    monkeypatch.setattr(storage, "_now", lambda: T_MOVED)

    sync.sync_one(OLD)

    row = storage.get_note(OLD)
    assert row["created_at"] == T_BORN
    assert row["updated_at"] == T_MOVED


def test_full_reindex_keeps_updated_at_and_still_rebuilds_the_graph(tmp_path, monkeypatch):
    """A migration that forces a full reindex must re-ingest every note (so
    Pass 2 rebuilds relations with the current resolver) WITHOUT pretending
    every note changed today. The trigger is the marker a migration deletes."""
    vault, storage, sync = _vault_with_note_born_at(tmp_path, monkeypatch)
    _force_reindex(storage)
    monkeypatch.setattr(storage, "_now", lambda: T_MOVED)

    result = sync.sync_all()

    row = storage.get_note(OLD)
    assert row["updated_at"] == T_BORN
    assert row["created_at"] == T_BORN
    assert result.skipped == 0, "a forced reindex re-ingests, it does not skip"
    assert storage.get_relations("wiki/n", direction="outgoing"), "the graph was rebuilt"
    assert not storage.needs_full_reindex()


def test_forced_reindex_runs_under_the_reindex_lock(tmp_path, monkeypatch):
    """Two cold starts in one window both see "no marker"; without a lock both
    purge, and the second purge can land after the first rebuilt the graph and
    stamped the marker — if the second then dies, the graph is empty for good.
    The ordinary path never takes the lock: it has nothing to serialize."""
    vault, storage, sync = _vault_with_note_born_at(tmp_path, monkeypatch)
    taken: list = []

    @contextmanager
    def spy(db_path):
        taken.append(db_path)
        yield
    monkeypatch.setattr("symbiosis_brain.search._reindex_lock", spy)

    sync.sync_all()
    assert taken == []
    _force_reindex(storage)
    sync.sync_all()
    assert taken == [storage.db_path]


def test_forced_reindex_yields_when_the_lock_holder_already_rebuilt(tmp_path, monkeypatch):
    """The process that waited on the lock re-checks the marker once it holds
    it: the previous holder usually just did the rebuild it queued up for, and
    purging again would throw that work away."""
    vault, storage, sync = _vault_with_note_born_at(tmp_path, monkeypatch)
    edges_before = storage.get_relations("wiki/n", "outgoing")
    assert edges_before
    _force_reindex(storage)

    @contextmanager
    def holder_finished_first(db_path):
        storage.mark_reindex_done()
        yield
    monkeypatch.setattr("symbiosis_brain.search._reindex_lock", holder_finished_first)

    result = sync.sync_all()

    assert result.skipped == 1, "ordinary path: the unchanged note is skipped, nothing purged"
    assert storage.get_relations("wiki/n", "outgoing") == edges_before


def test_failed_ingest_is_retried_next_sync_even_with_unchanged_bytes(tmp_path, monkeypatch):
    """A forced run purged the graph; one note failed to parse for a transient
    reason. Its bytes did not change, so with its hash intact the next run
    would skip it and its edges would never come back. A failed ingest must
    drop the hash: we could not read it, so the hash vouches for nothing."""
    vault, storage, sync = _vault_with_note_born_at(tmp_path, monkeypatch)
    _force_reindex(storage)
    import symbiosis_brain.sync as sync_mod
    real_parse = sync_mod.parse_note

    def flaky_once(text):
        monkeypatch.setattr(sync_mod, "parse_note", real_parse)
        raise ValueError("transient")
    monkeypatch.setattr(sync_mod, "parse_note", flaky_once)

    failed = sync.sync_all()
    assert [p for p, _ in failed.failed] == [OLD]
    assert storage.get_relations("wiki/n", "outgoing") == [], "purged and not rebuilt"

    healed = sync.sync_all()  # same bytes on disk

    assert healed.failed == [] and healed.updated == [OLD]
    assert storage.get_relations("wiki/n", "outgoing"), "the graph came back"
