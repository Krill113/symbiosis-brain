from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING

import numpy as np

if TYPE_CHECKING:
    from symbiosis_brain.storage import Storage

_embedder = None

_SCOPE_BOOST = 1.5
"""Multiplier applied to RRF scores of notes whose scope matches the query scope.

Promotes scope-specific matches above otherwise-equal global matches when a
non-global scope filter is set. RRF scores for adjacent top ranks sit around
1/(60+1) ≈ 0.016; a 1.5× boost is large enough to flip ties and small gaps
without overwhelming genuinely stronger matches from the global pool.
Tunable — see `docs/superpowers/plans/2026-04-21-w4-lint-data-hygiene.md`.
"""


def _extract_fallback_gist(content: str, max_chars: int = 80) -> str:
    """Extract first non-empty paragraph after frontmatter+heading, ≤max_chars.

    Used as fallback when frontmatter has no `gist:` field.
    """
    lines = content.split("\n")
    in_frontmatter = False
    in_para = False
    para_lines: list[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped == "---":
            in_frontmatter = not in_frontmatter
            continue
        if in_frontmatter:
            continue
        if not stripped:
            if in_para:
                break  # paragraph ended
            continue
        if stripped.startswith("#"):
            continue  # skip headings
        para_lines.append(stripped)
        in_para = True
    paragraph = " ".join(para_lines).strip()
    if len(paragraph) > max_chars:
        cut = paragraph[:max_chars]
        if " " in cut:
            cut = cut.rsplit(" ", 1)[0]
        paragraph = cut
    return paragraph


def _get_embedder():
    global _embedder
    if _embedder is None:
        from fastembed import TextEmbedding
        _embedder = TextEmbedding(model_name="BAAI/bge-small-en-v1.5")
    return _embedder


def _embed(texts: list[str]) -> list[list[float]]:
    embedder = _get_embedder()
    return [e.tolist() for e in embedder.embed(texts)]


def _embed_one(text: str) -> list[float]:
    return _embed([text])[0]


class SearchEngine:
    def __init__(self, storage: Storage):
        self.storage = storage
        self._vec_enabled = self._try_load_vec()

    def _try_load_vec(self) -> bool:
        try:
            import sqlite_vec
            self.storage._conn.enable_load_extension(True)
            sqlite_vec.load(self.storage._conn)
            self.storage._conn.enable_load_extension(False)
            self._ensure_vec_table()
            return True
        except Exception:
            return False

    def _ensure_vec_table(self):
        tables = self.storage.list_tables()
        if "notes_vec" not in tables:
            self.storage._conn.execute("""
                CREATE VIRTUAL TABLE notes_vec USING vec0(
                    path TEXT PRIMARY KEY,
                    embedding FLOAT[384]
                )
            """)
            self.storage._conn.commit()

    def index_note(self, path: str, content: str):
        if not self._vec_enabled:
            return
        embedding = _embed_one(content)
        self.storage._conn.execute("DELETE FROM notes_vec WHERE path=?", (path,))
        self.storage._conn.execute(
            "INSERT INTO notes_vec (path, embedding) VALUES (?, ?)",
            (path, np.array(embedding, dtype=np.float32).tobytes()),
        )
        self.storage._conn.commit()

    def index_all(self):
        if not self._vec_enabled:
            return
        notes = self.storage.list_notes()
        if not notes:
            return
        self.storage._conn.execute("DELETE FROM notes_vec")
        texts = [f"{n['title']}\n{n['content']}" for n in notes]
        embeddings = _embed(texts)
        for note, emb in zip(notes, embeddings):
            self.storage._conn.execute(
                "INSERT INTO notes_vec (path, embedding) VALUES (?, ?)",
                (note["path"], np.array(emb, dtype=np.float32).tobytes()),
            )
        self.storage._conn.commit()

    @staticmethod
    def _sanitize_fts_query(query: str) -> str:
        """Escape user input for FTS5 MATCH.

        Strips FTS5 operators and wraps each token in double quotes
        so that characters like hyphens, dots, and colons are treated
        as literals, not syntax.
        """
        import re
        # Remove characters that are FTS5 operators or break the parser
        cleaned = re.sub(r'["\(\)\*\:\.\{\}\[\]\^\~\|]', ' ', query)
        tokens = cleaned.split()
        if not tokens:
            return '""'
        return " ".join(f'"{t}"' for t in tokens)

    def search_fts(self, query: str, scope: str | None = None, limit: int = 10) -> list[dict]:
        fts_query = self._sanitize_fts_query(query)
        if scope:
            rows = self.storage._conn.execute("""
                SELECT n.*, bm25(notes_fts, 10, 1, 1) as rank
                FROM notes_fts fts
                JOIN notes n ON n.rowid = fts.rowid
                WHERE notes_fts MATCH ? AND n.scope IN (?, 'global')
                ORDER BY rank
                LIMIT ?
            """, (fts_query, scope, limit)).fetchall()
        else:
            rows = self.storage._conn.execute("""
                SELECT n.*, bm25(notes_fts, 10, 1, 1) as rank
                FROM notes_fts fts
                JOIN notes n ON n.rowid = fts.rowid
                WHERE notes_fts MATCH ?
                ORDER BY rank
                LIMIT ?
            """, (fts_query, limit)).fetchall()
        return [self.storage._row_to_note(r) for r in rows]

    def search_vector(self, query: str, scope: str | None = None, limit: int = 10) -> list[dict]:
        if not self._vec_enabled:
            return []
        q_emb = _embed_one(query)
        rows = self.storage._conn.execute("""
            SELECT v.path, v.distance
            FROM notes_vec v
            WHERE v.embedding MATCH ?
            ORDER BY v.distance
            LIMIT ?
        """, (np.array(q_emb, dtype=np.float32).tobytes(), limit * 2)).fetchall()

        results = []
        for row in rows:
            note = self.storage.get_note(row[0])
            if note and (scope is None or note["scope"] in (scope, "global")):
                note["_distance"] = row[1]
                results.append(note)
                if len(results) >= limit:
                    break
        return results

    def search(self, query: str, scope: str | None = None, limit: int = 10,
               mode: str = "preview") -> list[dict]:
        """Hybrid search: FTS5 + vector with Reciprocal Rank Fusion.

        mode='preview' (default) — returns notes with full content for legacy callers.
        mode='gist' — adds 'gist' key (frontmatter['gist'] or fallback 80-char paragraph).
        """
        fts_results = self.search_fts(query, scope=scope, limit=limit * 2)
        vec_results = self.search_vector(query, scope=scope, limit=limit * 2)

        scores: dict[str, float] = {}
        k = 60  # RRF constant

        for rank, note in enumerate(fts_results):
            scores[note["path"]] = scores.get(note["path"], 0) + 1.0 / (k + rank + 1)

        for rank, note in enumerate(vec_results):
            scores[note["path"]] = scores.get(note["path"], 0) + 1.0 / (k + rank + 1)

        all_notes = {n["path"]: n for n in fts_results + vec_results}

        if scope and scope != "global":
            for path in scores:
                note = all_notes.get(path)
                if note and note.get("scope") == scope:
                    scores[path] *= _SCOPE_BOOST

        sorted_paths = sorted(scores, key=lambda p: scores[p], reverse=True)
        results = [all_notes[p] for p in sorted_paths[:limit]]

        if mode == "gist":
            for note in results:
                fm = note.get("frontmatter") or {}
                gist = fm.get("gist", "").strip() if isinstance(fm, dict) else ""
                if not gist:
                    gist = _extract_fallback_gist(note["content"], max_chars=80)
                note["gist"] = gist

        return results
