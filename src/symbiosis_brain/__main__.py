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
    # is silent empty recall. Same UTF-8 guard as install_cli.py.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    parser = argparse.ArgumentParser(prog="symbiosis_brain search-gist")
    parser.add_argument("--vault", required=True, help="Vault path")
    # --query is now OPTIONAL (was required): stdin-only callers pass the prompt
    # via --prompt-from-stdin and need not supply --query at all.
    parser.add_argument("--query", default=None)
    parser.add_argument("--prompt-from-stdin", action="store_true")
    parser.add_argument("--envelope", action="store_true")
    parser.add_argument("--scope", default=None)
    parser.add_argument("--limit", type=int, default=5)
    parser.add_argument("--skip-memory", action="store_true")
    parser.add_argument("--session-id", default="")
    parser.add_argument("--routing-mode", default="decompose")
    parser.add_argument("--monotonic-turn", type=int, default=0)
    parser.add_argument("--rules-emitted", action="store_true")
    args = parser.parse_args(argv)

    # 🚨 BACKWARD-COMPAT (controller correction 2026-06-05): the DEPLOYED
    # ~/.claude bash hook calls this `search-gist` (via `uv run`) and parses the
    # OLD BARE LIST `[{path,title,scope,gist}]`. It is NOT redeployed until
    # Phase B. So we MUST return the bare list BY DEFAULT (byte-shape-identical
    # to the legacy contract) and emit the `{memory_hits, route_hints}` envelope
    # ONLY when a NEW flag opts in: --prompt-from-stdin (the Phase-B hook) or an
    # explicit --envelope. This keeps live memory recall working pre-Phase-B.
    envelope = args.prompt_from_stdin or args.envelope

    # Resolve the prompt. --prompt-from-stdin reads the RAW hook JSON and takes
    # ["prompt"] untruncated (do NOT rely on the truncated --query); embedded
    # quotes survive json.loads.
    prompt = args.query or ""
    if args.prompt_from_stdin:
        try:
            raw = sys.stdin.read()
            data = json.loads(raw) if raw else {}
            prompt = (data.get("prompt") if isinstance(data, dict) else "") or ""
        except (json.JSONDecodeError, ValueError):
            prompt = ""

    vault_path = Path(args.vault).expanduser().resolve()

    # Legacy bare-list path: keep the exact old behavior, including the early
    # "[]" return for a missing vault. Routing is NOT run here (the deployed
    # hook does not consume route_hints yet).
    if not envelope:
        if not vault_path.exists():
            print("[]")
            return 0
        results = _gist_search(vault_path, args.query, args.scope, args.limit)
        print(json.dumps(results, ensure_ascii=False))
        return 0

    # Envelope path (Phase B / opt-in). Fold the routing engine in and emit
    # {memory_hits, route_hints}. Every routing step is fail-open.
    route_hint_list: list = []
    try:
        from symbiosis_brain import tool_routing as tr

        routes = tr.load_routes(vault=vault_path if vault_path.exists() else None)
        matched = tr.match_routes(
            prompt, routes, scope=args.scope, vault=vault_path,
            roster=tr._roster_set(args.session_id),
        )
        matched = tr.dedup_augment(matched, args.session_id)
        route_hint_list = tr.route_hints(matched)
        # Tier-0 telemetry via the engine appender (the canonical writer for the
        # CLI fold). Task 6 owns the env-reading _append_route_events variant —
        # we do NOT call it here, so events are not double-written.
        tr.append_route_fired(
            args.session_id, matched, monotonic_turn=args.monotonic_turn,
            routing_mode=args.routing_mode, rules_emitted=args.rules_emitted,
            prompt=prompt,
        )
    except Exception:
        route_hint_list = []

    memory_hits: list = []
    if not args.skip_memory and vault_path.exists() and prompt:
        memory_hits = _gist_search(vault_path, prompt, args.scope, args.limit)

    print(json.dumps(
        {"memory_hits": memory_hits, "route_hints": route_hint_list},
        ensure_ascii=False,
    ))
    return 0


def _gist_search(vault_path, query, scope, limit) -> list:
    """Run the gist-mode vault search and shape each hit as
    `{path,title,scope,gist}`. Shared by the legacy bare-list and envelope paths
    so both return byte-identical hit objects."""
    from symbiosis_brain.storage import Storage
    from symbiosis_brain.search import SearchEngine
    from symbiosis_brain.sync import VaultSync

    db_path = vault_path / ".index" / "brain.db"
    storage = Storage(db_path)
    VaultSync(vault_path, storage).sync_all()
    search = SearchEngine(storage)
    # Note: we DO NOT re-index_all() here — too slow for hook (~3-5s).
    # Fall back to FTS-only if vector index isn't fresh.
    results = search.search(query=query, scope=scope, limit=limit, mode="gist")
    return [
        {
            "path": r["path"],
            "title": r["title"],
            "scope": r["scope"],
            "gist": r.get("gist", ""),
        }
        for r in results
    ]


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

    from symbiosis_brain.pre_action_config import _tmp_dir
    debug = _tmp_dir() / "brain-hook-debug.log"

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


def _run_pre_action_recall(argv: list[str]) -> int:
    """Pre-action recall subcommand for PreToolUse hook (B1).

    Reads hook payload from stdin (piped by bash wrapper to avoid Windows
    arg-length cap on large Task prompts), applies config + whitelist +
    type filter, calls SearchEngine, formats top-N hits as JSON for hook.

    Fail-open: any unexpected error → exit 0 + empty stdout.
    """
    import argparse
    import json
    import os
    from pathlib import Path

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    # Kill-switch (env var; no config-file roundtrip needed)
    if os.environ.get("SYMBIOSIS_BRAIN_PRE_ACTION_DISABLED") == "1":
        return 0

    parser = argparse.ArgumentParser(prog="symbiosis_brain pre-action-recall")
    parser.add_argument("--vault", required=True)
    try:
        args = parser.parse_args(argv)
    except SystemExit:
        # argparse calls sys.exit(2) on bad args — convert to fail-open exit 0
        return 0

    # Read PreToolUse payload from stdin (piped by bash wrapper)
    try:
        payload_str = sys.stdin.read()
        payload = json.loads(payload_str) if payload_str else {}
    except json.JSONDecodeError:
        return 0  # fail-open

    tool_name = payload.get("tool_name") or ""
    tool_input = payload.get("tool_input") or {}
    session_id = payload.get("session_id") or ""

    from symbiosis_brain.pre_action_config import load_config
    from symbiosis_brain.pre_action_recall import (
        build_query, format_recall_block, run_recall,
    )
    from symbiosis_brain.bash_filter import matches_whitelist

    cfg = load_config()
    if not cfg.enabled:
        return 0
    if tool_name not in cfg.matchers:
        return 0
    if tool_name == "Bash":
        cmd = tool_input.get("command") or ""
        if not matches_whitelist(cmd, cfg.bash_whitelist):
            return 0

    query = build_query(tool_name, tool_input, cfg.query_max_chars)
    if not query:
        return 0

    # Scope from env var (set by SessionStart hook via CLAUDE_ENV_FILE,
    # propagated to this subprocess by uv run; not a bridge file).
    scope = os.environ.get("SYMBIOSIS_BRAIN_SCOPE") or None

    # Plug SearchEngine. Wrapped in try/except per fail-open principle —
    # corrupt vault, locked DB, or unexpected runtime errors must not block
    # the tool call. Bash hook has its own outer error handling; this is
    # defense in depth.
    try:
        from symbiosis_brain.storage import Storage
        from symbiosis_brain.search import SearchEngine
        from symbiosis_brain.sync import VaultSync

        vault_path = Path(args.vault).expanduser().resolve()
        if not vault_path.exists():
            return 0
        db_path = vault_path / ".index" / "brain.db"
        storage = Storage(db_path)
        VaultSync(vault_path, storage).sync_all()
        engine = SearchEngine(storage)
        # Note: we DO NOT re-index_all() here — too slow for hook (~3-5s).
        # In production the vector index is prewarmed at SessionStart and
        # persists across sessions. Tests pre-populate the index in fixture.
        if not getattr(engine, "_vec_enabled", True):
            from symbiosis_brain.pre_action_config import _debug_log
            _debug_log("pre-action-recall: vector index cold/disabled — FTS-only recall")

        seen = None
        if cfg.recall_dedup_enabled and session_id:
            try:
                from symbiosis_brain.recall_dedup import SeenStore
                seen = SeenStore(session_id, ttl_seconds=cfg.recall_dedup_ttl_seconds)
            except Exception:
                seen = None  # fail-open: dedup is best-effort, never block recall

        hits = run_recall(query=query, scope=scope, config=cfg, engine=engine, seen=seen)
        if not hits:
            return 0

        block = format_recall_block(query, hits)
        output = {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "additionalContext": block,
            }
        }
        print(json.dumps(output, ensure_ascii=False))
        return 0
    except Exception:
        return 0  # fail-open on any runtime error


def main():
    argv = sys.argv[1:]
    if argv and argv[0] == "search-gist":
        sys.exit(_run_search_gist(argv[1:]))
    if argv and argv[0] == "prewarm":
        sys.exit(_run_prewarm(argv[1:]))
    if argv and argv[0] == "pre-action-recall":
        sys.exit(_run_pre_action_recall(argv[1:]))
    # Default — MCP server
    from symbiosis_brain.server import main as server_main
    server_main()


if __name__ == "__main__":
    main()
