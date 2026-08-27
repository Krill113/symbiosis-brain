#!/usr/bin/env python3
"""Recalibrate the brain_write duplicate hint against a real vault.

The 0.5 default of `DEDUP_CONTAINMENT_MIN` (src/symbiosis_brain/search.py) was
measured on ONE 1465-note vault: containment >= 0.5 together with `_in_both`
fired on 4.4 % of writes there. On a different corpus the same threshold means
a different amount of noise — especially on a small vault, where `_in_both` is
satisfied trivially. This script reproduces that table for any vault so the
threshold can be picked on numbers instead of on feel:

    python tools/dedup_calib.py --vault <vault> [--sample 250] [--seed 7]

The live database is NEVER opened for writing. It is read through a
`?mode=ro` URI and copied out with `VACUUM INTO`; copying the three files of a
WAL set by hand is forbidden (they would arrive torn). Everything after the
snapshot runs against the copy, including the migrations `Storage` applies on
open.

Repo tooling, not shipped in the wheel (see pyproject.toml wheel targets).
"""
from __future__ import annotations

import argparse
import random
import sqlite3
import sys
import tempfile
from pathlib import Path

_GRID = (0.4, 0.5, 0.6)


def read_only_uri(db_path: Path) -> str:
    """SQLite URI that can read a live database but never write it."""
    return f"file:{Path(db_path).resolve().as_posix()}?mode=ro"


def snapshot(db_path: Path, dest: Path) -> Path:
    """Copy a live database out via VACUUM INTO on a read-only connection."""
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        dest.unlink()
    conn = sqlite3.connect(read_only_uri(db_path), uri=True)
    try:
        conn.execute("VACUUM INTO ?", (str(dest),))
    finally:
        conn.close()
    return dest


def measure(storage, engine, notes: list[dict], *, top_k: int) -> dict[tuple[str, float], int]:
    """How often the hint would fire, per rule, over `notes`."""
    from symbiosis_brain.search import FTS_MODE_ANY, _dedup_tokens, containment

    fired: dict[tuple[str, float], int] = {}
    for rule in ("plain", "in_both"):
        for threshold in _GRID:
            fired[(rule, threshold)] = 0

    for note in notes:
        frontmatter = note.get("frontmatter") or {}
        gist = str(frontmatter.get("gist") or "") if isinstance(frontmatter, dict) else ""
        query = f"{note.get('title') or ''} {gist}".strip()
        mine = _dedup_tokens(query)
        if not query or not mine:
            continue
        best_plain = 0.0
        best_in_both = 0.0
        for hit in engine.search(query=query, scope=None, limit=top_k,
                                 mode="gist", fts_mode=FTS_MODE_ANY):
            if hit.get("path") == note.get("path"):
                continue        # себя не считаем — ровно как dedup_candidates
            theirs = _dedup_tokens(f"{hit.get('title') or ''} {hit.get('gist') or ''}")
            score = containment(mine, theirs)
            best_plain = max(best_plain, score)
            if hit.get("_in_both"):
                best_in_both = max(best_in_both, score)
        for threshold in _GRID:
            if best_plain >= threshold:
                fired[("plain", threshold)] += 1
            if best_in_both >= threshold:
                fired[("in_both", threshold)] += 1
    return fired


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="dedup_calib")
    parser.add_argument("--vault", required=True, help="vault directory")
    parser.add_argument("--sample", type=int, default=250,
                        help="how many notes to probe (0 = all)")
    parser.add_argument("--seed", type=int, default=7, help="sampling seed")
    parser.add_argument("--work-dir", default=None,
                        help="where the snapshot goes (default: a temp dir)")
    parser.add_argument("--top-k", type=int, default=None,
                        help="candidates per probe (default: DEDUP_TOP_K)")
    args = parser.parse_args(argv)

    vault = Path(args.vault).expanduser().resolve()
    live_db = vault / ".index" / "brain.db"
    if not live_db.exists():
        print(f"no database at {live_db}", file=sys.stderr)
        return 2

    work = (Path(args.work_dir).expanduser().resolve() if args.work_dir
            else Path(tempfile.mkdtemp(prefix="sb-dedup-calib-")))
    copy_db = snapshot(live_db, work / "brain.db")

    from symbiosis_brain.search import DEDUP_TOP_K, SearchEngine
    from symbiosis_brain.storage import Storage

    storage = Storage(copy_db)
    try:
        engine = SearchEngine(storage)
        if not engine._vec_enabled:
            print("WARNING: vector half unavailable in this copy — the `_in_both` "
                  "rows below would all read 0 %", file=sys.stderr)
        notes = storage.list_notes()
        total = len(notes)
        if args.sample and 0 < args.sample < total:
            notes = random.Random(args.seed).sample(notes, args.sample)
        top_k = args.top_k or DEDUP_TOP_K
        fired = measure(storage, engine, notes, top_k=top_k)
    finally:
        storage.close()

    probed = len(notes) or 1
    print(f"vault:    {vault}")
    print(f"snapshot: {copy_db}")
    print(f"notes:    {len(notes)} probed of {total} (seed {args.seed}, top_k {top_k})")
    print()
    print("| rule | share of writes where the hint would fire |")
    print("|---|---|")
    for threshold in _GRID:
        share = 100.0 * fired[("plain", threshold)] / probed
        print(f"| containment >= {threshold} (no _in_both) | {share:.1f} % |")
    for threshold in _GRID:
        share = 100.0 * fired[("in_both", threshold)] / probed
        print(f"| containment >= {threshold} and _in_both | {share:.1f} % |")
    print()
    print("Set SYMBIOSIS_BRAIN_DEDUP_MIN to the threshold whose share you can live "
          "with; 0 disables the hint entirely.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
