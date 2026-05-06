import json
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

_BUSY_TIMEOUT_MS = 30_000


class Storage:
    def __init__(self, db_path: Path):
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False,
                                     isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        # Set busy_timeout FIRST so the connection has retry semantics before
        # any locking operations.
        self._conn.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
        # journal_mode=WAL requires a brief exclusive lock; the SQLite busy
        # handler does not cover PRAGMA journal_mode, so we retry manually
        # when multiple processes open the same DB simultaneously.
        deadline = time.monotonic() + _BUSY_TIMEOUT_MS / 1000  # ms → s
        while True:
            try:
                self._conn.execute("PRAGMA journal_mode=WAL")
                break
            except sqlite3.OperationalError as e:
                # Only retry on locking contention; readonly/I/O errors should
                # propagate immediately rather than hang for the full timeout.
                if "locked" not in str(e).lower() or time.monotonic() >= deadline:
                    raise
                time.sleep(0.05)
        self._conn.execute("PRAGMA wal_autocheckpoint=200")
        self._conn.execute("PRAGMA journal_size_limit=10485760")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._create_tables()

    def _create_tables(self):
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS notes (
                path TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                content TEXT NOT NULL,
                note_type TEXT NOT NULL DEFAULT 'wiki',
                scope TEXT NOT NULL DEFAULT 'global',
                tags TEXT NOT NULL DEFAULT '[]',
                frontmatter TEXT NOT NULL DEFAULT '{}',
                content_hash TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                valid_from TEXT,
                valid_to TEXT
            );

            CREATE TABLE IF NOT EXISTS entities (
                name TEXT PRIMARY KEY,
                entity_type TEXT NOT NULL DEFAULT 'concept',
                scope TEXT NOT NULL DEFAULT 'global',
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS relations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                from_name TEXT NOT NULL,
                to_name TEXT NOT NULL,
                relation_type TEXT NOT NULL DEFAULT 'related_to',
                source_note TEXT,
                created_at TEXT NOT NULL,
                UNIQUE(from_name, to_name, relation_type)
            );

            CREATE VIRTUAL TABLE IF NOT EXISTS notes_fts USING fts5(
                title, content, tags,
                content='notes',
                content_rowid='rowid',
                tokenize='porter'
            );

            CREATE TRIGGER IF NOT EXISTS notes_ai AFTER INSERT ON notes BEGIN
                INSERT INTO notes_fts(rowid, title, content, tags)
                VALUES (new.rowid, new.title, new.content, new.tags);
            END;

            CREATE TRIGGER IF NOT EXISTS notes_ad AFTER DELETE ON notes BEGIN
                INSERT INTO notes_fts(notes_fts, rowid, title, content, tags)
                VALUES ('delete', old.rowid, old.title, old.content, old.tags);
            END;

            CREATE TRIGGER IF NOT EXISTS notes_au AFTER UPDATE ON notes BEGIN
                INSERT INTO notes_fts(notes_fts, rowid, title, content, tags)
                VALUES ('delete', old.rowid, old.title, old.content, old.tags);
                INSERT INTO notes_fts(rowid, title, content, tags)
                VALUES (new.rowid, new.title, new.content, new.tags);
            END;

            CREATE TABLE IF NOT EXISTS schema_version (
                key TEXT PRIMARY KEY,
                version INTEGER NOT NULL
            );
        """)
        self._conn.commit()
        self._migrate_wikilink_normalization()

    def _migrate_wikilink_normalization(self):
        # BEGIN IMMEDIATE serializes parallel migrators: only one runs the
        # ALTER TABLE block; the others wait via busy_timeout, then re-read
        # schema_version and skip (current >= 1).
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            row = self._conn.execute(
                "SELECT version FROM schema_version WHERE key=?",
                ("wikilink_normalization",),
            ).fetchone()
            current = row["version"] if row else 0

            if current < 1:
                existing_cols = {
                    r["name"]
                    for r in self._conn.execute(
                        "PRAGMA table_info(relations)"
                    ).fetchall()
                }
                if "label" not in existing_cols:
                    self._conn.execute(
                        "ALTER TABLE relations ADD COLUMN label TEXT"
                    )
                if "raw_target" not in existing_cols:
                    self._conn.execute(
                        "ALTER TABLE relations ADD COLUMN raw_target TEXT"
                    )
                if "broken" not in existing_cols:
                    self._conn.execute(
                        "ALTER TABLE relations ADD COLUMN broken INTEGER NOT NULL DEFAULT 0"
                    )
                self._conn.execute(
                    "INSERT OR REPLACE INTO schema_version (key, version) VALUES (?, ?)",
                    ("wikilink_normalization", 1),
                )
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise

    def list_tables(self) -> list[str]:
        rows = self._conn.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table','view') ORDER BY name"
        ).fetchall()
        return [r["name"] for r in rows]

    def _now(self) -> str:
        return datetime.now(timezone.utc).isoformat()

    # --- Notes CRUD ---

    def upsert_note(self, path: str, title: str, content: str, note_type: str, scope: str,
                    tags: list[str] | None = None, frontmatter: dict | None = None,
                    valid_from: str | None = None, valid_to: str | None = None):
        now = self._now()
        tags_json = json.dumps(tags or [])
        fm_json = json.dumps(frontmatter or {})
        existing = self.get_note(path)
        if existing:
            self._conn.execute("""
                UPDATE notes SET title=?, content=?, note_type=?, scope=?, tags=?,
                    frontmatter=?, updated_at=?, valid_from=?, valid_to=?
                WHERE path=?
            """, (title, content, note_type, scope, tags_json, fm_json, now, valid_from, valid_to, path))
        else:
            self._conn.execute("""
                INSERT INTO notes (path, title, content, note_type, scope, tags, frontmatter,
                    created_at, updated_at, valid_from, valid_to)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (path, title, content, note_type, scope, tags_json, fm_json, now, now, valid_from, valid_to))
        self._conn.commit()

    def get_note(self, path: str) -> dict | None:
        row = self._conn.execute("SELECT * FROM notes WHERE path=?", (path,)).fetchone()
        if row is None:
            return None
        return self._row_to_note(row)

    def delete_note(self, path: str):
        self._conn.execute("DELETE FROM notes WHERE path=?", (path,))
        self._conn.commit()

    def delete_relations_by_source(self, source_note: str):
        self._conn.execute("DELETE FROM relations WHERE source_note=?", (source_note,))
        self._conn.commit()

    def list_notes(
        self,
        scope: str | None = None,
        note_type: str | None = None,
        strict: bool = False,
    ) -> list[dict]:
        query = "SELECT * FROM notes WHERE 1=1"
        params: list = []
        if scope:
            # Single-scope filter when strict=True OR caller asked for global.
            # strict+global is a no-op: the IN-branch would reduce to ('global','global').
            if scope == "global" or strict:
                query += " AND scope=?"
                params.append(scope)
            else:
                query += " AND scope IN (?, 'global')"
                params.append(scope)
        if note_type:
            query += " AND note_type=?"
            params.append(note_type)
        query += " ORDER BY updated_at DESC"
        rows = self._conn.execute(query, params).fetchall()
        return [self._row_to_note(r) for r in rows]

    def count_notes(self) -> int:
        """Count total notes in vault (efficient — no data loaded)."""
        row = self._conn.execute("SELECT COUNT(*) as cnt FROM notes").fetchone()
        return row["cnt"]

    def _row_to_note(self, row: sqlite3.Row) -> dict:
        d = dict(row)
        d["tags"] = json.loads(d["tags"])
        d["frontmatter"] = json.loads(d["frontmatter"])
        return d

    # --- Entities ---

    def upsert_entity(self, name: str, entity_type: str = "concept", scope: str = "global"):
        now = self._now()
        self._conn.execute("""
            INSERT INTO entities (name, entity_type, scope, created_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(name) DO UPDATE SET entity_type=excluded.entity_type
        """, (name, entity_type, scope, now))
        self._conn.commit()

    def list_entities(self, scope: str | None = None) -> list[dict]:
        if scope:
            rows = self._conn.execute("SELECT * FROM entities WHERE scope=?", (scope,)).fetchall()
        else:
            rows = self._conn.execute("SELECT * FROM entities").fetchall()
        return [dict(r) for r in rows]

    # --- Relations ---

    def upsert_relation(self, from_name: str, to_name: str, relation_type: str = "related_to",
                        source_note: str | None = None, label: str | None = None,
                        raw_target: str | None = None, broken: bool = False):
        now = self._now()
        self._conn.execute("""
            INSERT INTO relations (from_name, to_name, relation_type, source_note, created_at,
                                   label, raw_target, broken)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(from_name, to_name, relation_type) DO UPDATE SET
                label=excluded.label,
                raw_target=excluded.raw_target,
                broken=excluded.broken
        """, (from_name, to_name, relation_type, source_note, now,
              label, raw_target, 1 if broken else 0))
        self._conn.commit()

    def get_relations(self, entity_name: str, direction: str = "outgoing") -> list[dict]:
        if direction == "outgoing":
            rows = self._conn.execute(
                "SELECT * FROM relations WHERE from_name=?", (entity_name,)
            ).fetchall()
        elif direction == "incoming":
            rows = self._conn.execute(
                "SELECT * FROM relations WHERE to_name=?", (entity_name,)
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM relations WHERE from_name=? OR to_name=?",
                (entity_name, entity_name)
            ).fetchall()
        return [dict(r) for r in rows]

    def get_in_degree_map(self) -> dict[str, int]:
        """Return {entity_name: incoming_edge_count} for every node with ≥1 incoming edge.

        Entities with zero incoming edges are omitted — callers should default to 0.
        """
        rows = self._conn.execute(
            "SELECT to_name, COUNT(*) AS cnt FROM relations GROUP BY to_name"
        ).fetchall()
        return {row["to_name"]: row["cnt"] for row in rows}

    def get_all_paths(self) -> list[str]:
        """Return all note paths (including .md extension) from notes table."""
        rows = self._conn.execute("SELECT path FROM notes").fetchall()
        return [r["path"] for r in rows]

    def needs_full_reindex(self) -> bool:
        """True if any registered migration recorded a forced-resync marker."""
        row = self._conn.execute(
            "SELECT version FROM schema_version WHERE key=?",
            ("wikilink_normalization_reindex",),
        ).fetchone()
        return row is None

    def mark_reindex_done(self):
        self._conn.execute(
            "INSERT OR REPLACE INTO schema_version (key, version) VALUES (?, ?)",
            ("wikilink_normalization_reindex", 1),
        )
        self._conn.commit()

    def close(self):
        self._conn.close()
