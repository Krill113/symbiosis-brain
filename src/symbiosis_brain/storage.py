import json
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

from symbiosis_brain.retrieval_log import RETRIEVAL_LOG_STATEMENTS

_BUSY_TIMEOUT_MS = 30_000


class Storage:
    def __init__(self, db_path: Path):
        self.db_path = db_path
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
        self._migrate_phase8_resolver_reindex()
        self._migrate_retrieval_log()
        # Partial index on broken=1 — must come after _migrate_wikilink_normalization
        # because `broken` is added by that migration on fresh DBs (it is not in
        # the base CREATE TABLE above).  CREATE INDEX IF NOT EXISTS is idempotent.
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_relations_broken"
            " ON relations(broken) WHERE broken=1"
        )
        self._conn.commit()

    def _migrate_phase8_resolver_reindex(self):
        """Phase 8 (anchor-strip + scope-prefix shorthand + stale-relation
        re-resolution) shipped resolver fixes that change which targets are
        broken vs resolved. Existing vaults still carry stale broken=True
        rows from before the fix because sync_all skips notes whose content_hash
        matches disk — Pass 2 (re-resolve wiki-links) never runs on unchanged
        notes. See [[mistakes/brain-sync-skips-stale-relations-via-content-hash]].

        This migration triggers the existing `needs_full_reindex` mechanism
        once per vault: deletes the `wikilink_normalization_reindex` marker so
        the next `sync_all()` call sees needs_full_reindex=True, purges
        relations + entities, re-ingests every note (hashes and stamps intact —
        the rebuild is forced by a flag on that run, not by NULLing hashes, as
        of 2026-09-04), and rebuilds the graph with the new resolver. After
        that rebuild marks itself done, both markers stay set and this
        migration is a no-op forever.
        """
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            row = self._conn.execute(
                "SELECT version FROM schema_version WHERE key=?",
                ("phase8_resolver_reindex",),
            ).fetchone()
            current = row["version"] if row else 0

            if current < 1:
                self._conn.execute(
                    "DELETE FROM schema_version WHERE key=?",
                    ("wikilink_normalization_reindex",),
                )
                self._conn.execute(
                    "INSERT OR REPLACE INTO schema_version (key, version) VALUES (?, ?)",
                    ("phase8_resolver_reindex", 1),
                )
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise

    def _migrate_retrieval_log(self):
        """Stage 2: retrieval_event + retrieval_hit. Empty tables, no reindex.

        Same shape as the two migrations above: BEGIN IMMEDIATE serializes
        parallel migrators, the version key makes the step a no-op afterwards.
        NOT executescript — it does an implicit COMMIT and tears the
        transaction apart (measured, §2.3); the real guarantee here is the
        idempotent DDL, and the transaction only keeps two cold-starting
        processes from interleaving halfway.
        """
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            row = self._conn.execute(
                "SELECT version FROM schema_version WHERE key=?", ("retrieval_log",)
            ).fetchone()
            current = row["version"] if row else 0

            if current < 1:
                for stmt in RETRIEVAL_LOG_STATEMENTS:
                    self._conn.execute(stmt)
                self._conn.execute(
                    "INSERT OR REPLACE INTO schema_version (key, version) VALUES (?, ?)",
                    ("retrieval_log", 1),
                )
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise

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
                    valid_from: str | None = None, valid_to: str | None = None,
                    content_hash: str | None = None):
        """Insert or refresh one note row.

        `updated_at` means "the file's content changed", not "the row was
        written". The two used to be the same thing: every re-sync restamped
        it, and a full reindex — which re-upserts the whole vault — left 1554
        of 1632 notes carrying the day of the migration. Now the caller passes
        the file's `content_hash` and the statement itself compares it with the
        stored one: equal → the old stamp stays; different, or either side
        unknown → restamped. `created_at` is never touched on conflict.
        "Content" is the file's bytes, whoever changed them: when brain_rename
        rewrites [[links]] in the notes that pointed at the moved one, those
        notes did change and their stamp moves — a mechanical edit is an edit.

        The hash is written here, in the same statement, rather than by a
        follow-up UPDATE: the comparison needs the OLD hash still in the row,
        and a second statement would have raced a parallel sync between the
        two. A caller with no hash (a test seeding rows) keeps whatever hash
        the row already has — it did not read the file, so it cannot vouch
        for it.
        """
        now = self._now()
        tags_json = json.dumps(tags or [])
        fm_json = json.dumps(frontmatter or {})
        # Single atomic statement avoids the SELECT-then-INSERT race that
        # causes UNIQUE-constraint failures when two processes concurrently
        # upsert the same path (e.g. parallel cold-starts on a fresh vault).
        self._conn.execute("""
            INSERT INTO notes (path, title, content, note_type, scope, tags, frontmatter,
                content_hash, created_at, updated_at, valid_from, valid_to)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(path) DO UPDATE SET
                title=excluded.title,
                content=excluded.content,
                note_type=excluded.note_type,
                scope=excluded.scope,
                tags=excluded.tags,
                frontmatter=excluded.frontmatter,
                updated_at=CASE
                    WHEN excluded.content_hash IS NOT NULL
                         AND notes.content_hash = excluded.content_hash
                    THEN notes.updated_at
                    ELSE excluded.updated_at
                END,
                content_hash=COALESCE(excluded.content_hash, notes.content_hash),
                valid_from=excluded.valid_from,
                valid_to=excluded.valid_to
        """, (path, title, content, note_type, scope, tags_json, fm_json, content_hash,
              now, now, valid_from, valid_to))
        self._conn.commit()

    def get_note(self, path: str) -> dict | None:
        row = self._conn.execute("SELECT * FROM notes WHERE path=?", (path,)).fetchone()
        if row is None:
            return None
        return self._row_to_note(row)

    def delete_note(self, path: str):
        self._conn.execute("DELETE FROM notes WHERE path=?", (path,))
        self._conn.commit()

    def move_note(self, old_path: str, new_path: str) -> None:
        """Re-key one note row under a new path, REPLACING any row already
        there — not a safe primitive for a live note at `new_path`; the caller
        must know that path holds no note. Everything else about the moved row
        stays put — rowid, created_at, updated_at, content_hash, valid_from/to.

        This is the primitive a rename was missing: with `path` as the primary
        key and only upsert/delete available, every move was DELETE + INSERT,
        and the INSERT stamped both dates with `now` (the 02.09 layout
        migration re-aged 1525 notes this way). FTS follows on its own through
        the `notes_au` trigger — same rowid, re-tokenized once.

        A row already sitting at `new_path` is replaced, in the same
        transaction. Both callers guarantee it is not a live note: brain_rename
        has checked that no FILE exists there (a leftover DB row is drift), and
        sync_all's pairing only names a path that is on disk but absent from
        the DB — a row can only have appeared there through a concurrent
        process running the old delete+insert code, stamped with fresh dates.
        Replacing it with the moved row is exactly the outcome wanted in both
        cases; a plain UPDATE would instead hit the UNIQUE path and raise after
        the file had already moved on disk. BEGIN IMMEDIATE on this
        autocommit connection (isolation_level=None, see __init__) makes the
        two statements one unit, as the migrations and index_note already do.

        NOT handled here, on purpose: the note's outgoing `relations` (a
        scope-prefix link may resolve differently after the move, so the
        caller deletes them by source and re-resolves — `_sync_wikilinks`
        starts with that DELETE, which also clears whatever edges the replaced
        row had) and its `notes_vec` row (vec0 refuses UPDATE on its primary
        key, see SearchEngine.move_vec).
        """
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            self._conn.execute("DELETE FROM notes WHERE path=?", (new_path,))
            self._conn.execute("UPDATE notes SET path=? WHERE path=?", (new_path, old_path))
            self._conn.execute("COMMIT")
        except Exception:
            try:
                self._conn.execute("ROLLBACK")
            except Exception:
                pass
            raise

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
        # Newest CLAIM first, not newest row. `updated_at` now moves only when
        # the content does, but it dates the file, not the claim — and when this
        # ordering was chosen a re-sync still rewrote it for every note it
        # touched (4 distinct values across 1632 notes on the live vault, so it
        # ordered nothing). `valid_from` lives in the file and says what the
        # reader asks. SQLite sorts NULL below every value, so DESC already puts
        # undated notes last; NULLIF folds the empty string into that same
        # bucket. `updated_at` stays as the tiebreaker.
        query += " ORDER BY NULLIF(valid_from, '') DESC, updated_at DESC"
        rows = self._conn.execute(query, params).fetchall()
        return [self._row_to_note(r) for r in rows]

    def count_notes(self) -> int:
        """Count total notes in vault (efficient — no data loaded)."""
        row = self._conn.execute("SELECT COUNT(*) as cnt FROM notes").fetchone()
        return row["cnt"]

    def count_broken_outgoing(self, source_canonical: str) -> int:
        """Count broken outgoing `references` relations for one source note
        (canonical path, no .md). Includes forward-refs, which are stored
        broken=True. Cheap: O(edges of one note). Used by the write counter
        to compute the broken-link delta of a single write."""
        row = self._conn.execute(
            "SELECT COUNT(*) AS cnt FROM relations "
            "WHERE from_name=? AND relation_type='references' AND broken=1",
            (source_canonical,),
        ).fetchone()
        return row["cnt"]

    def count_orphans(self) -> int:
        """Count notes with no LIVE-resolving incoming `references` edge.

        Uses resolver.compute_linked_canonicals — the SAME live connectivity logic
        as VaultLinter — so the write counter's orphans_now always agrees with
        brain_lint (even on a stale relations.broken cache). Excludes the
        scope-taxonomy index (kept in sync with lint's _TAXONOMY_PATH).
        """
        from symbiosis_brain.resolver import compute_linked_canonicals
        linked = compute_linked_canonicals(self)
        count = 0
        for p in self.get_all_paths():
            if p == "reference/scope-taxonomy.md":
                continue
            # Strip the same way VaultLinter.lint does (removesuffix), so the
            # two orphan counts agree exactly.
            if p.removesuffix(".md") not in linked:
                count += 1
        return count

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

    def get_reference_relations(self) -> list[dict]:
        """Return all `references` relations (from_name, to_name, raw_target).
        Used by resolver.compute_linked_canonicals for live connectivity."""
        rows = self._conn.execute(
            "SELECT from_name, to_name, raw_target FROM relations "
            "WHERE relation_type='references'"
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

    def find_broken_relations(self) -> list[dict]:
        """Return all relations currently marked broken=True, with their
        raw_target strings. Used to re-resolve when a new note is created
        that might satisfy a previously-unresolvable target."""
        rows = self._conn.execute(
            "SELECT id, from_name, source_note, raw_target, label "
            "FROM relations WHERE broken=1 AND relation_type='references'"
        ).fetchall()
        return [dict(r) for r in rows]

    def update_relation_resolution(
        self, relation_id: int, new_to_name: str, broken: bool
    ) -> None:
        """Update a relation's to_name + broken flag in place. Used by the
        stale-relation re-resolver after a new note is created.

        Note: the relations table has UNIQUE(from_name, to_name, relation_type).
        Updating to_name on a broken row is safe because the corresponding
        broken row used `to_name='broken:<target>'` — different from the
        canonical to_name we're switching to. If a duplicate happens to exist
        (e.g. the same source already has a non-broken edge to canonical),
        SQLite raises IntegrityError; caller should treat as best-effort and
        skip the offending row.
        """
        self._conn.execute(
            "UPDATE relations SET to_name=?, broken=? WHERE id=?",
            (new_to_name, 1 if broken else 0, relation_id),
        )
        self._conn.commit()

    def find_inbound_refs(self, canonical_path: str) -> list[dict]:
        """Return all non-broken `references` relations whose to_name equals
        canonical_path. Used by brain_rename / brain_delete to identify which
        notes link TO the target."""
        rows = self._conn.execute(
            "SELECT id, source_note, raw_target, label, broken "
            "FROM relations "
            "WHERE relation_type='references' AND to_name=? AND broken=0",
            (canonical_path,),
        ).fetchall()
        return [dict(r) for r in rows]

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

    def get_schema_version(self, key: str) -> str | int | None:
        """Read a schema_version key. Returns int if the stored value parses
        as int, else str. Returns None if key is absent.

        Note: the underlying column is named `version` (legacy — INTEGER affinity),
        but we use SQLite's dynamic typing to store strings (e.g. model names) too.
        Always go through these helpers, not raw SQL.
        """
        row = self._conn.execute(
            "SELECT version FROM schema_version WHERE key=?", (key,)
        ).fetchone()
        if row is None:
            return None
        v = row["version"]
        if isinstance(v, int):
            return v
        try:
            return int(v)
        except (ValueError, TypeError):
            return v

    def set_schema_version(self, key: str, value: str | int) -> None:
        """Upsert a schema_version key. Strings stored as-is via SQLite
        dynamic typing on the INTEGER-affinity column."""
        self._conn.execute(
            "INSERT OR REPLACE INTO schema_version (key, version) VALUES (?, ?)",
            (key, str(value) if not isinstance(value, int) else value),
        )
        self._conn.commit()

    def close(self):
        self._conn.close()


def vector_drift(conn) -> tuple[int, int] | None:
    """`(notes, notes_vec)` — or None when there is no vector table to compare.

    One definition, two readers: brain_status reports it to a person, doctor
    turns it into a pass/fail. Counting the same thing twice in two places is
    how the two answers start disagreeing, and on 2026-09-02 the disagreement
    that mattered was between "All OK" and a notes_vec of zero — semantic
    search dead, every surface reporting health.

    Takes a connection rather than a path so the caller decides what it opened.
    Doctor must NOT construct a Storage: that runs migrations, and an
    instrument that changes what it inspects is not a diagnostic.
    """
    try:
        notes = conn.execute("SELECT COUNT(*) FROM notes").fetchone()[0]
    except sqlite3.Error:
        return None

    # `notes_vec` is a sqlite-vec virtual table: querying it needs the extension
    # loaded on THIS connection, and a connection that never loaded it gets the
    # same error as one where the table does not exist. Reading that error as
    # "there is no vector index" is a lie a caller cannot see through — doctor
    # printed exactly that against a vault holding 1628 indexed notes. So the
    # extension is loaded first, and only a failure AFTERWARDS means absent.
    try:
        import sqlite_vec
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
    except Exception:
        # Cannot load it here: whether an index exists is genuinely unknown, and
        # "unknown" is the honest answer, not "none".
        return None

    try:
        vec = conn.execute("SELECT COUNT(*) FROM notes_vec").fetchone()[0]
    except sqlite3.Error:
        return None
    return int(notes), int(vec)
