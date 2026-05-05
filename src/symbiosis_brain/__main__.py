"""CLI entry: `python -m symbiosis_brain <subcommand>`.

Subcommands:
  (default)     — run MCP server (delegates to server.main)
  search-gist   — fast vault search for hook callers, returns JSON to stdout
"""
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


def main():
    argv = sys.argv[1:]
    if argv and argv[0] == "search-gist":
        sys.exit(_run_search_gist(argv[1:]))
    # Default — MCP server
    from symbiosis_brain.server import main as server_main
    server_main()


if __name__ == "__main__":
    main()
