"""Read-only compatibility gate for querying the MCP-owned Brain index."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from symbiosis_brain.storage import Storage

_BUSY_TIMEOUT_MS = 500
_NOTES_COLUMNS = {
    "path", "title", "content", "note_type", "scope", "tags", "frontmatter",
    "content_hash", "created_at", "updated_at", "valid_from", "valid_to",
}


class IndexNotReady(RuntimeError):
    """The existing index cannot safely serve hybrid retrieval."""


class ReadOnlyIndexView(Storage):
    """The Storage surface SearchEngine needs, without its writable setup path."""

    def __init__(self, db_path: str | Path, *, busy_timeout_ms: int = _BUSY_TIMEOUT_MS):
        path = Path(db_path).expanduser().resolve(strict=True)
        if not path.is_file():
            raise FileNotFoundError(path)
        if isinstance(busy_timeout_ms, bool) or not isinstance(busy_timeout_ms, int) or not 1 <= busy_timeout_ms <= 5000:
            raise ValueError("busy_timeout_ms must be between 1 and 5000")
        self.db_path = path
        self._conn = sqlite3.connect(
            f"{path.as_uri()}?mode=ro", uri=True, check_same_thread=False,
            isolation_level=None, timeout=busy_timeout_ms / 1000,
        )
        try:
            self._conn.row_factory = sqlite3.Row
            self._conn.execute(f"PRAGMA busy_timeout={busy_timeout_ms}")
            self._conn.execute("PRAGMA query_only=ON")
            if self._conn.execute("PRAGMA query_only").fetchone()[0] != 1:
                raise sqlite3.OperationalError("query_only could not be enabled")
            # SQLite virtual-table modules are registered per connection.
            # Loading sqlite-vec is required to inspect/query notes_vec; this
            # read-only view never creates or repairs the virtual table.
            import sqlite_vec

            self._conn.enable_load_extension(True)
            try:
                sqlite_vec.load(self._conn)
            finally:
                self._conn.enable_load_extension(False)
        except BaseException:
            self._conn.close()
            raise

    def close(self) -> None:
        self._conn.close()


def _table_names(view: ReadOnlyIndexView) -> set[str]:
    rows = view._conn.execute("SELECT name FROM sqlite_master WHERE type IN ('table','view')").fetchall()
    return {row[0] for row in rows}


def validate_index(view: ReadOnlyIndexView) -> str:
    """Validate the existing Core search schema and return its stored model."""
    try:
        tables = _table_names(view)
        if not {"notes", "notes_fts", "notes_vec", "schema_version"}.issubset(tables):
            raise IndexNotReady("index schema missing required notes/FTS/vector metadata")
        columns = {row[1] for row in view._conn.execute("PRAGMA table_info(notes)")}
        if not _NOTES_COLUMNS.issubset(columns):
            raise IndexNotReady("index notes schema is incompatible")
        fts_sql = view._conn.execute(
            "SELECT sql FROM sqlite_master WHERE name='notes_fts'"
        ).fetchone()
        vec_sql = view._conn.execute(
            "SELECT sql FROM sqlite_master WHERE name='notes_vec'"
        ).fetchone()
        if not fts_sql or "fts5" not in (fts_sql[0] or "").lower():
            raise IndexNotReady("index FTS schema is incompatible")
        if not vec_sql or "vec0" not in (vec_sql[0] or "").lower():
            raise IndexNotReady("index vector schema is incompatible")
        # sqlite-vec exposes the virtual-table columns via table_xinfo; unlike
        # table_info this works for hidden virtual-table columns consistently.
        vec_columns = {row[1] for row in view._conn.execute("PRAGMA table_xinfo(notes_vec)")}
        if not {"path", "embedding"}.issubset(vec_columns):
            raise IndexNotReady("index vector columns are incompatible")
        model_row = view._conn.execute(
            "SELECT version FROM schema_version WHERE key='embedding_model'"
        ).fetchone()
        model = model_row[0] if model_row else None
        if not isinstance(model, str) or not model.strip():
            raise IndexNotReady("index embedding model metadata is unavailable")
        return model
    except IndexNotReady:
        raise
    except sqlite3.Error as exc:
        raise IndexNotReady("index schema could not be inspected") from exc


class ExistingIndexReader:
    """Run the unchanged Core hybrid algorithm behind adapter-local readiness gates."""

    def __init__(self, engine: Any):
        self.engine = engine
        self.storage = engine.storage
        self.model_name = engine.model_name
        self.view = self.storage
        stored_model = validate_index(self.view)
        if stored_model != self.model_name:
            raise IndexNotReady("index embedding model does not match search engine")
        if engine._vec_enabled is not True:
            raise IndexNotReady("Core vector search is not enabled")

    def search(
        self, query: str, scope: str | None = None, limit: int = 10,
        mode: str = "preview", *, fts_mode: str = "any", include_closed: bool = True,
        log_ctx: Any = None, stats: dict | None = None,
    ) -> list[dict]:
        if self.engine._vec_enabled is not True:
            raise IndexNotReady("Core vector search is not enabled")
        try:
            stored_model = validate_index(self.view)
            if stored_model != self.model_name:
                raise IndexNotReady("index embedding model changed; reopen and rewarm retrieval")
            self.view._conn.execute("BEGIN")
            try:
                if validate_index(self.view) != self.model_name:
                    raise IndexNotReady("index embedding model changed; reopen and rewarm retrieval")
                if self.engine._vec_enabled is not True:
                    raise IndexNotReady("Core vector search is not enabled")
                results = self.engine.search(
                    query=query, scope=scope, limit=limit, mode=mode,
                    fts_mode=fts_mode, include_closed=include_closed,
                    log_ctx=log_ctx, stats=stats,
                )
                if self.engine._vec_enabled is not True:
                    raise IndexNotReady("Core vector search became unavailable")
                self.view._conn.execute("COMMIT")
                return results
            except BaseException:
                if self.view._conn.in_transaction:
                    self.view._conn.execute("ROLLBACK")
                raise
        except IndexNotReady:
            raise
        except Exception as exc:
            raise IndexNotReady("hybrid index lookup failed") from exc
