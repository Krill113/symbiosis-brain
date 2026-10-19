from __future__ import annotations

import sqlite3
import struct
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from symbiosis_brain.adapters.opencode.compat_index import (
    ExistingIndexReader,
    IndexNotReady,
    ReadOnlyIndexView,
    validate_index,
)


def _fixture(
    path: Path, *, model: str = "fixture/model", include_vec: bool = True,
    empty: bool = False,
) -> None:
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE notes (
            path TEXT PRIMARY KEY, title TEXT NOT NULL, content TEXT NOT NULL,
            note_type TEXT NOT NULL, scope TEXT NOT NULL, tags TEXT NOT NULL,
            frontmatter TEXT NOT NULL, content_hash TEXT, created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL, valid_from TEXT, valid_to TEXT
        );
        CREATE VIRTUAL TABLE notes_fts USING fts5(title, content, tags, content='notes', content_rowid='rowid', tokenize='porter');
        CREATE TRIGGER notes_au AFTER UPDATE ON notes BEGIN
          INSERT INTO notes_fts(notes_fts, rowid, title, content, tags) VALUES ('delete', old.rowid, old.title, old.content, old.tags);
          INSERT INTO notes_fts(rowid, title, content, tags) VALUES (new.rowid, new.title, new.content, new.tags);
        END;
        CREATE TABLE schema_version (key TEXT PRIMARY KEY, version INTEGER NOT NULL);
        INSERT INTO schema_version VALUES ('embedding_model', 'fixture/model');
    """)
    conn.execute("UPDATE schema_version SET version=? WHERE key='embedding_model'", (model,))
    if not empty:
        conn.execute(
            "INSERT INTO notes VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("fixture.md", "Fixture", "synthetic amber context", "wiki", "sample", "[]",
             '{"gist":"synthetic amber gist"}', "hash", "now", "now", None, None),
        )
        conn.execute("INSERT INTO notes_fts(rowid, title, content, tags) SELECT rowid, title, content, tags FROM notes")
    if include_vec:
        import sqlite_vec

        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
        conn.execute("CREATE VIRTUAL TABLE notes_vec USING vec0(path TEXT PRIMARY KEY, embedding FLOAT[4])")
        if not empty:
            conn.execute("INSERT INTO notes_vec VALUES (?, ?)", ("fixture.md", bytes(16)))
    conn.commit()
    conn.close()


def _add_candidates(path: Path) -> None:
    conn = sqlite3.connect(path)
    import sqlite_vec

    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    conn.execute("PRAGMA journal_mode=WAL")
    notes = [
        ("sample/decisions/amber.md", "Amber decision", "amber synthetic decision context", "sample", '{"gist":"amber decision gist"}', "hash-2"),
        ("global/amber.md", "Global amber", "amber synthetic global guidance", "global", '{"gist":"amber global gist"}', "hash-3"),
    ]
    for path_name, title, content, scope, frontmatter, content_hash in notes:
        conn.execute(
            "INSERT INTO notes VALUES (?, ?, ?, 'decision', ?, '[]', ?, ?, 'now', 'now', NULL, NULL)",
            (path_name, title, content, scope, frontmatter, content_hash),
        )
        conn.execute(
            "INSERT INTO notes_fts(rowid, title, content, tags) SELECT rowid, title, content, tags FROM notes WHERE path=?",
            (path_name,),
        )
        vector = struct.pack("4f", 0.8 if scope == "sample" else 0.6, 0.2, 0.0, 0.0)
        conn.execute("INSERT INTO notes_vec(path, embedding) VALUES (?, ?)", (path_name, vector))
    conn.commit()
    conn.close()


def _db_snapshot(path: Path) -> tuple[list[tuple], list[tuple], list[tuple], list[tuple]]:
    conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    import sqlite_vec

    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    result = (
        [tuple(row) for row in conn.execute("SELECT type, name, sql FROM sqlite_master ORDER BY type, name")],
        [tuple(row) for row in conn.execute("SELECT path, title, content, content_hash FROM notes ORDER BY path")],
        [tuple(row) for row in conn.execute("SELECT key, version FROM schema_version ORDER BY key")],
        [tuple(row) for row in conn.execute("SELECT path FROM notes_vec ORDER BY path")],
    )
    conn.close()
    return result


def test_read_only_uri_refuses_writes_and_does_not_create_missing_db(tmp_path: Path) -> None:
    path = tmp_path / "vault with spaces.sqlite3"
    _fixture(path)
    view = ReadOnlyIndexView(path)
    assert view._conn.execute("PRAGMA query_only").fetchone()[0] == 1
    assert view._conn.execute("PRAGMA journal_mode").fetchone()[0].lower() in {"wal", "delete"}
    with pytest.raises(sqlite3.OperationalError):
        view._conn.execute("UPDATE notes SET title='changed'")
    with pytest.raises(sqlite3.OperationalError):
        view._conn.execute("CREATE TABLE forbidden(x)")
    view.close()

    absent = tmp_path / "not-created" / "vault.sqlite3"
    with pytest.raises((sqlite3.OperationalError, IndexNotReady, FileNotFoundError)):
        ReadOnlyIndexView(absent)
    assert not absent.exists()


def test_schema_gate_rejects_missing_vector_table(tmp_path: Path) -> None:
    path = tmp_path / "missing-vector.sqlite3"
    _fixture(path, include_vec=False)
    view = ReadOnlyIndexView(path)
    with pytest.raises(IndexNotReady, match="vector"):
        validate_index(view)
    view.close()


def test_model_drift_is_rechecked_even_when_dimension_is_unchanged(tmp_path: Path) -> None:
    path = tmp_path / "drift.sqlite3"
    _fixture(path)
    view = ReadOnlyIndexView(path)

    class Engine:
        storage = view
        model_name = "fixture/model"
        _vec_enabled = True

        def search(self, **kwargs):
            return []

    reader = ExistingIndexReader(Engine())
    writer = sqlite3.connect(path)
    writer.execute("UPDATE schema_version SET version='fixture/other-model' WHERE key='embedding_model'")
    writer.commit()
    writer.close()
    with pytest.raises(IndexNotReady, match="model"):
        reader.search("amber", scope="sample")
    view.close()


def test_existing_reader_requires_vector_path_and_uses_short_read_transaction(tmp_path: Path) -> None:
    path = tmp_path / "reader.sqlite3"
    _fixture(path)
    view = ReadOnlyIndexView(path)

    class Engine:
        storage = view
        model_name = "fixture/model"
        _vec_enabled = True

        def search(self, **kwargs):
            assert view._conn.in_transaction
            return [{"path": "fixture.md", "gist": "synthetic amber gist"}]

    hits = ExistingIndexReader(Engine()).search("amber", scope="sample")
    assert [hit["path"] for hit in hits] == ["fixture.md"]
    assert not view._conn.in_transaction
    Engine._vec_enabled = False
    with pytest.raises(IndexNotReady, match="vector"):
        ExistingIndexReader(Engine()).search("amber", scope="sample")
    view.close()


def test_empty_but_schema_ready_index_is_a_successful_empty_result(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "empty.sqlite3"
    _fixture(path, empty=True)
    import symbiosis_brain.search as search

    monkeypatch.setattr(search, "_embed_query", lambda query, model: [1.0, 0.0, 0.0, 0.0])
    view = ReadOnlyIndexView(path)
    engine = search.SearchEngine(view, model_name="fixture/model")
    assert engine._vec_enabled is True
    reader = ExistingIndexReader(engine)
    assert reader.search("amber", scope="sample") == []
    view.close()


def test_unchanged_core_search_parity_and_committed_writer_visibility(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "core-parity.sqlite3"
    _fixture(path)
    _add_candidates(path)
    writer = sqlite3.connect(path)
    import sqlite_vec

    writer.enable_load_extension(True)
    sqlite_vec.load(writer)
    writer.enable_load_extension(False)
    writer.execute("UPDATE notes_vec SET embedding=? WHERE path=?", (struct.pack("4f", 1, 0, 0, 0), "fixture.md"))
    writer.commit()
    writer.close()

    import symbiosis_brain.search as search

    monkeypatch.setattr(search, "_embed_query", lambda query, model: [1.0, 0.0, 0.0, 0.0])
    view = ReadOnlyIndexView(path)
    engine = search.SearchEngine(view, model_name="fixture/model")
    reader = ExistingIndexReader(engine)
    schema_before = [tuple(row) for row in view._conn.execute(
        "SELECT type, name, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
    )]
    expected = engine.search("amber", scope="sample", mode="gist")
    actual = reader.search("amber", scope="sample", mode="gist")
    assert len(actual) >= 3
    assert [(item["path"], item["gist"]) for item in actual] == [
        (item["path"], item["gist"]) for item in expected
    ]
    (tmp_path / "fixture.md").write_text("synthetic cobalt external edit not yet indexed", encoding="utf-8")
    (tmp_path / "unindexed.md").write_text("synthetic cobalt note not yet indexed", encoding="utf-8")
    assert all(hit["path"] != "unindexed.md" for hit in reader.search("cobalt", scope="sample"))
    assert all("external edit" not in hit.get("gist", "") for hit in reader.search("cobalt", scope="sample"))
    schema_after = [tuple(row) for row in view._conn.execute(
        "SELECT type, name, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
    )]
    assert schema_after == schema_before
    before = view._conn.execute("SELECT content_hash, content FROM notes WHERE path='fixture.md'").fetchone()

    writer = sqlite3.connect(path)
    writer.execute("UPDATE notes SET content='freshly indexed violet note' WHERE path='fixture.md'")
    writer.commit()
    writer.close()
    fresh = reader.search("violet", scope="sample", mode="gist")
    assert "fixture.md" in [item["path"] for item in fresh]
    after = view._conn.execute("SELECT content_hash, content FROM notes WHERE path='fixture.md'").fetchone()
    assert before[0] == after[0] == "hash"
    assert after[1] == "freshly indexed violet note"
    assert not view._conn.in_transaction
    view.close()


def test_adapter_recall_never_calls_index_or_storage_mutation_apis(tmp_path: Path, monkeypatch) -> None:
    vault = tmp_path / "synthetic-vault"
    index_dir = vault / ".index"
    index_dir.mkdir(parents=True)
    path = index_dir / "brain.db"
    _fixture(path)
    import symbiosis_brain.search as search
    from symbiosis_brain.adapters.opencode.protocol import ContextRequest
    from symbiosis_brain.adapters.opencode.worker import _service_factory
    from symbiosis_brain.storage import Storage
    from symbiosis_brain.sync import VaultSync

    monkeypatch.setattr(search, "_embed_query", lambda query, model: [1.0, 0.0, 0.0, 0.0])
    def forbidden(*args, **kwargs):
        raise AssertionError("read-only worker invoked a mutation API")

    monkeypatch.setattr(search.SearchEngine, "index_note", forbidden)
    monkeypatch.setattr(search.SearchEngine, "index_all", forbidden)
    monkeypatch.setattr(search.SearchEngine, "_recreate_vec_table", forbidden)
    monkeypatch.setattr(Storage, "__init__", forbidden)
    monkeypatch.setattr(Storage, "_create_tables", forbidden)
    monkeypatch.setattr(Storage, "upsert_note", forbidden)
    monkeypatch.setattr(Storage, "delete_note", forbidden)
    monkeypatch.setattr(VaultSync, "sync_all", forbidden)
    monkeypatch.setattr(VaultSync, "sync_one", forbidden)
    request = ContextRequest(
        request_id="synthetic-recall", session_id="synthetic-session", generation=1,
        operation="recall", cwd="/synthetic/project", origin="user", capability_revision=0,
        boundary="/synthetic", scope="sample", query="amber",
    )
    result = _service_factory(str(vault))("retrieval")(request)
    assert result.status == "ok"
    assert [block.id for block in result.blocks] == ["memory:fixture.md"]


def test_killed_subprocess_during_real_vec_lookup_leaves_schema_and_logical_content_unchanged(tmp_path: Path) -> None:
    path = tmp_path / "kill-during-query.sqlite3"
    _fixture(path)
    _add_candidates(path)
    before = _db_snapshot(path)
    code = r'''
import sys
import symbiosis_brain.search as search
from symbiosis_brain.adapters.opencode.compat_index import ExistingIndexReader, ReadOnlyIndexView
from symbiosis_brain.search import SearchEngine
search._embed_query = lambda query, model: [1.0, 0.0, 0.0, 0.0]
view = ReadOnlyIndexView(sys.argv[1])
reader = ExistingIndexReader(SearchEngine(view, model_name="fixture/model"))
def trace(sql):
    if "FROM notes_vec v" in sql and "MATCH" in sql:
        print("VECTOR_LOOKUP_ENTERED", flush=True)
        sys.stdin.readline()
view._conn.set_trace_callback(trace)
reader.search("amber", scope="sample", mode="gist")
'''
    process = subprocess.Popen(
        [sys.executable, "-c", code, str(path)], stdin=subprocess.PIPE,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8",
    )
    executor = ThreadPoolExecutor(max_workers=1)
    try:
        marker = executor.submit(process.stdout.readline).result(timeout=10)
        assert marker.strip() == "VECTOR_LOOKUP_ENTERED"
        process.kill()
        assert process.wait(timeout=5) is not None
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        if process.stdin:
            process.stdin.close()
        if process.stdout:
            process.stdout.close()
        if process.stderr:
            process.stderr.close()
        executor.shutdown(wait=True)
    assert _db_snapshot(path) == before
