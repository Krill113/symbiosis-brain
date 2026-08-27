from __future__ import annotations

import hashlib
import logging
import os
import sqlite3
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np

if TYPE_CHECKING:
    from symbiosis_brain.storage import Storage

from symbiosis_brain import retrieval_log
from symbiosis_brain.retrieval_log import LogContext

logger = logging.getLogger("symbiosis-brain.search")

_MODEL_NAME = "BAAI/bge-small-en-v1.5"
_EMBED_BATCH_SIZE = 16
"""fastembed's silent default is batch_size=256. With 512-token padded
documents that peaks the onnxruntime CPU arena at ~11 GB on a ~1300-note
corpus; the arena is never returned to the OS. batch_size=16 measured
identical throughput (518 vs 544 CPU-s) at ~1.1 GB peak (2026-08-10)."""
_embedder = None

LOCK_DIR = Path(tempfile.gettempdir())
_FASTEMBED_LOCK_TIMEOUT_S = 120

_REINDEX_LOCK_WAIT_S = 180
"""How long we queue for the reindex lock before proceeding unguarded.
Giving up costs duplicated work — exactly the pre-fix behaviour — never a
hang."""

_REINDEX_LOCK_STALE_S = 1800
"""When an unattended lock file is broken as abandoned. Orphaned locks are
the NORMAL case, not the exception: _run_server force-exits via os._exit(0)
(server.py:870) whenever the parent Claude window dies, so a holder that is
mid-index_all never runs its finally."""

_SCOPE_BOOST = 1.5
"""Multiplier applied to RRF scores of notes whose scope matches the query scope.

Promotes scope-specific matches above otherwise-equal global matches when a
non-global scope filter is set. RRF scores for adjacent top ranks sit around
1/(60+1) ≈ 0.016; a 1.5× boost is large enough to flip ties and small gaps
without overwhelming genuinely stronger matches from the global pool.
Tunable — see `docs/superpowers/plans/2026-04-21-w4-lint-data-hygiene.md`.
"""

FTS_MODE_ANY = "any"
"""Лексическая половина: OR по токенам — «хотя бы одно слово»."""

FTS_MODE_ALL = "all"
"""Лексическая половина: AND по токенам. Историческое поведение и дефолт
_sanitize_fts_query/search_fts: неизвестный режим не должен молча РАСШИРЯТЬ
выдачу."""

FTS_MODE_ALL_THEN_ANY = "all_then_any"
"""AND, а при нуле строк лексической половины — повтор в OR.

Только ЗАПРОШЕННЫЙ режим: наружу (в `stats` и в журнал) уходит ИСХОД —
FTS_MODE_ALL или FTS_EFFECTIVE_FALLBACK_ANY (§2.9, §4.2). Замер 26.08 на 1666
живых запросах: на инжект-путях этот режим вырождается в OR в 97-99 % случаев,
поэтому митигацией precision он не считается (§12, риск 6)."""

FTS_EFFECTIVE_FALLBACK_ANY = "fallback_any"
"""ИСХОД, а не режим запроса: AND дал ноль строк, и мы повторили в OR.
Легально только в `stats["fts_mode"]` и в колонке `retrieval_event.fts_mode`;
передавать это значение в `search(fts_mode=…)` нельзя — там оно трактуется как
неизвестный режим, то есть как AND."""

_STRONG_RANK_MAX = 3
"""★ = нота в топ-3 ОБЕИХ половин (§4.5, I-22). Замер на реальном профиле
рендера PreToolUse (пул 6, кап 3, 142 живых запроса): 3,5 % показанных хитов
против 27,5 % у прежнего `_in_both`. Порог зависит от размера пула — меняя
`hit_limit`, перечитай [[decisions/2026-08-26-recall-strength-mark]]."""

OVERFETCH_FACTORS = (2, 8, 32)
"""Множители `limit` для KNN в scoped-выдаче (I-20, §4.4).

vec0 не умеет предфильтр: `notes_vec` объявлена как
`vec0(path TEXT PRIMARY KEY, embedding FLOAT[384])` (search.py:245-254), метаданной
для партиционирования там нет, и джойн с `notes` по scope даёт побайтно тот же
результат, что постфильтр — замерено (EXPLAIN QUERY PLAN: SCAN v VIRTUAL TABLE,
затем SEARCH n). Поэтому лестница: берём глобальный топ-k, фильтруем, и при
недоборе увеличиваем k. Первый шаг равен сегодняшнему `limit * 2`, то есть
незаскоупленная выдача поведения не меняет. Цена третьего шага честная:
limit*32 при limit=12 — KNN на 384 кандидата, 2,3-6,3 мс против 10,2 мс всей
векторной половины ([отчёт 03, F31]); он срабатывает только на узких скоупах."""


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
    if _embedder is not None:
        return _embedder

    lockfile = LOCK_DIR / "sb-fastembed-init.lock"
    deadline = time.time() + _FASTEMBED_LOCK_TIMEOUT_S
    acquired = False

    while not acquired:
        try:
            fd = os.open(str(lockfile), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
            with os.fdopen(fd, "w") as f:
                f.write(f"{os.getpid()}\n{int(time.time())}\n")
            acquired = True
        except FileExistsError:
            try:
                age = time.time() - lockfile.stat().st_mtime
            except FileNotFoundError:
                continue
            except OSError:
                # Unreadable rather than gone: on Windows a live handle raises
                # PermissionError (WinError 32), which IS an OSError but NOT a
                # FileNotFoundError — pre-fix it escaped and killed the cold
                # start. Treat the lock as fresh and fall through to the wait.
                # NEVER `continue` here: a persistent error would busy-loop.
                logger.warning("fastembed lock: cannot stat lock file", exc_info=True)
                age = 0.0
            if age > _FASTEMBED_LOCK_TIMEOUT_S:
                try:
                    lockfile.unlink()
                    continue
                except FileNotFoundError:
                    continue
                except OSError:
                    # Undeletable, not absent — same Windows shape as above.
                    # Fall through to the wait instead of retrying immediately.
                    logger.warning("fastembed lock: cannot remove stale lock", exc_info=True)
            if time.time() >= deadline:
                # Lock held too long — give up, attempt unguarded init.
                # Worst case: parallel cold-starts compete; not a hang.
                break
            time.sleep(0.1)

    try:
        if _embedder is None:
            from fastembed import TextEmbedding
            _embedder = TextEmbedding(model_name=_MODEL_NAME)
    finally:
        if acquired:
            try:
                lockfile.unlink()
            except OSError:
                # Cleanup is best-effort: a stale file is broken later by the
                # staleness check, but an exception here would replace whatever
                # the guarded body raised.
                pass
    return _embedder


@contextmanager
def _reindex_lock(db_path):
    """Cross-process single-flight for reindex work on one vault DB.

    Same O_CREAT|O_EXCL idiom as _get_embedder's model lock. Not reentrant —
    never call while already holding it. On wait-timeout we proceed unguarded
    (worst case: duplicated work, never a hang). Callers MUST re-check their
    trigger condition after acquiring — the previous holder usually just did
    the work we queued up for. No PID-liveness probing: os.kill(pid, 0) on
    Windows calls TerminateProcess.
    """
    tag = hashlib.sha256(str(db_path).encode()).hexdigest()[:12]
    lockfile = LOCK_DIR / f"sb-reindex-{tag}.lock"
    deadline = time.time() + _REINDEX_LOCK_WAIT_S
    acquired = False
    while not acquired:
        try:
            fd = os.open(str(lockfile), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
            with os.fdopen(fd, "w") as f:
                f.write(f"{os.getpid()}\n{int(time.time())}\n")
            acquired = True
        except FileExistsError:
            try:
                mtime = lockfile.stat().st_mtime
            except FileNotFoundError:
                continue
            except OSError:
                # Unreadable rather than gone — a live handle on Windows raises
                # PermissionError, an OSError but not a FileNotFoundError, and it
                # used to escape this loop and kill the caller mid-startup. Treat
                # the lock as fresh and fall through to the wait below; never
                # `continue` here, or a persistent error busy-loops the thread.
                logger.warning("reindex lock: cannot stat lock file", exc_info=True)
                mtime = time.time()
            if time.time() - mtime > _REINDEX_LOCK_STALE_S:
                # Re-stat immediately before unlinking: if the mtime moved
                # since we judged staleness, another process already broke
                # and re-acquired this lock — it is now live, not abandoned.
                # Skip the unlink and re-evaluate from scratch.
                try:
                    if lockfile.stat().st_mtime == mtime:
                        lockfile.unlink()
                        logger.warning(
                            "reindex lock: broke stale lock (age > %ds)",
                            _REINDEX_LOCK_STALE_S)
                    continue
                except FileNotFoundError:
                    continue
                except OSError:
                    # Undeletable (live handle). Fall through to the wait.
                    logger.warning("reindex lock: cannot break stale lock", exc_info=True)
            if time.time() >= deadline:
                break
            time.sleep(0.5)
    if not acquired:
        logger.warning(
            "reindex lock: gave up waiting after %ds — proceeding unguarded (pid=%d)",
            _REINDEX_LOCK_WAIT_S, os.getpid())
    try:
        yield
    finally:
        if acquired:
            try:
                lockfile.unlink()
            except OSError:
                # Best-effort release: an exception raised inside `finally`
                # would replace the exception the guarded body raised (B6c).
                pass


def _embed(texts: list[str]) -> list[list[float]]:
    embedder = _get_embedder()
    return [e.tolist() for e in embedder.embed(texts, batch_size=_EMBED_BATCH_SIZE)]


def _embed_one(text: str) -> list[float]:
    return _embed([text])[0]


class SearchEngine:
    def __init__(self, storage: Storage):
        self.storage = storage
        self._vec_enabled = self._try_load_vec()

    @property
    def _model_name(self) -> str:
        return _MODEL_NAME

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
        # BEGIN IMMEDIATE serializes concurrent indexers on the same path.
        # vec0 does not support INSERT OR REPLACE, so DELETE + INSERT is the
        # only upsert pattern — wrapping it in an exclusive transaction prevents
        # the UNIQUE-constraint race that arises when two processes index the
        # same note simultaneously (e.g. parallel cold-starts on a fresh vault).
        self.storage._conn.execute("BEGIN IMMEDIATE")
        try:
            self.storage._conn.execute("DELETE FROM notes_vec WHERE path=?", (path,))
            self.storage._conn.execute(
                "INSERT INTO notes_vec (path, embedding) VALUES (?, ?)",
                (path, np.array(embedding, dtype=np.float32).tobytes()),
            )
            self.storage._conn.execute("COMMIT")
        except Exception:
            try:
                self.storage._conn.execute("ROLLBACK")
            except Exception:
                pass
            raise

    def index_all(self):
        """Full rebuild of notes_vec from notes. Callers must hold
        _reindex_lock(storage.db_path)."""
        if not self._vec_enabled:
            return
        notes = self.storage.list_notes()
        if not notes:
            return
        texts = [f"{n['title']}\n{n['content']}" for n in notes]
        embeddings = _embed(texts)
        # BEGIN IMMEDIATE serializes concurrent full rebuilds. Without this,
        # two parallel cold-starts both see a dirty index and both run the
        # DELETE + multi-INSERT loop, causing UNIQUE-constraint violations.
        self.storage._conn.execute("BEGIN IMMEDIATE")
        try:
            self.storage._conn.execute("DELETE FROM notes_vec")
            for note, emb in zip(notes, embeddings):
                self.storage._conn.execute(
                    "INSERT INTO notes_vec (path, embedding) VALUES (?, ?)",
                    (note["path"], np.array(emb, dtype=np.float32).tobytes()),
                )
            self.storage._conn.execute("COMMIT")
        except Exception:
            try:
                self.storage._conn.execute("ROLLBACK")
            except Exception:
                pass
            raise

    def repair_index(self) -> dict:
        """Reconcile notes_vec with notes incrementally.

        Embeds only notes that lack a vector row and deletes orphan vector
        rows whose note is gone. Unlike index_all(), never touches rows that
        are already correct — a drift of K rows costs K _embed_one calls, not
        a full-corpus rebuild (measured 2026-08-10: drift=1 via index_all
        cost 503 CPU-s / 11.2 GB). The gate for doing nothing is "no missing
        and no orphan rows" (computed inside the lock), not is_index_dirty()'s
        bare count comparison — count alone misses balanced drift, e.g. an
        equal number of missing and orphaned rows from a historical rename,
        which would otherwise never get repaired. Residual race: if a note is
        deleted by another process between our `missing` snapshot and the
        embed, we may re-insert its vec row as an orphan; the next repair
        deletes it.
        """
        if not self._vec_enabled:
            return {"embedded": 0, "orphans_deleted": 0}
        with _reindex_lock(self.storage.db_path):
            missing = [r[0] for r in self.storage._conn.execute(
                "SELECT n.path FROM notes n LEFT JOIN notes_vec v ON v.path = n.path"
                " WHERE v.path IS NULL"
            ).fetchall()]
            orphans = [r[0] for r in self.storage._conn.execute(
                "SELECT v.path FROM notes_vec v LEFT JOIN notes n ON n.path = v.path"
                " WHERE n.path IS NULL"
            ).fetchall()]
            if not missing and not orphans:
                # Another process likely repaired while we queued for the
                # lock — re-checking the actual join lists (not just counts)
                # here is what makes this a correct double-checked-lock gate.
                return {"embedded": 0, "orphans_deleted": 0}
            for path in orphans:
                self.delete_vec(path)
            embedded = 0
            for path in missing:
                note = self.storage.get_note(path)
                if note is None:
                    continue
                self.index_note(path, f"{note['title']}\n{note['content']}")
                embedded += 1
            return {"embedded": embedded, "orphans_deleted": len(orphans)}

    def delete_vec(self, path: str) -> None:
        """Remove the vector embedding for a single note path."""
        if not self._vec_enabled:
            return
        self.storage._conn.execute("DELETE FROM notes_vec WHERE path=?", (path,))
        self.storage._conn.commit()

    def is_index_dirty(self) -> bool:
        """True if count(notes) != count(notes_vec). Cheap O(1) drift check."""
        if not self._vec_enabled:
            return False
        n = self.storage._conn.execute("SELECT COUNT(*) FROM notes").fetchone()[0]
        v = self.storage._conn.execute("SELECT COUNT(*) FROM notes_vec").fetchone()[0]
        return n != v

    def has_index_delta(self) -> bool:
        """True if any note lacks a notes_vec row OR any notes_vec row has no note.

        Two cheap `SELECT 1 ... LEFT JOIN ... WHERE ... IS NULL LIMIT 1` probes —
        the same joins repair_index() computes inside its lock. Unlike
        is_index_dirty()'s bare COUNT comparison this catches BALANCED drift
        (an equal number of missing and orphaned rows — the classic residue of a
        historical rename), which repair_index() is built to fix but the old
        count gate never let it see (B6a).

        Never raises: any DB error -> False. This runs on the cold-start path,
        where a failing drift probe must not take the whole server down with it.
        """
        if not self._vec_enabled:
            return False
        try:
            missing = self.storage._conn.execute(
                "SELECT 1 FROM notes n LEFT JOIN notes_vec v ON v.path = n.path"
                " WHERE v.path IS NULL LIMIT 1"
            ).fetchone()
            if missing is not None:
                return True
            orphan = self.storage._conn.execute(
                "SELECT 1 FROM notes_vec v LEFT JOIN notes n ON n.path = v.path"
                " WHERE n.path IS NULL LIMIT 1"
            ).fetchone()
            return orphan is not None
        except Exception:
            logger.warning("has_index_delta: probe failed, assuming clean", exc_info=True)
            return False

    @staticmethod
    def _sanitize_fts_query(query: str, mode: str = FTS_MODE_ALL) -> str:
        """Escape user input for FTS5 MATCH.

        Strips FTS5 operators and wraps each token in double quotes so that
        characters like hyphens, dots, and colons are treated as literals, not
        syntax. `mode` decides how the quoted tokens are joined:

        - FTS_MODE_ANY  -> ' OR ' between them: at least one token must match;
        - anything else -> a bare space, i.e. FTS5's implicit AND.

        AND stays the default on purpose: an unknown or stub mode must never
        silently WIDEN a query. Measured 2026-08-26 on 1666 live queries: the
        implicit AND returned zero rows for 84.5 % of them (94.3 % of the
        Russian ones), which is what FTS_MODE_ANY exists to fix (§4.1).

        Quoting also disarms the OR keyword itself: a user token `OR` arrives
        as `"OR"` and matches literally, never as an operator.
        """
        import re
        # Remove characters that are FTS5 operators or break the parser
        cleaned = re.sub(r'["\(\)\*\:\.\{\}\[\]\^\~\|]', ' ', query)
        tokens = cleaned.split()
        if not tokens:
            return '""'
        quoted = [f'"{t}"' for t in tokens]
        if mode == FTS_MODE_ANY:
            return " OR ".join(quoted)
        return " ".join(quoted)

    def search_fts(self, query: str, scope: str | None = None, limit: int = 10,
                   *, mode: str = FTS_MODE_ALL) -> list[dict]:
        fts_query = self._sanitize_fts_query(query, mode)
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
        """Vector half. Signature unchanged (I-20) — the escalation is internal.

        A scoped call cannot be pre-filtered by vec0, so we over-fetch: pull the
        global top-k, post-filter by scope, and grow k when the scope came up
        short. Exactly two stop conditions — `limit` collected, or the corpus
        exhausted (`k >= count(notes_vec)`). Measured before this change: 25 %
        of scoped queries returned fewer notes than asked ([report 03, F21]).
        """
        if not self._vec_enabled:
            return []
        q_blob = np.array(_embed_one(query), dtype=np.float32).tobytes()
        try:
            corpus = self.storage._conn.execute(
                "SELECT COUNT(*) FROM notes_vec"
            ).fetchone()[0]
        except Exception:
            # Fail-safe, not fail-open-wide: an unknown corpus size collapses the
            # ladder to its first rung, i.e. exactly the pre-Stage-2 behaviour.
            corpus = 0
        # Notes are memoised across rungs: without it the third rung re-reads
        # every path the first two already fetched.
        notes_by_path: dict[str, dict | None] = {}
        results: list[dict] = []
        for factor in OVERFETCH_FACTORS:
            k = max(1, limit * factor)
            rows = self.storage._conn.execute("""
                SELECT v.path, v.distance
                FROM notes_vec v
                WHERE v.embedding MATCH ?
                ORDER BY v.distance
                LIMIT ?
            """, (q_blob, k)).fetchall()

            results = []
            for row in rows:
                path = row[0]
                if path not in notes_by_path:
                    notes_by_path[path] = self.storage.get_note(path)
                note = notes_by_path[path]
                if note and (scope is None or note["scope"] in (scope, "global")):
                    note["_distance"] = row[1]
                    results.append(note)
                    if len(results) >= limit:
                        break
            if len(results) >= limit:
                break
            if k >= corpus:
                break
        return results

    def search(self, query: str, scope: str | None = None, limit: int = 10,
               mode: str = "preview", *, fts_mode: str = FTS_MODE_ANY,
               log_ctx: "LogContext | None" = None,
               stats: dict | None = None) -> list[dict]:
        """Hybrid search: FTS5 + vector with Reciprocal Rank Fusion.

        mode='preview' (default) — returns notes with full content for legacy callers.
        mode='gist' — adds 'gist' key (frontmatter['gist'] or fallback 80-char paragraph).

        fts_mode picks the LEXICAL half's token policy (I-17..I-19). What leaves
        this function — through `stats` and through the journal — is the
        EFFECTIVE mode: 'any' | 'all' | 'fallback_any'. The requested
        'all_then_any' is never written anywhere (§2.9, §4.2): the share of
        'fallback_any' IS the metric, and substituting the request for the
        outcome empties it silently.

        log_ctx (I-7): when given, this call IS the surfacing point and the
        retrieval log is written here, after _score/_in_both are attached and
        before the return. Used by `mcp_search` and `legacy_gist` only — the
        hook paths pass log_ctx=None and log from `run_recall` instead, because
        what the agent sees there is the list AFTER type filtering, SeenStore
        and the cap (§2.9). Default None keeps today's behaviour: nobody logs.

        stats (I-7): a write-only back channel for whoever logs from outside.
        Filled with EXACTLY two keys before every return, including an empty
        result — those are the cases the metric exists for. `fts_mode` is the
        EFFECTIVE mode, `vec_enabled` is the only legal source of the NOT NULL
        column of the same name on the hook paths (§2.9, I-8).
        """
        t0 = time.perf_counter()
        if fts_mode == FTS_MODE_ALL_THEN_ANY:
            fts_results = self.search_fts(query, scope=scope, limit=limit * 2,
                                          mode=FTS_MODE_ALL)
            if fts_results:
                effective_fts_mode = FTS_MODE_ALL
            else:
                fts_results = self.search_fts(query, scope=scope, limit=limit * 2,
                                              mode=FTS_MODE_ANY)
                effective_fts_mode = FTS_EFFECTIVE_FALLBACK_ANY
        else:
            # Unknown values fall back to AND, never to OR — see _sanitize_fts_query.
            effective_fts_mode = FTS_MODE_ANY if fts_mode == FTS_MODE_ANY else FTS_MODE_ALL
            fts_results = self.search_fts(query, scope=scope, limit=limit * 2,
                                          mode=effective_fts_mode)
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

        # Attach ranking metadata (Stage 0, recall-hardening; Stage 2, I-22).
        # Purely additive, underscore-prefixed (cf. _distance), visible to both
        # preview and gist callers. _score is post-boost RRF. _in_both means
        # the note surfaced in BOTH halves and KEEPS its old meaning — existing
        # tests stand on it and the journal logs it (retrieval_hit.in_both).
        # _strong is the NEW label behind ★: top-_STRONG_RANK_MAX in both
        # halves (§4.5). Neither is ever a filter
        # (see [[decisions/2026-06-03-recall-behavior]]).
        fts_rank_by_path: dict[str, int] = {}
        for rank, note in enumerate(fts_results):
            fts_rank_by_path.setdefault(note["path"], rank)
        vec_rank_by_path: dict[str, int] = {}
        for rank, note in enumerate(vec_results):
            vec_rank_by_path.setdefault(note["path"], rank)
        for note in results:
            note["_score"] = float(scores.get(note["path"], 0.0))
            fts_rank = fts_rank_by_path.get(note["path"])
            vec_rank = vec_rank_by_path.get(note["path"])
            note["_fts_rank"] = fts_rank
            note["_vec_rank"] = vec_rank
            note["_in_both"] = fts_rank is not None and vec_rank is not None
            note["_strong"] = (
                fts_rank is not None and fts_rank < _STRONG_RANK_MAX
                and vec_rank is not None and vec_rank < _STRONG_RANK_MAX
            )

        if mode == "gist":
            for note in results:
                fm = note.get("frontmatter") or {}
                gist = fm.get("gist", "").strip() if isinstance(fm, dict) else ""
                if not gist:
                    gist = _extract_fallback_gist(note["content"], max_chars=80)
                note["gist"] = gist

        latency_ms = int((time.perf_counter() - t0) * 1000)

        # This is the single return of search(). If a future change adds an
        # early one, it MUST fill `stats` and log first — that is the contract,
        # not a courtesy (I-7).
        if stats is not None:
            stats["fts_mode"] = effective_fts_mode
            stats["vec_enabled"] = bool(self._vec_enabled)
        if log_ctx is not None:
            retrieval_log.record(
                log_ctx, query=query, scope=scope, mode=mode,
                fts_mode=effective_fts_mode, hits=results,
                latency_ms=latency_ms, vec_enabled=bool(self._vec_enabled),
            )

        return results
