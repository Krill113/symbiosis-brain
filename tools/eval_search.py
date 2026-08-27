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


def rebuild_vector_index(engine, storage, *, model: str) -> int:
    """Swap the embedder and rebuild notes_vec inside the COPY. Returns the dim.

    notes_vec is declared FLOAT[384] (search.py:245-254), so a model of another
    width needs the table dropped and recreated. The width is PROBED from the
    model rather than taken on trust — that removes a CLI flag and a whole class
    of "wrong dimension, silent garbage" mistakes.

    `engine` must already exist: dropping a vec0 virtual table needs the
    sqlite-vec extension loaded, and SearchEngine.__init__ is what loads it
    (search.py:234-243).
    """
    from symbiosis_brain import search as sb_search

    if not engine._vec_enabled:
        raise EvalError("sqlite-vec is not available — a model swap has nothing to index")

    apply_model_override(model)
    dim = len(sb_search._embed_one("dimension probe"))
    if dim != 384:
        storage._conn.execute("DROP TABLE IF EXISTS notes_vec")
        storage._conn.commit()
        storage._conn.execute(
            "CREATE VIRTUAL TABLE notes_vec USING vec0("
            f"path TEXT PRIMARY KEY, embedding FLOAT[{dim}])"
        )
        storage._conn.commit()
    with sb_search._reindex_lock(storage.db_path):
        engine.index_all()
    return dim


def _vec_count(storage) -> int:
    try:
        row = storage._conn.execute("SELECT COUNT(*) FROM notes_vec").fetchone()
    except sqlite3.Error:
        return 0
    return int(row[0]) if row else 0


def open_engine(work_db: Path, *, model: str | None):
    """Open the COPY and return (engine, storage). The caller closes storage."""
    from symbiosis_brain import search as sb_search
    from symbiosis_brain.storage import Storage

    storage = Storage(Path(work_db))
    try:
        engine = sb_search.SearchEngine(storage)
        if model:
            rebuild_vector_index(engine, storage, model=model)
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

def _run_one(engine, cfg: Config, row: dict[str, Any], k: int) -> Run:
    query = row["query"]
    scope = row.get("scope") or None
    stats: dict[str, Any] = {}
    started = time.perf_counter()
    if cfg.kind == "fts":
        hits = engine.search_fts(query, scope=scope, limit=k, mode=cfg.fts_mode)
    elif cfg.kind == "vector":
        hits = engine.search_vector(query, scope=scope, limit=k)
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


def run_eval(*, vault: Path, work_dir: Path, queries: Path, config_names: list[str],
             k: int = K_DEFAULT, model: str | None = None) -> dict[str, Any]:
    from symbiosis_brain import search as sb_search

    known = configs()
    unknown = [name for name in config_names if name not in known]
    if unknown:
        raise EvalError(f"unknown config(s): {', '.join(unknown)}; "
                        f"known: {', '.join(known)}")

    rows, dropped = load_queries(queries)
    if not rows:
        raise EvalError(f"{queries} has no usable rows")

    work_db = prepare_work_dir(vault, work_dir)
    engine, storage = open_engine(work_db, model=model)
    try:
        lexical = lexical_zero_hit_rate(engine, rows, k)
        results: list[dict[str, Any]] = []
        fallback_share: dict[str, float] = {}
        for name in config_names:
            cfg = known[name]
            if cfg.kind in ("vector", "hybrid") and not engine._vec_enabled:
                raise EvalError(
                    f"config {name} needs the vector half, but sqlite-vec did not load")
            result, runs = evaluate(engine, cfg, rows, k)
            if model:
                result["config"] = f"{cfg.name}+{_model_slug(model)}"
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
        return {"meta": meta, "results": results}
    finally:
        storage.close()


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
    args = parser.parse_args(argv)

    config_names = [name.strip() for name in args.configs.split(",") if name.strip()]
    if not config_names:
        print("eval_search: --configs is empty", file=sys.stderr)
        return 1

    try:
        payload = run_eval(vault=args.vault, work_dir=args.work_dir, queries=args.queries,
                           config_names=config_names, k=args.k, model=args.model)
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
