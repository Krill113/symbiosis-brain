#!/usr/bin/env python3
"""Search-quality eval harness for symbiosis-brain (Stage 2, spec 7.1, I-32).

Run:

    uv run --directory <repo> python tools/eval_search.py \
        --vault <vault> --work-dir <scratch> --queries <queries.jsonl> \
        [--configs fts-all,fts-any,...] [--k 5] [--out results.json] [--model NAME]

Repo tooling, not part of the wheel — same shelf as tools/changelog_section.py.

TWO RULES THAT ARE NOT NEGOTIABLE (I-32):

1. The live database is opened READ-ONLY and copied with `VACUUM INTO`. Copying
   brain.db{,-wal,-shm} file by file gives a TORN snapshot: a checkpoint can land
   between two copies (PRAGMA wal_autocheckpoint=200, storage.py:34) and the -wal
   file then no longer matches the main file. The `?mode=ro` URI idiom is lifted
   from sqlite_health.py:79 — such a connection cannot create or write the
   database it was pointed at.
2. The model name lives in module singletons (search._MODEL_NAME, search.py:20;
   search._embedder, search.py:26; lazy init at search.py:130-133), so "just pass
   the name" has nowhere to go. The order is the contract: set the name -> drop
   the warm embedder -> reindex the COPY -> measure. The work dir is rebuilt from
   the snapshot on EVERY run, which is what keeps two models from ever sharing
   one notes_vec.

Nothing here writes to the vault, to the live database, or to the retrieval log:
SearchEngine.search() only records when it is handed a LogContext (I-7), and this
harness never builds one.
"""
from __future__ import annotations

import argparse
import json
import math
import sqlite3
import statistics
import sys
import textwrap
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

K_DEFAULT = 5

GRADE_READ = 2
"""`read_after` carries the path: shown AND read next (spec 7.2, positive)."""

GRADE_SHOWN = 0
"""Shown and ignored — a weak negative, NOT an absence of judgement."""

SYSTEM_QUERY_PREFIXES = ("<task-notification>", "<system-reminder>", "<local-command-")
"""hook-prompt rows whose "query" is harness plumbing rather than a human prompt.
mine_queries.py:203 recovers the prompt by walking back to the nearest user-role
message, and a task notification IS a user-role message."""

DEFAULT_CONFIGS = (
    "fts-all", "fts-any", "vec-current",
    "hybrid-all", "hybrid-any", "hybrid-all-then-any",
)
"""The six cheap configurations of spec 7.3 — no download, no reindex, no network.
The four paid ones are the same ids run again under --model (CP-8b, owner's go)."""

RERANK_MODEL = "BAAI/bge-reranker-base"
"""The only multilingual cross-encoder in fastembed 0.8.0 with a permissive
licence (mit, 1.04 GB). The Jina rerankers are cc-by-nc-4.0 and therefore
unusable in an Apache-2.0 package — see the non-goals in spec 7.3."""

RERANK_OVERFETCH = 4
"""The rerank configuration fuses k * RERANK_OVERFETCH hybrid hits and lets the
cross-encoder pick k of them. Reranking the k that fusion already chose would
measure nothing: the set it reorders would be the answer itself."""

E5_PREFIX_MODELS = ("intfloat/multilingual-e5-large",)
"""Models that need "query: " / "passage: " prefixes to perform as published.
index_all builds its document text inside search.py (search.py:287) and this
checkpoint does not touch search.py, so documents go in UNPREFIXED unless
--doc-prefix/--query-prefix are given explicitly (E2, lead directive §3).
Without them the number for such a model is a LOWER BOUND — say so in the
report; do not quietly compare it with the others as if it were the same
measurement."""

_cross_encoder_singleton = None
_rerank_model_name = RERANK_MODEL
"""Process-lifetime choice of rerank model (E1, lead directive §3):
_cross_encoder() takes no argument — the spec's own test suite monkeypatches
it as a zero-arg callable — so the chosen model travels through this module
global instead, exactly like sb_search._MODEL_NAME travels the embedder
choice. run_eval sets it once, before any rerank config runs."""

CAVEATS = (
    "1. The label set favours the LIVE configuration (spec 4.3). read_after was collected from "
    "transcripts in which hybrid-all did the showing, so every other configuration is punished for "
    "surfacing something else. Read recall/MRR/nDCG as \"no worse / no better\", never as absolute "
    "truth; the decisive number is the label-independent lexical zero-hit rate printed above.",

    "2. read_after is joined WITHOUT a session_id (spec 7.2). Server-side paths never carry one, so "
    "with two windows open a read from window A can be credited to a hit from window B. Two "
    "mitigations are mandatory for any journal-derived set: a join window of at most 10 minutes, and "
    "metrics counted only over intervals in which a single window was active, with the share of "
    "excluded overlapping intervals printed here. In THIS transcript-derived set the join is "
    "per-transcript over 0-10 assistant turns (mine_queries.py), which bounds the ambiguity without "
    "removing it for subagent transcripts owned by the same main session.",

    "3. Hook queries in this set are RECONSTRUCTIONS, not the text the engine saw (plan section 2, "
    "F7). hook-pretool was recovered from the [recall: N hits for \"...\"] block, whose snippet the "
    "renderer cuts at 60 characters (pre_action_recall.py:112) while the query itself is built up to "
    "query_max_chars=500 (pre_action_config.py:99); hook-prompt is the first 200 characters of the "
    "prompt text (mine_queries.py:203,257). The real query text appears for the first time in the "
    "retrieval log (cap QUERY_LOG_MAX_CHARS=2000, I-1/I-4), so every cut by `source` over hook rows "
    "is an estimate on a shortened query.",
)


class EvalError(RuntimeError):
    """Anything the operator can fix: a missing vault, an unreadable set, a bad
    config id, a database that cannot be opened read-only."""


@dataclass(frozen=True)
class Config:
    name: str
    kind: str            # "fts" | "vector" | "hybrid"
    fts_mode: str | None


@dataclass
class Run:
    """One query under one configuration."""
    row: dict[str, Any]
    ranked: list[str]
    latency_ms: float
    effective_fts_mode: str | None


def configs() -> dict[str, Config]:
    """The six cheap configurations. The FTS mode names come from search.py
    (I-17) and are imported lazily so `--help` works in a bare checkout."""
    from symbiosis_brain import search as sb_search
    return {
        "fts-all": Config("fts-all", "fts", sb_search.FTS_MODE_ALL),
        "fts-any": Config("fts-any", "fts", sb_search.FTS_MODE_ANY),
        "vec-current": Config("vec-current", "vector", None),
        "hybrid-all": Config("hybrid-all", "hybrid", sb_search.FTS_MODE_ALL),
        "hybrid-any": Config("hybrid-any", "hybrid", sb_search.FTS_MODE_ANY),
        "hybrid-all-then-any": Config(
            "hybrid-all-then-any", "hybrid", sb_search.FTS_MODE_ALL_THEN_ANY),
        # Paid, CP-8b: current embedder + a cross-encoder over an over-fetched pool.
        # Not in DEFAULT_CONFIGS — it downloads 1.04 GB on first use.
        "hybrid-any+rerank": Config("hybrid-any+rerank", "rerank", sb_search.FTS_MODE_ANY),
    }


# --------------------------------------------------------------- snapshot ---

def snapshot_db(live_db: Path, dest: Path) -> None:
    """Consistent copy of a possibly-live SQLite database (I-32 п. 1).

    `?mode=ro` cannot create the file and cannot write to it; VACUUM INTO reads
    the database in one transaction and writes one whole file with no WAL tail.
    """
    live_db = Path(live_db).expanduser().resolve()
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    for suffix in ("", "-wal", "-shm"):
        stale = dest.with_name(dest.name + suffix)
        if stale.exists():
            stale.unlink()

    uri = live_db.as_uri() + "?mode=ro"          # idiom: sqlite_health.py:79
    try:
        src = sqlite3.connect(uri, uri=True, timeout=5)
    except sqlite3.Error as e:
        raise EvalError(
            f"cannot open {live_db} read-only: {e}. A WAL database needs its -shm "
            "file reachable for a read-only connection — start `symbiosis-brain "
            "serve` (or open Claude Code) and run this again. Copying "
            "brain.db/-wal/-shm by hand is NOT a substitute: that copy can be torn."
        ) from e
    try:
        src.execute("VACUUM INTO ?", (str(dest),))
    except sqlite3.Error as e:
        raise EvalError(f"VACUUM INTO {dest} failed: {e}") from e
    finally:
        src.close()


def prepare_work_dir(vault: Path, work_dir: Path) -> Path:
    """Return the path of a private brain.db inside work_dir.

    A live vault (has .index/brain.db) is SNAPSHOTTED. A corpus-only directory —
    the synthetic set under tests/data/eval_search/vault — gets a fresh database
    built from its markdown with VaultSync (sync.py:46).

    The work dir is rebuilt on every run, and that is deliberate: it makes the
    I-32 invariant "between configurations with different models --work-dir is
    recreated from the snapshot" true without an extra flag.
    """
    from symbiosis_brain.storage import Storage
    from symbiosis_brain.sync import VaultSync

    vault = Path(vault)
    if not vault.is_dir():
        raise EvalError(f"{vault} is not a directory")

    work_dir = Path(work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)
    work_db = work_dir / "brain.db"
    for suffix in ("", "-wal", "-shm"):
        stale = work_db.with_name(work_db.name + suffix)
        if stale.exists():
            stale.unlink()

    live_db = vault / ".index" / "brain.db"
    if live_db.exists():
        snapshot_db(live_db, work_db)
        return work_db

    storage = Storage(work_db)
    try:
        result = VaultSync(vault, storage).sync_all()
        if result.failed:
            raise EvalError("vault sync failed on: "
                            + ", ".join(path for path, _ in result.failed))
        if not storage.count_notes():
            raise EvalError(f"{vault} has neither .index/brain.db nor indexable markdown")
    finally:
        storage.close()
    return work_db


# ----------------------------------------------------------- model swap -----

def apply_model_override(model: str) -> None:
    """Point the module singletons at another embedder (I-32 п. 2).

    Order is the whole contract: name FIRST, warm singleton SECOND.
    _get_embedder reads _MODEL_NAME only while _embedder is None
    (search.py:130-133), so clearing first and naming second rebuilds the OLD
    model and every later number is a lie about which model produced it.
    """
    from symbiosis_brain import search as sb_search
    if not model or not model.strip():
        raise EvalError("--model needs a non-empty model name")
    sb_search._MODEL_NAME = model
    sb_search._embedder = None


def rebuild_vector_index(engine, storage, *, model: str, timings: dict | None = None) -> int:
    """Swap the embedder and rebuild notes_vec inside the COPY. Returns the dim.

    notes_vec is declared FLOAT[384] (search.py:245-254), so a model of another
    width needs the table dropped and recreated. The width is PROBED from the
    model rather than taken on trust — that removes a CLI flag and a whole class
    of "wrong dimension, silent garbage" mistakes.

    `engine` must already exist: dropping a vec0 virtual table needs the
    sqlite-vec extension loaded, and SearchEngine.__init__ is what loads it
    (search.py:234-243).

    `timings`, when given, is filled with three separately-measured phases
    (each rounded to 0.1s) — CP-8b follow-up (K.D. 2026-08-27), because a
    single number wrapped around all three used to hide which one actually
    cost the time:
      - model_load_s: building the ONNX session and running the first embed
        (the "dimension probe").
      - lock_wait_s: time spent waiting to ACQUIRE _reindex_lock — up to
        _REINDEX_LOCK_WAIT_S=180s (search.py:35) on a stale lock, and pure
        contention, never actual index work.
      - rebuild_s: engine.index_all() itself, once the lock is held. This is
        the only phase the caller should compare against spec 7.4's 600s cap.
    """
    from symbiosis_brain import search as sb_search

    if not engine._vec_enabled:
        raise EvalError("sqlite-vec is not available — a model swap has nothing to index")

    model_load_started = time.perf_counter()
    apply_model_override(model)
    dim = len(sb_search._embed_one("dimension probe"))
    if timings is not None:
        timings["model_load_s"] = round(time.perf_counter() - model_load_started, 1)
    if dim != 384:
        storage._conn.execute("DROP TABLE IF EXISTS notes_vec")
        storage._conn.commit()
        storage._conn.execute(
            "CREATE VIRTUAL TABLE notes_vec USING vec0("
            f"path TEXT PRIMARY KEY, embedding FLOAT[{dim}])"
        )
        storage._conn.commit()
    lock_wait_started = time.perf_counter()
    with sb_search._reindex_lock(storage.db_path):
        if timings is not None:
            timings["lock_wait_s"] = round(time.perf_counter() - lock_wait_started, 1)
        rebuild_started = time.perf_counter()
        engine.index_all()
        if timings is not None:
            timings["rebuild_s"] = round(time.perf_counter() - rebuild_started, 1)
    return dim


def _vec_count(storage) -> int:
    try:
        row = storage._conn.execute("SELECT COUNT(*) FROM notes_vec").fetchone()
    except sqlite3.Error:
        return 0
    return int(row[0]) if row else 0


def _l2_normalize(vector: list[float]) -> list[float]:
    """v / ‖v‖₂. A zero vector is returned unchanged rather than divided by
    zero — theoretically reachable from a fake embedder in a test, and cheaper
    to special-case than to forbid."""
    norm = math.sqrt(sum(x * x for x in vector))
    return [x / norm for x in vector] if norm else list(vector)


def _install_embed_wrappers(sb_search_module, query_prefix: str | None,
                            doc_prefix: str | None, normalize: bool) -> None:
    """E2/E3 (lead directive §3): wrap _embed / _embed_one so a document/query
    text carries the model's published prefix and/or comes back L2-normalised,
    without touching search.py. Called by open_engine BEFORE rebuild_vector_index,
    so index_all's documents pick up doc_prefix and normalize too; run_eval
    restores the originals in `finally`.

    Composition order is prefix -> embed -> normalize: the prefix changes what
    text the model sees, normalize only rescales what the model hands back.
    E3's reason to exist: fastembed 0.8.0 normalises the output of only some
    models (lead's measurement: bge-small 1.0000, the multilingual candidates
    2.0-28), and notes_vec is declared without distance_metric (search.py:
    245-254) so sqlite-vec scores it by plain L2 — an unnormalised candidate is
    measured with a handicap unless this wrapper is on.

    index_note (search.py:459) also calls _embed_one and would pick up every
    active wrapper too — this harness never calls index_note after a rebuild
    (only reads follow one), so that is acceptable HERE and would NOT be in
    the product.
    """
    orig_embed = sb_search_module._embed
    dp = doc_prefix or ""
    qp = query_prefix or ""

    def _maybe_normalize(vectors):
        return [_l2_normalize(v) for v in vectors] if normalize else vectors

    def wrapped_embed(texts):
        return _maybe_normalize(orig_embed([dp + t for t in texts]))

    def wrapped_embed_one(text):
        return _maybe_normalize(orig_embed([qp + text]))[0]

    sb_search_module._embed = wrapped_embed
    sb_search_module._embed_one = wrapped_embed_one


def open_engine(work_db: Path, *, model: str | None,
                query_prefix: str | None = None, doc_prefix: str | None = None,
                normalize: bool = False, timings: dict | None = None):
    """Open the COPY and return (engine, storage). The caller closes storage.

    query_prefix/doc_prefix/normalize are installed here, before
    rebuild_vector_index, and stay live after this function returns — the
    wrapped _embed_one is what every later search_vector() call goes through
    too. This function never restores them; run_eval owns that, in `finally`.

    `timings`, when given, is forwarded to rebuild_vector_index (see there for
    the keys it fills) — it only matters on the `model` branch, since that is
    the only one that rebuilds anything.
    """
    from symbiosis_brain import search as sb_search
    from symbiosis_brain.storage import Storage

    storage = Storage(Path(work_db))
    try:
        engine = sb_search.SearchEngine(storage)
        if model:
            if query_prefix or doc_prefix or normalize:
                _install_embed_wrappers(sb_search, query_prefix, doc_prefix, normalize)
            rebuild_vector_index(engine, storage, model=model, timings=timings)
        elif engine._vec_enabled and storage.count_notes() and _vec_count(storage) == 0:
            # A database built from markdown (the synthetic set) has no vectors yet.
            with sb_search._reindex_lock(storage.db_path):
                engine.index_all()
    except BaseException:
        storage.close()
        raise
    return engine, storage


# ------------------------------------------------------------- the set ------

def load_queries(path: Path) -> tuple[list[dict[str, Any]], int]:
    """Read a JSONL set (I-33). Returns (rows, dropped).

    Extra fields are kept as they are: the real set carries ts/project_dir/
    session/mode/limit/dup_count on top of I-33 and nothing here depends on them.
    """
    path = Path(path)
    if not path.is_file():
        raise EvalError(f"{path} does not exist")
    rows: list[dict[str, Any]] = []
    dropped = 0
    with path.open("r", encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as e:
                raise EvalError(f"{path}:{lineno}: {e}") from e
            query = (row.get("query") or "").strip()
            if not query or query.startswith(SYSTEM_QUERY_PREFIXES):
                dropped += 1
                continue
            rows.append(row)
    return rows, dropped


def grades_for(row: dict[str, Any]) -> dict[str, int]:
    """Path -> graded relevance for one query (spec 7.2 table).

    A path that never appeared in `shown` gets NO grade and never enters a
    denominator: nothing is known about it, and treating unknown as negative is
    exactly how a biased set turns into a verdict. `gold` (synthetic set only)
    is a hand-made judgement and outranks the transcript proxy.
    """
    grades: dict[str, int] = {}
    for path in row.get("shown") or []:
        grades[path] = GRADE_SHOWN
    for path in row.get("read_after") or []:
        grades[path] = GRADE_READ
    for path in row.get("gold") or []:
        grades[path] = GRADE_READ
    return grades


def _relevant(grades: dict[str, int]) -> set[str]:
    return {path for path, grade in grades.items() if grade >= GRADE_READ}


def recall_at_k(ranked: list[str], grades: dict[str, int], k: int) -> float | None:
    """None when the query carries no positive label — not 0.0."""
    relevant = _relevant(grades)
    if not relevant:
        return None
    return len([p for p in ranked[:k] if p in relevant]) / len(relevant)


def reciprocal_rank(ranked: list[str], grades: dict[str, int], k: int) -> float | None:
    relevant = _relevant(grades)
    if not relevant:
        return None
    for position, path in enumerate(ranked[:k], 1):
        if path in relevant:
            return 1.0 / position
    return 0.0


def ndcg_at_k(ranked: list[str], grades: dict[str, int], k: int) -> float | None:
    """Linear gains (gain = grade), positions discounted by log2(i+1)."""
    positive = {path: grade for path, grade in grades.items() if grade > 0}
    if not positive:
        return None
    dcg = sum(grades.get(path, 0) / math.log2(position + 1)
              for position, path in enumerate(ranked[:k], 1))
    ideal = sorted(positive.values(), reverse=True)[:k]
    idcg = sum(gain / math.log2(position + 1) for position, gain in enumerate(ideal, 1))
    return (dcg / idcg) if idcg else None


def _mean(values: list[float]) -> float:
    return round(sum(values) / len(values), 4) if values else 0.0


def _metrics(runs: list[Run], k: int) -> dict[str, Any]:
    """The I-34 metric block — exactly these six keys, no more.

    `n` counts the JUDGED queries (the denominator of recall/MRR/nDCG);
    `zero_hit_rate` is over every executed query, judged or not, because it is
    the one number that needs no labels at all.
    """
    recalls: list[float] = []
    rrs: list[float] = []
    ndcgs: list[float] = []
    zero = 0
    for run in runs:
        if not run.ranked:
            zero += 1
        grades = grades_for(run.row)
        recall = recall_at_k(run.ranked, grades, k)
        if recall is None:
            continue
        recalls.append(recall)
        rrs.append(reciprocal_rank(run.ranked, grades, k) or 0.0)
        ndcgs.append(ndcg_at_k(run.ranked, grades, k) or 0.0)
    latencies = [run.latency_ms for run in runs]
    return {
        "n": len(recalls),
        "recall_at_k": _mean(recalls),
        "mrr": _mean(rrs),
        "ndcg_at_k": _mean(ndcgs),
        "zero_hit_rate": round(zero / len(runs), 4) if runs else 0.0,
        "p50_latency_ms": round(statistics.median(latencies), 3) if latencies else 0.0,
    }


# ------------------------------------------------------------ retrieval -----

def _cross_encoder():
    """Lazily built, once per process. Downloads _rerank_model_name on first use."""
    global _cross_encoder_singleton
    if _cross_encoder_singleton is None:
        from fastembed.rerank.cross_encoder import TextCrossEncoder
        _cross_encoder_singleton = TextCrossEncoder(model_name=_rerank_model_name)
    return _cross_encoder_singleton


def _rerank(query: str, hits: list[dict[str, Any]], k: int) -> list[dict[str, Any]]:
    """Reorder an over-fetched pool with the cross-encoder. Higher score first."""
    if not hits:
        return []
    documents = [f"{hit.get('title', '')}\n{(hit.get('content') or '')[:512]}"
                 for hit in hits]
    scores = list(_cross_encoder().rerank(query, documents))
    order = sorted(range(len(hits)), key=lambda i: scores[i], reverse=True)
    return [hits[i] for i in order[:k]]


def _run_one(engine, cfg: Config, row: dict[str, Any], k: int) -> Run:
    query = row["query"]
    scope = row.get("scope") or None
    stats: dict[str, Any] = {}
    started = time.perf_counter()
    if cfg.kind == "fts":
        hits = engine.search_fts(query, scope=scope, limit=k, mode=cfg.fts_mode)
    elif cfg.kind == "vector":
        hits = engine.search_vector(query, scope=scope, limit=k)
    elif cfg.kind == "rerank":
        pool = engine.search(query, scope=scope, limit=k * RERANK_OVERFETCH,
                             fts_mode=cfg.fts_mode, stats=stats)
        hits = _rerank(query, pool, k)
    else:
        hits = engine.search(query, scope=scope, limit=k, fts_mode=cfg.fts_mode, stats=stats)
    latency_ms = (time.perf_counter() - started) * 1000.0
    return Run(
        row=row,
        ranked=[hit["path"] for hit in hits],
        latency_ms=latency_ms,
        effective_fts_mode=stats.get("fts_mode"),
    )


def evaluate(engine, cfg: Config, rows: list[dict[str, Any]], k: int
             ) -> tuple[dict[str, Any], list[Run]]:
    """One configuration over the whole set. Returns (I-34 result, runs)."""
    runs = [_run_one(engine, cfg, row, k) for row in rows]

    result: dict[str, Any] = {"config": cfg.name}
    result.update(_metrics(runs, k))
    buckets: dict[str, dict[str, Any]] = {}
    for prefix in ("lang", "source"):
        groups: dict[str, list[Run]] = {}
        for run in runs:
            groups.setdefault(str(run.row.get(prefix) or "unknown"), []).append(run)
        for value, group in sorted(groups.items()):
            buckets[f"{prefix}:{value}"] = _metrics(group, k)
    result["buckets"] = buckets
    return result, runs


def lexical_zero_hit_rate(engine, rows: list[dict[str, Any]], k: int) -> dict[str, float]:
    """The label-INDEPENDENT number of spec 7.2, for both lexical modes.

    Spec 4.3 calls it decisive precisely because the biased label set cannot
    touch it: "the lexical half returned nothing" is a fact about the engine.
    """
    from symbiosis_brain import search as sb_search
    out: dict[str, float] = {}
    for label, mode in (("all", sb_search.FTS_MODE_ALL), ("any", sb_search.FTS_MODE_ANY)):
        zero = 0
        for row in rows:
            hits = engine.search_fts(row["query"], scope=row.get("scope") or None,
                                     limit=k, mode=mode)
            if not hits:
                zero += 1
        out[label] = round(zero / len(rows), 4) if rows else 0.0
    return out


def _model_slug(model: str) -> str:
    return model.rsplit("/", 1)[-1].strip().lower()


def peak_rss_bytes() -> int | None:
    """Peak resident set size of THIS process, or None where it cannot be read.

    Never raises. Spec 7.5 makes this number mandatory for a reindex measurement
    because of the reindex-storm incident (11 GB ONNX arena at batch 256,
    search.py:21-25) — a candidate that ranks well and eats 11 GB is not a
    candidate. psutil is not a dependency of this project, so: Windows goes
    through GetProcessMemoryInfo (same ctypes shape as parent_watchdog.py:67),
    POSIX reads ru_maxrss, which is KiB on Linux and bytes on macOS.

    GetProcessMemoryInfo needs explicit argtypes: GetCurrentProcess() returns
    the pseudo-handle (HANDLE)-1, and without a declared HANDLE argtype ctypes
    marshals that 64-bit value through a 32-bit C int on the call into psapi,
    truncating it — the callee then fails on the mangled handle (invalid on
    64-bit Windows) and this function honestly reports "unreadable" for a
    counter that was never actually asked for.
    """
    if sys.platform == "win32":
        try:
            import ctypes
            from ctypes import wintypes

            class _ProcessMemoryCounters(ctypes.Structure):
                _fields_ = [
                    ("cb", wintypes.DWORD),
                    ("PageFaultCount", wintypes.DWORD),
                    ("PeakWorkingSetSize", ctypes.c_size_t),
                    ("WorkingSetSize", ctypes.c_size_t),
                    ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                    ("PagefileUsage", ctypes.c_size_t),
                    ("PeakPagefileUsage", ctypes.c_size_t),
                ]

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            psapi = ctypes.WinDLL("psapi", use_last_error=True)
            kernel32.GetCurrentProcess.argtypes = []
            kernel32.GetCurrentProcess.restype = wintypes.HANDLE
            psapi.GetProcessMemoryInfo.argtypes = [
                wintypes.HANDLE,
                ctypes.POINTER(_ProcessMemoryCounters),
                wintypes.DWORD,
            ]
            psapi.GetProcessMemoryInfo.restype = wintypes.BOOL
            counters = _ProcessMemoryCounters()
            counters.cb = ctypes.sizeof(_ProcessMemoryCounters)
            ok = psapi.GetProcessMemoryInfo(
                kernel32.GetCurrentProcess(), ctypes.byref(counters), counters.cb)
            return int(counters.PeakWorkingSetSize) if ok else None
        except Exception:
            return None

    try:
        import resource
    except ImportError:
        return None
    try:
        peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    except (OSError, ValueError):
        return None
    # Linux reports KiB, macOS bytes.
    return int(peak) if sys.platform == "darwin" else int(peak) * 1024


def _gib(value: int) -> str:
    return f"{value / (1024 ** 3):.2f} GiB"


def run_eval(*, vault: Path, work_dir: Path, queries: Path, config_names: list[str],
             k: int = K_DEFAULT, model: str | None = None,
             rerank_model: str | None = None,
             query_prefix: str | None = None, doc_prefix: str | None = None,
             normalize: bool = False) -> dict[str, Any]:
    from symbiosis_brain import search as sb_search

    if (query_prefix or doc_prefix) and not model:
        raise EvalError(
            "--query-prefix/--doc-prefix need --model: they wrap the embedder that "
            "--model swaps in, and without --model there is nothing swapped in to wrap")
    if normalize and not model:
        raise EvalError(
            "--normalize needs --model — the installed index holds raw vectors; "
            "normalising only the query would compare unlike with unlike")

    known = configs()
    unknown = [name for name in config_names if name not in known]
    if unknown:
        raise EvalError(f"unknown config(s): {', '.join(unknown)}; "
                        f"known: {', '.join(known)}")

    rows, dropped = load_queries(queries)
    if not rows:
        raise EvalError(f"{queries} has no usable rows")

    has_rerank = any(known[name].kind == "rerank" for name in config_names)
    resolved_rerank_model = rerank_model or RERANK_MODEL
    if has_rerank:
        # E1 — _cross_encoder() takes no argument (its own test suite
        # monkeypatches it as a zero-arg callable), so the chosen model
        # travels through this module global instead.
        global _rerank_model_name
        _rerank_model_name = resolved_rerank_model

    work_db = prepare_work_dir(vault, work_dir)
    # E2 — captured BEFORE open_engine installs any prefix wrapper, so the
    # `finally` below can hand the module singletons back untouched even if
    # nothing was ever wrapped (a same-value restore is a harmless no-op).
    embed_before, embed_one_before = sb_search._embed, sb_search._embed_one
    timings: dict[str, float] = {}
    try:
        engine, storage = open_engine(work_db, model=model,
                                      query_prefix=query_prefix, doc_prefix=doc_prefix,
                                      normalize=normalize, timings=timings)
        try:
            lexical = lexical_zero_hit_rate(engine, rows, k)
            results: list[dict[str, Any]] = []
            fallback_share: dict[str, float] = {}
            for name in config_names:
                cfg = known[name]
                if cfg.kind in ("vector", "hybrid", "rerank") and not engine._vec_enabled:
                    raise EvalError(
                        f"config {name} needs the vector half, but sqlite-vec did not load")
                result, runs = evaluate(engine, cfg, rows, k)
                if cfg.kind == "rerank":
                    result["config"] = f"{cfg.name}({_model_slug(resolved_rerank_model)})"
                elif model:
                    result["config"] = f"{cfg.name}+{_model_slug(model)}"
                if query_prefix or doc_prefix:
                    result["config"] += "+prefixed"
                if normalize:
                    result["config"] += "+norm"
                if cfg.fts_mode == sb_search.FTS_MODE_ALL_THEN_ANY and runs:
                    fell_back = sum(1 for run in runs
                                    if run.effective_fts_mode == "fallback_any")
                    fallback_share[result["config"]] = round(fell_back / len(runs), 4)
                results.append(result)

            judged = sum(1 for row in rows if _relevant(grades_for(row)))
            meta = {
                "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "vault": str(vault),
                "work_dir": str(work_dir),
                "queries": str(queries),
                "k": k,
                "model": model or sb_search._MODEL_NAME,
                "rows_total": len(rows) + dropped,
                "rows_executed": len(rows),
                "rows_dropped_system": dropped,
                "rows_judged": judged,
                "lexical_zero_hit_rate": lexical,
                "fallback_any_share": fallback_share,
                "caveats": list(CAVEATS),
            }
            # m1 (pre-flight addendum): hybrid-any+rerank downloads a 1.04 GB
            # cross-encoder without ever touching --model, so peak RSS is
            # measured on EVERY run — only reindex_seconds stays --model-only,
            # since there is no rebuild to time without one.
            meta["peak_rss_bytes"] = peak_rss_bytes()
            # M4.2 (pre-flight addendum): notes vs notes_vec drift is a red
            # flag for the run that produced these numbers, not only for the
            # live vault — print it next to every result, not just a model swap.
            meta["notes"] = storage.count_notes()
            meta["notes_vec"] = _vec_count(storage)
            if model:
                # Spec 7.5 asks for the rebuild cost, and 7.4 turns it into a
                # criterion: a full rebuild over 10 minutes disqualifies a candidate
                # regardless of how well it ranks. reindex_seconds counts ONLY
                # engine.index_all() inside the lock (timings["rebuild_s"]) — the
                # dimension-probe embed and any wait for the reindex lock are
                # reported apart, under their own keys, so this number stays
                # comparable across runs regardless of lock contention (CP-8b
                # follow-up, K.D. 2026-08-27: a stale-lock wait once inflated a
                # 135s rebuild into a reported 315s and misled the next reader).
                meta["reindex_seconds"] = timings.get("rebuild_s")
                meta["reindex_lock_wait_seconds"] = timings.get("lock_wait_s")
                meta["model_load_seconds"] = timings.get("model_load_s")
            if has_rerank:
                meta["rerank_model"] = resolved_rerank_model
            if query_prefix or doc_prefix:
                meta["query_prefix"] = query_prefix
                meta["doc_prefix"] = doc_prefix
            if normalize:
                meta["normalize"] = True
            return {"meta": meta, "results": results}
        finally:
            storage.close()
    finally:
        sb_search._embed, sb_search._embed_one = embed_before, embed_one_before


# -------------------------------------------------------------- rendering ---

def _wrap(text: str, *, width: int = 96, indent: str = "   ") -> str:
    return "\n".join(textwrap.wrap(text, width=width, initial_indent=indent,
                                   subsequent_indent=indent + "   "))


def _render_buckets(results: list[dict[str, Any]], prefix: str) -> str:
    keys = sorted({key for result in results for key in result["buckets"]
                   if key.startswith(prefix + ":")})
    if not keys:
        return ""
    lines = [f"recall@k by {prefix}:"]
    header = f"{'config':<40}" + "".join(f"{key.split(':', 1)[1]:>14}" for key in keys)
    lines.append(header)
    lines.append("-" * len(header))
    for result in results:
        row = f"{result['config']:<40}"
        for key in keys:
            bucket = result["buckets"].get(key)
            row += f"{bucket['recall_at_k']:>14.3f}" if bucket else f"{'n/a':>14}"
        lines.append(row)
    return "\n".join(lines) + "\n"


def render_report(payload: dict[str, Any]) -> str:
    meta = payload["meta"]
    out: list[str] = ["=== symbiosis-brain search eval ==="]
    out.append(f"generated : {meta['generated_at']}")
    out.append(f"vault     : {meta['vault']}")
    out.append(f"work dir  : {meta['work_dir']}  (rebuilt from the snapshot on every run)")
    out.append(f"queries   : {meta['queries']}")
    out.append(f"rows      : {meta['rows_executed']} executed of {meta['rows_total']} "
               f"({meta['rows_dropped_system']} dropped as harness text); "
               f"{meta['rows_judged']} carry a positive label")
    out.append(f"k         : {meta['k']}")
    out.append(f"model     : {meta['model']}")
    if meta.get("reindex_seconds") is not None:
        reindex_line = (f"reindex   : {meta['reindex_seconds']} s for the COPY "
                        f"(spec 7.4 disqualifies a candidate above 600 s)")
        lock_wait = meta.get("reindex_lock_wait_seconds")
        if lock_wait is not None and lock_wait > 0.05:
            reindex_line += f" — waited {lock_wait} s for the reindex lock (NOT counted)"
        out.append(reindex_line)
    if meta.get("model_load_seconds") is not None:
        out.append(f"model load: {meta['model_load_seconds']} s")
    if meta.get("peak_rss_bytes"):
        out.append(f"peak RSS  : {_gib(meta['peak_rss_bytes'])} (peak of this process)")
    if "notes" in meta and "notes_vec" in meta:
        drift = "  ⚠ drift" if meta["notes"] != meta["notes_vec"] else ""
        out.append(f"index     : {meta['notes']} notes, {meta['notes_vec']} vectors{drift}")
    if meta.get("rerank_model"):
        out.append(f"rerank    : {meta['rerank_model']}")
    if "query_prefix" in meta:
        out.append(f'prefixes  : query="{meta.get("query_prefix") or ""}" '
                   f'doc="{meta.get("doc_prefix") or ""}"')
    if "normalize" in meta:
        out.append("normalize : L2-normalised embeddings (harness wrapper; the live "
                   "table compares raw vectors by L2)")
    lexical = " | ".join(f"{mode} {value:.1%}"
                         for mode, value in sorted(meta["lexical_zero_hit_rate"].items()))
    out.append(f"lexical zero-hit rate (label-independent, spec 7.2): {lexical}")
    if meta["fallback_any_share"]:
        out.append("all_then_any fell back to any on: " + " | ".join(
            f"{name} {value:.1%}" for name, value in sorted(meta["fallback_any_share"].items())))
    out.append("")
    out.append("CAVEATS — read these before the table:")
    for caveat in meta["caveats"]:
        out.append(_wrap(caveat))
    out.append("")

    header = (f"{'config':<40}{'n':>6}{'recall@k':>11}{'MRR':>9}"
              f"{'nDCG@k':>9}{'zero-hit':>10}{'p50 ms':>9}")
    out.append(header)
    out.append("-" * len(header))
    for result in payload["results"]:
        out.append(f"{result['config']:<40}{result['n']:>6}{result['recall_at_k']:>11.3f}"
                   f"{result['mrr']:>9.3f}{result['ndcg_at_k']:>9.3f}"
                   f"{result['zero_hit_rate']:>10.1%}{result['p50_latency_ms']:>9.1f}")
    out.append("")
    for prefix in ("lang", "source"):
        rendered = _render_buckets(payload["results"], prefix)
        if rendered:
            out.append(rendered)
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    # The report carries Cyrillic and typographic dashes; a Windows console
    # defaulting to CP1251 would crash on write (cf. tools/changelog_section.py).
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, OSError):
        pass

    parser = argparse.ArgumentParser(
        prog="eval_search.py",
        description="Measure symbiosis-brain search quality on a query set (spec 7.1).",
    )
    parser.add_argument("--vault", required=True, type=Path,
                        help="Vault directory. Its .index/brain.db is SNAPSHOTTED, never opened "
                             "for writing; a directory without one is indexed from its markdown.")
    parser.add_argument("--work-dir", required=True, type=Path,
                        help="Scratch directory for the snapshot. Rebuilt on every run.")
    parser.add_argument("--queries", required=True, type=Path,
                        help="JSONL query set (I-33).")
    parser.add_argument("--configs", default=",".join(DEFAULT_CONFIGS),
                        help="Comma-separated config ids. Default: the six cheap ones.")
    parser.add_argument("--k", type=int, default=K_DEFAULT, help="Cut-off. Default 5.")
    parser.add_argument("--out", type=Path, help="Write the JSON result here as well.")
    parser.add_argument("--model", default=None,
                        help="Embedder to measure instead of the installed one. Downloads it "
                             "on first use and reindexes the COPY — CP-8b, owner's go only.")
    parser.add_argument("--rerank-model", default=RERANK_MODEL,
                        help=f"Cross-encoder for the hybrid-any+rerank config (E1). "
                             f"Default: {RERANK_MODEL}. Downloads it on first use — CP-8b, "
                             f"owner's go only.")
    parser.add_argument("--query-prefix", default=None,
                        help="Prepended to every query before embedding (E2). Needs --model — "
                             "there is no swapped-in embedder to wrap otherwise.")
    parser.add_argument("--doc-prefix", default=None,
                        help="Prepended to every document before embedding (E2). Needs --model, "
                             "same reason as --query-prefix.")
    parser.add_argument("--normalize", action="store_true",
                        help="L2-normalise every embedding the harness produces (E3). Needs "
                             "--model — the installed index holds raw vectors, so normalising "
                             "only the query would compare unlike with unlike.")
    args = parser.parse_args(argv)

    config_names = [name.strip() for name in args.configs.split(",") if name.strip()]
    if not config_names:
        print("eval_search: --configs is empty", file=sys.stderr)
        return 1

    try:
        payload = run_eval(vault=args.vault, work_dir=args.work_dir, queries=args.queries,
                           config_names=config_names, k=args.k, model=args.model,
                           rerank_model=args.rerank_model,
                           query_prefix=args.query_prefix, doc_prefix=args.doc_prefix,
                           normalize=args.normalize)
    except EvalError as e:
        print(f"eval_search: {e}", file=sys.stderr)
        return 1

    print(render_report(payload))
    if args.out:
        from symbiosis_brain.atomic_write import atomic_write_text
        atomic_write_text(args.out, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
        print(f"\nJSON written to {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
