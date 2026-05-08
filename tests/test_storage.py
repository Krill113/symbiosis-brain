import multiprocessing as mp
import sqlite3
from pathlib import Path

from symbiosis_brain.storage import Storage


class TestStorageInit:
    def test_creates_database_and_tables(self, db_path: Path):
        storage = Storage(db_path)
        tables = storage.list_tables()
        assert "notes" in tables
        assert "entities" in tables
        assert "relations" in tables
        assert "notes_fts" in tables

    def test_creates_parent_directory(self, tmp_path: Path):
        db_path = tmp_path / "subdir" / "brain.db"
        storage = Storage(db_path)
        assert db_path.exists()


class TestNotesCRUD:
    def test_upsert_and_get_note(self, db_path: Path):
        storage = Storage(db_path)
        storage.upsert_note(
            path="projects/beta.md",
            title="Beta Project",
            content="# Beta\nMain project",
            note_type="project",
            scope="beta",
            tags=["dotnet", "wpf"],
            frontmatter={"created_at": "2025-01-01"},
        )
        note = storage.get_note("projects/beta.md")
        assert note is not None
        assert note["title"] == "Beta Project"
        assert note["scope"] == "beta"
        assert "dotnet" in note["tags"]

    def test_upsert_updates_existing(self, db_path: Path):
        storage = Storage(db_path)
        storage.upsert_note(path="wiki/test.md", title="V1", content="old", note_type="wiki", scope="global")
        storage.upsert_note(path="wiki/test.md", title="V2", content="new", note_type="wiki", scope="global")
        note = storage.get_note("wiki/test.md")
        assert note["title"] == "V2"
        assert note["content"] == "new"

    def test_delete_note(self, db_path: Path):
        storage = Storage(db_path)
        storage.upsert_note(path="wiki/tmp.md", title="Tmp", content="x", note_type="wiki", scope="global")
        storage.delete_note("wiki/tmp.md")
        assert storage.get_note("wiki/tmp.md") is None

    def test_list_notes_by_scope(self, db_path: Path):
        storage = Storage(db_path)
        storage.upsert_note(path="p/a.md", title="A", content="a", note_type="project", scope="beta")
        storage.upsert_note(path="p/b.md", title="B", content="b", note_type="project", scope="api")
        storage.upsert_note(path="w/c.md", title="C", content="c", note_type="wiki", scope="global")
        ld_notes = storage.list_notes(scope="beta")
        assert len(ld_notes) == 2  # beta + global
        titles = [n["title"] for n in ld_notes]
        assert "A" in titles
        assert "C" in titles
        assert "B" not in titles

    def test_list_notes_strict_excludes_global(self, db_path: Path):
        storage = Storage(db_path)
        storage.upsert_note(
            path="global/a.md", title="A", content="", note_type="wiki",
            scope="global", tags=[],
        )
        storage.upsert_note(
            path="alpha-seti/b.md", title="B", content="", note_type="wiki",
            scope="alpha-seti", tags=[],
        )

        # Default (strict=False): scope-specific request returns matches + global.
        default = storage.list_notes(scope="alpha-seti")
        assert {n["path"] for n in default} == {"global/a.md", "alpha-seti/b.md"}

        # strict=True: scope-specific request excludes global.
        strict = storage.list_notes(scope="alpha-seti", strict=True)
        assert {n["path"] for n in strict} == {"alpha-seti/b.md"}

        # strict=True + scope="global" is a no-op (global filter is already strict).
        strict_global = storage.list_notes(scope="global", strict=True)
        assert {n["path"] for n in strict_global} == {"global/a.md"}

        # strict=True without scope = return everything (strict is meaningful only with scope).
        all_strict = storage.list_notes(strict=True)
        assert {n["path"] for n in all_strict} == {"global/a.md", "alpha-seti/b.md"}


class TestEntitiesAndRelations:
    def test_upsert_entities_from_wikilinks(self, db_path: Path):
        storage = Storage(db_path)
        storage.upsert_entity(name="Dapper", entity_type="technology")
        storage.upsert_entity(name="EF Core", entity_type="technology")
        entities = storage.list_entities()
        names = [e["name"] for e in entities]
        assert "Dapper" in names
        assert "EF Core" in names

    def test_create_relation(self, db_path: Path):
        storage = Storage(db_path)
        storage.upsert_entity(name="Dapper", entity_type="technology")
        storage.upsert_entity(name="beta", entity_type="project")
        storage.upsert_relation(from_name="beta", to_name="Dapper", relation_type="uses")
        relations = storage.get_relations("beta")
        assert len(relations) == 1
        assert relations[0]["to_name"] == "Dapper"
        assert relations[0]["relation_type"] == "uses"


class TestGetInDegreeMap:
    def test_empty_db_returns_empty(self, db_path):
        storage = Storage(db_path)
        assert storage.get_in_degree_map() == {}

    def test_counts_incoming_edges(self, db_path):
        storage = Storage(db_path)
        storage.upsert_entity("A", "concept")
        storage.upsert_entity("B", "concept")
        storage.upsert_entity("Hub", "concept")
        storage.upsert_relation("A", "Hub", "uses")
        storage.upsert_relation("B", "Hub", "uses")
        result = storage.get_in_degree_map()
        assert result["Hub"] == 2
        assert result.get("A", 0) == 0
        assert result.get("B", 0) == 0

    def test_multiple_relation_types_between_same_pair_count_separately(self, db_path):
        storage = Storage(db_path)
        storage.upsert_entity("A", "concept")
        storage.upsert_entity("B", "concept")
        storage.upsert_relation("A", "B", "uses")
        storage.upsert_relation("A", "B", "references")
        result = storage.get_in_degree_map()
        assert result["B"] == 2

    def test_duplicate_relation_not_double_counted(self, db_path):
        storage = Storage(db_path)
        storage.upsert_entity("A", "concept")
        storage.upsert_entity("B", "concept")
        storage.upsert_relation("A", "B", "uses")
        storage.upsert_relation("A", "B", "uses")  # ON CONFLICT DO NOTHING
        result = storage.get_in_degree_map()
        assert result["B"] == 1


class TestSchemaMigration:
    def test_schema_version_table_created(self, tmp_path):
        s = Storage(tmp_path / "t.db")
        tables = s.list_tables()
        assert "schema_version" in tables

    def test_wikilink_normalization_version_1(self, tmp_path):
        s = Storage(tmp_path / "t.db")
        row = s._conn.execute(
            "SELECT version FROM schema_version WHERE key=?",
            ("wikilink_normalization",),
        ).fetchone()
        assert row is not None
        assert row["version"] == 1

    def test_relations_has_new_columns(self, tmp_path):
        s = Storage(tmp_path / "t.db")
        cols = [
            r["name"]
            for r in s._conn.execute("PRAGMA table_info(relations)").fetchall()
        ]
        assert "label" in cols
        assert "raw_target" in cols
        assert "broken" in cols

    def test_migration_from_old_schema(self, tmp_path):
        """Simulate opening an old DB without the new columns."""
        db = tmp_path / "old.db"
        conn = sqlite3.connect(str(db))
        conn.executescript("""
            CREATE TABLE notes (path TEXT PRIMARY KEY, title TEXT NOT NULL,
              content TEXT NOT NULL, note_type TEXT, scope TEXT, tags TEXT,
              frontmatter TEXT, content_hash TEXT, created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL, valid_from TEXT, valid_to TEXT);
            CREATE TABLE relations (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              from_name TEXT NOT NULL, to_name TEXT NOT NULL,
              relation_type TEXT NOT NULL DEFAULT 'related_to',
              source_note TEXT, created_at TEXT NOT NULL,
              UNIQUE(from_name, to_name, relation_type)
            );
        """)
        conn.commit()
        conn.close()

        s = Storage(db)

        cols = [
            r["name"]
            for r in s._conn.execute("PRAGMA table_info(relations)").fetchall()
        ]
        assert "label" in cols
        assert "broken" in cols


class TestUpsertRelationNewFields:
    def test_stores_label_raw_target_broken(self, tmp_path):
        from symbiosis_brain.storage import Storage
        s = Storage(tmp_path / "t.db")
        s.upsert_entity(name="projects/foo")
        s.upsert_relation(
            from_name="projects/src",
            to_name="projects/foo",
            relation_type="references",
            source_note="projects/src.md",
            label="Foo Project",
            raw_target="projects/foo|Foo Project",
            broken=False,
        )
        row = s._conn.execute(
            "SELECT label, raw_target, broken FROM relations WHERE from_name=?",
            ("projects/src",),
        ).fetchone()
        assert row["label"] == "Foo Project"
        assert row["raw_target"] == "projects/foo|Foo Project"
        assert row["broken"] == 0

    def test_broken_flag_stored_as_1(self, tmp_path):
        from symbiosis_brain.storage import Storage
        s = Storage(tmp_path / "t.db")
        s.upsert_relation(
            from_name="projects/src",
            to_name="broken:nonexistent",
            relation_type="references",
            source_note="projects/src.md",
            broken=True,
            raw_target="nonexistent",
        )
        row = s._conn.execute(
            "SELECT broken FROM relations WHERE from_name=?", ("projects/src",)
        ).fetchone()
        assert row["broken"] == 1


def test_pragmas_set_on_init(db_path: Path):
    s = Storage(db_path)
    assert s._conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert s._conn.execute("PRAGMA busy_timeout").fetchone()[0] == 30000
    assert s._conn.execute("PRAGMA wal_autocheckpoint").fetchone()[0] == 200
    assert s._conn.execute("PRAGMA journal_size_limit").fetchone()[0] == 10485760
    assert s._conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    s.close()


def _open_storage_in_subprocess(db_path_str: str, queue: "mp.Queue"):
    """Helper for test_concurrent_migration_safe — must be top-level for spawn."""
    try:
        s = Storage(Path(db_path_str))
        version = s._conn.execute(
            "SELECT version FROM schema_version WHERE key='wikilink_normalization'"
        ).fetchone()
        queue.put(("ok", version[0] if version else None))
        s.close()
    except Exception as exc:
        queue.put(("err", f"{type(exc).__name__}: {exc}"))


def test_concurrent_migration_safe(db_path: Path):
    # Bootstrap an unmigrated DB by stopping at the bare bones.
    db_path.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(str(db_path))
    c.executescript("""
        CREATE TABLE notes (path TEXT PRIMARY KEY, title TEXT, content TEXT,
            note_type TEXT, scope TEXT, tags TEXT, frontmatter TEXT,
            content_hash TEXT, created_at TEXT, updated_at TEXT,
            valid_from TEXT, valid_to TEXT);
        CREATE TABLE entities (name TEXT PRIMARY KEY, entity_type TEXT,
            scope TEXT, created_at TEXT);
        CREATE TABLE relations (id INTEGER PRIMARY KEY AUTOINCREMENT,
            from_name TEXT, to_name TEXT, relation_type TEXT,
            source_note TEXT, created_at TEXT,
            UNIQUE(from_name, to_name, relation_type));
        CREATE TABLE schema_version (key TEXT PRIMARY KEY, version INTEGER);
    """)
    c.commit()
    c.close()

    # Now spawn 3 processes that each open Storage and trigger migration.
    ctx = mp.get_context("spawn")
    q = ctx.Queue()
    procs = [ctx.Process(target=_open_storage_in_subprocess,
                         args=(str(db_path), q)) for _ in range(3)]
    for p in procs:
        p.start()
    for p in procs:
        p.join(timeout=15)
        assert p.exitcode == 0, f"process exited with {p.exitcode}"

    results = [q.get_nowait() for _ in range(3)]
    assert all(r[0] == "ok" for r in results), f"errors: {results}"
    assert all(r[1] == 1 for r in results), f"unexpected versions: {results}"


def test_schema_version_helpers_round_trip(db_path: Path):
    s = Storage(db_path)
    assert s.get_schema_version("does_not_exist") is None
    s.set_schema_version("embedding_model", "BAAI/bge-small-en-v1.5")
    assert s.get_schema_version("embedding_model") == "BAAI/bge-small-en-v1.5"
    s.set_schema_version("embedding_model", "BAAI/bge-large-en-v1.5")
    assert s.get_schema_version("embedding_model") == "BAAI/bge-large-en-v1.5"
    s.set_schema_version("some_int", 42)
    assert s.get_schema_version("some_int") == 42
    s.close()


def test_find_inbound_refs_returns_only_non_broken_pointers_to_path(tmp_path):
    storage = Storage(tmp_path / "test.db")

    # B exists
    storage.upsert_note(
        path="wiki/b.md", title="B", content="# B", note_type="wiki",
        scope="global", tags=[], frontmatter={"gist": "x"},
        valid_from=None, valid_to=None,
    )
    # A points at B (resolved)
    storage.upsert_relation(
        from_name="wiki/a", to_name="wiki/b", relation_type="references",
        source_note="wiki/a.md", label=None, raw_target="wiki/b", broken=False,
    )
    # C points at "wiki/missing" (broken)
    storage.upsert_relation(
        from_name="wiki/c", to_name="broken:wiki/missing", relation_type="references",
        source_note="wiki/c.md", label=None, raw_target="wiki/missing", broken=True,
    )

    refs = storage.find_inbound_refs("wiki/b")
    sources = [r["source_note"] for r in refs]
    assert sources == ["wiki/a.md"]
    # SQLite stores broken as INTEGER 0/1, not Python bool
    assert all(r["broken"] == 0 for r in refs)
