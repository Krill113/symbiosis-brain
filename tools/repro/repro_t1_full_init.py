"""Tier 1 reproducer: full MCP server _init() path equivalent.

Mimics what happens when a fresh Claude Code session starts symbiosis-brain MCP server:
  - Open Storage (creates tables, migrations)
  - Init SearchEngine (loads sqlite-vec extension)
  - VaultSync.sync_all() (reads vault, upserts notes)
  - SearchEngine.index_all() (DELETE notes_vec + 395 INSERTs + 1 commit)

Then performs a single brain_search to simulate first user query.

REPRO_VAULT env var must point to a vault directory.
Each phase is timed individually; output is one JSON line on stdout.
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

    from symbiosis_brain.storage import Storage
    from symbiosis_brain.search import SearchEngine
    from symbiosis_brain.sync import VaultSync
    mark("imports_s", t0)

    db_path = vault / ".index" / "brain.db"
    storage = Storage(db_path)
    mark("storage_open_s", t0)

    search = SearchEngine(storage)
    mark("search_init_s", t0)

    sync = VaultSync(vault, storage)
    sync_result = sync.sync_all()
    mark("sync_all_s", t0)

    search.index_all()
    mark("index_all_s", t0)

    results = search.search("test query for parallel cold-start measurement",
                            limit=3, mode="gist")
    mark("first_search_s", t0)

    PHASE["pid"] = os.getpid()
    PHASE["sync_stats"] = {
        "added": sync_result["added"],
        "updated": sync_result["updated"],
        "removed": sync_result["removed"],
        "skipped": sync_result["skipped"],
    }
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
