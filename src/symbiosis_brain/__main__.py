"""CLI entry: `python -m symbiosis_brain <subcommand>`.

Subcommands:
  (default)     — run MCP server (delegates to server.main)
  search-gist   — fast vault search for hook callers, returns JSON to stdout
  prewarm       — fastembed + sqlite-vec page-cache priming for SessionStart hook
"""
import os
import sys


def _run_search_gist(argv: list[str]):
    import argparse
    import json
    from pathlib import Path

    # Force UTF-8 stdout — cyrillic + `→` (U+2192) in gists crashes default
    # cp1251 codec on Windows. Hook callers swallow stderr, so the only symptom
    # is silent empty recall. Mirror of the install_cli.py / brain-save-trigger.py fix.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    from symbiosis_brain.storage import Storage
    from symbiosis_brain.search import SearchEngine
    from symbiosis_brain.sync import VaultSync

    parser = argparse.ArgumentParser(prog="symbiosis_brain search-gist")
    parser.add_argument("--vault", required=True, help="Vault path")
    parser.add_argument("--query", required=True)
    parser.add_argument("--scope", default=None)
    parser.add_argument("--limit", type=int, default=5)
    args = parser.parse_args(argv)

    vault_path = Path(args.vault).expanduser().resolve()
    if not vault_path.exists():
        print("[]")
        return 0

    db_path = vault_path / ".index" / "brain.db"
    storage = Storage(db_path)
    sync = VaultSync(vault_path, storage)
    sync.sync_all()
    search = SearchEngine(storage)
    # Note: we DO NOT re-index_all() here — too slow for hook (~3-5s).
    # Fall back to FTS-only if vector index isn't fresh.
    results = search.search(
        query=args.query, scope=args.scope, limit=args.limit, mode="gist"
    )
    out = [
        {
            "path": r["path"],
            "title": r["title"],
            "scope": r["scope"],
            "gist": r.get("gist", ""),
        }
        for r in results
    ]
    print(json.dumps(out, ensure_ascii=False))
    return 0


def _run_prewarm(argv: list[str]) -> int:
    """Pre-warm fastembed model + sqlite-vec extension + vault DB.

    Hook spawns this in background at SessionStart so that the first real
    prompt-check invocation hits a warm OS page cache instead of paying full
    cold-start (~25s → ~6-8s observed). Subprocess Python heap is discarded —
    this only warms file-level caches, not the embedder object itself.

    Silent on success; logs to <TMPDIR>/brain-hook-debug.log on unexpected
    error so we don't lose visibility."""
    import argparse
    import datetime
    from pathlib import Path

    parser = argparse.ArgumentParser(prog="symbiosis_brain prewarm")
    parser.add_argument("--vault", required=True)
    args = parser.parse_args(argv)

    vault = Path(args.vault).expanduser()
    if not vault.exists():
        return 0  # graceful no-op for missing vault

    debug = Path(os.environ.get("TMPDIR") or os.environ.get("TEMP") or "/tmp") / "brain-hook-debug.log"

    try:
        # Triggers fastembed import + onnx file IO into page cache.
        # _get_embedder() takes NO arguments — see src/symbiosis_brain/search.py:65.
        from symbiosis_brain.search import _get_embedder
        emb = _get_embedder()
        list(emb.embed(["warmup"]))

        # Touch DB if it already exists, so sqlite-vec extension loads + WAL gets paged in.
        # Skip Storage init when DB doesn't exist — it would create empty tables on a
        # fresh install, which is not our job here.
        db_path = vault / ".index" / "brain.db"
        if db_path.exists():
            from symbiosis_brain.storage import Storage
            from symbiosis_brain.search import SearchEngine
            storage = Storage(db_path)
            SearchEngine(storage)  # load sqlite-vec extension
    except Exception as e:
        try:
            with debug.open("a", encoding="utf-8") as f:
                f.write(f"[{datetime.datetime.now().isoformat(timespec='seconds')}] "
                        f"prewarm FAIL {type(e).__name__}: {e}\n")
        except OSError:
            pass
        return 0  # never block session start
    return 0


def main():
    argv = sys.argv[1:]
    if argv and argv[0] == "search-gist":
        sys.exit(_run_search_gist(argv[1:]))
    if argv and argv[0] == "prewarm":
        sys.exit(_run_prewarm(argv[1:]))
    # Default — MCP server
    from symbiosis_brain.server import main as server_main
    server_main()


if __name__ == "__main__":
    main()
