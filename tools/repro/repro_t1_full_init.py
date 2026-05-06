"""Tier 1 reproducer: full MCP server `_init()` path.

Mimics what happens when a fresh Claude Code session starts symbiosis-brain MCP server.
Calls `server._init(vault)` directly so the run exercises the production code path:

- Storage open (PRAGMAs + migration + WAL retry)
- SearchEngine init (sqlite-vec extension load)
- VaultSync.sync_all (idempotent — skips unchanged notes via content_hash)
- _init's bootstrap-by-inference / model-drift / targeted-incremental / count-drift logic

REPRO_VAULT env var must point to a vault directory.
Each phase is timed individually; output is one JSON line on stdout.

Note: the FIRST cold run on a freshly-upgraded vault may execute a one-time
full re-embed (bootstrap path, ~50s for ~400 notes) if `embedding_model` is
not yet registered. Subsequent runs skip everything in <1s. Warm the vault
once with N=1 before running parallel benchmarks.
"""
import json
import os
import sys
import time

PHASE = {}


def mark(name, t0):
    PHASE[name] = round(time.perf_counter() - t0, 3)


def main():
    vault_str = os.environ.get("REPRO_VAULT")
    if not vault_str:
        raise RuntimeError("REPRO_VAULT env var not set")
    from pathlib import Path
    vault = Path(vault_str)

    t0 = time.perf_counter()

    from symbiosis_brain import server
    mark("imports_s", t0)

    server._init(vault)
    mark("init_s", t0)

    results = server._search.search(
        "test query for parallel cold-start measurement",
        limit=3, mode="gist",
    )
    mark("first_search_s", t0)

    PHASE["pid"] = os.getpid()
    PHASE["search_hits"] = len(results)
    PHASE["total_s"] = round(time.perf_counter() - t0, 3)
    print(json.dumps(PHASE))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(json.dumps({
            "error": str(exc)[:300],
            "type": type(exc).__name__,
            "pid": os.getpid(),
        }))
        sys.exit(1)
