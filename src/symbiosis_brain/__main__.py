"""CLI entry: `python -m symbiosis_brain <subcommand>`.

Subcommands:
  (default)     — run MCP server (delegates to server.main)
  search-gist   — fast vault search for hook callers, returns JSON to stdout
  prewarm       — fastembed + sqlite-vec page-cache priming for SessionStart hook
"""
import os
import sys


def _emit_json(obj) -> None:
    """Emit a JSON envelope to stdout for hook consumers.

    ASCII-safe by design (``ensure_ascii=True``): the search-gist child only
    reconfigures *stdout* to UTF-8, not stdin, so on Windows a hook-piped
    UTF-8 prompt can decode to a lone surrogate (e.g. ``\\udc98``). Emitting
    with ``ensure_ascii=False`` would then raise ``UnicodeEncodeError`` against
    strict-UTF-8 stdout, the hook fails open to ``[]`` and BOTH memory and
    route hints are dropped. ``ensure_ascii=True`` escapes every non-ASCII
    code point (including lone surrogates) to ``\\uXXXX``, which the bash
    consumer un-escapes via ``json.loads`` — byte-transparent for it.
    """
    import json
    print(json.dumps(obj, ensure_ascii=True))


def _append_route_events(
    session_id: str,
    route_hints: list[dict],
    *,
    routing_mode: str,
    rules_emitted: bool,
    prompt: str,
) -> None:
    """Tier-0: append one route_fired JSONL line per fired route. Fail-open.

    Reads monotonic turn from env ``SYMBIOSIS_BRAIN_ROUTE_TURN`` (exported by
    bash before calling search-gist). This is the env-reading variant used by
    unit tests; the CLI fold in ``_run_search_gist`` calls
    ``tool_routing.append_route_fired`` directly — do NOT wire this helper into
    that fold or events will double-write.
    """
    if not route_hints:
        return
    import datetime as _dt
    import json as _json

    from symbiosis_brain.pre_action_config import _tmp_dir

    turn_raw = os.environ.get("SYMBIOSIS_BRAIN_ROUTE_TURN") or "0"
    try:
        turn_i = int(turn_raw)
    except ValueError:
        turn_i = 0
    snippet = (prompt or "")[:60]
    sid = session_id or "default"
    path = _tmp_dir() / f"brain-route-events-{sid}.jsonl"
    ts = _dt.datetime.now(_dt.timezone.utc).isoformat()
    try:
        with path.open("a", encoding="utf-8") as f:
            for r in route_hints:
                line = _json.dumps(
                    {
                        "session_id": sid,
                        "ts": ts,
                        "monotonic_turn": turn_i,
                        "event": "route_fired",
                        "route_id": r.get("id"),
                        "expected_tool": r.get("expected_tool"),
                        "observable": r.get("observable", False),
                        "routing_mode": routing_mode,
                        "rules_emitted": rules_emitted,
                        "prompt_snippet": snippet,
                    },
                    ensure_ascii=False,
                )
                f.write(line + "\n")
    except OSError:
        pass  # fail-open


def _load_pre_action_config():
    """PreActionConfig for the hook CLI paths — defaults on ANY failure.

    Returns None only when the module itself will not import; the caller then
    falls back to the raw legacy search rather than dropping recall entirely.
    Loaded ONCE per invocation and shared by the routing fold and the prompt
    recall fold, which both read knobs from it.
    """
    try:
        from symbiosis_brain.pre_action_config import PreActionConfig, load_config
    except Exception:
        return None
    try:
        return load_config()
    except Exception:
        return PreActionConfig()


def parse_hook_started_at(raw: str) -> float | None:
    """`--hook-started-at` → epoch seconds, or None for "not given" (I-9).

    The cost of an error here is asymmetric, and that is why the value crosses
    the boundary as a STRING: a comma-separated float blows argparse up with
    SystemExit(2), and downstream that is EXIT=2 in brain-save-trigger.sh:249-253,
    where GIST_JSON is silently replaced with '[]' — telemetry would have killed
    memory itself for every user on a ru/de locale (§2.8, measured).

    Garbage is "not given", never an error. Digits only = whole microseconds
    (I-10); a value with a dot = seconds; anything outside a sane hour-wide
    window is treated as not given, because a stale value would poison e2e_ms.
    """
    import time

    s = (raw or "").strip().replace(",", ".")
    if not s:
        return None
    try:
        v = float(s)
    except ValueError:
        return None
    if v <= 0:
        return None
    t0 = v / 1_000_000 if "." not in s else v
    now = time.time()
    return t0 if 0 < now - t0 < 3600 else None


def _close_retrieval_log() -> None:
    """Flush point (в) of §2.4 п. 4 at the end of a hook process. Fail-open:
    a telemetry teardown must never change the hook's exit code."""
    try:
        from symbiosis_brain import retrieval_log

        retrieval_log.close()
    except Exception:
        pass


def _detect_origin(payload: dict | None) -> str:
    """Fail-open wrapper around retrieval_log.detect_origin (I-4)."""
    try:
        from symbiosis_brain import retrieval_log

        return retrieval_log.detect_origin(payload)
    except Exception:
        return "unknown"


def _hook_log_ctx(source, vault_path, cfg, *, session_id=None, origin="unknown",
                  tool=None, started_at=None):
    """LogContext for a hook-path retrieval, or None when the log is off.

    `client` is the literal 'hook' (§2.5, Р4): a CLI process has no `app` and
    therefore no clientInfo to report. Both switches are consulted here, and
    the env one wins over the file one — `is_enabled` enforces that (§2.7).
    Fail-open: telemetry that cannot even build its context stays silent.
    """
    try:
        from symbiosis_brain import retrieval_log

        if not retrieval_log.is_enabled(cfg):
            return None
        return retrieval_log.LogContext(
            source=source,
            db_path=vault_path / ".index" / "brain.db",
            session_id=session_id or None,
            origin=origin,
            tool=tool,
            client="hook",
            started_at=started_at,
        )
    except Exception:
        return None


def _run_search_gist(argv: list[str]):
    import argparse
    import json
    from pathlib import Path

    # Force UTF-8 stdout — cyrillic + `→` (U+2192) in gists crashes default
    # cp1251 codec on Windows. Hook callers swallow stderr, so the only symptom
    # is silent empty recall. Same UTF-8 guard as install_cli.py.
    # errors="backslashreplace": defense-in-depth so a stray lone surrogate
    # (e.g. \udc98 from cp1251/surrogateescape stdin on Windows) can NEVER raise
    # UnicodeEncodeError on emit and fail the hook open to '[]' (bug #1).
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
    # Reconfigure stdin too: the hook pipes UTF-8 prompt bytes, but the child's
    # default Windows stdin codec is cp1251/surrogateescape, which mojibakes
    # multibyte UTF-8 into lone surrogates. Decode as UTF-8 to fix the ingress
    # root cause; errors="replace" keeps it fail-open on truly invalid bytes.
    if hasattr(sys.stdin, "reconfigure"):
        sys.stdin.reconfigure(encoding="utf-8", errors="replace")

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
    # str, never type=float (I-9): in a comma locale a float would not parse,
    # argparse would exit 2 and brain-save-trigger.sh:249-253 would substitute
    # '[]' — telemetry taking memory down with it (§2.8).
    parser.add_argument("--hook-started-at", default="")
    # parse_known_args + SystemExit guard: the ONLY dangerous drift direction is
    # a NEWER bash hook against an OLDER package (§8.5). An unknown flag must be
    # ignored, not kill recall. Today this entry has no guard at all.
    try:
        args, _unknown = parser.parse_known_args(argv)
    except SystemExit:
        # A typed option (--limit abc) can still exit. Emit the legacy empty
        # list: the bash consumer accepts both shapes (brain-save-trigger.sh:259-265).
        print("[]")
        return 0

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
    payload: dict = {}
    if args.prompt_from_stdin:
        try:
            raw = sys.stdin.read()
            data = json.loads(raw) if raw else {}
            payload = data if isinstance(data, dict) else {}
            prompt = (data.get("prompt") if isinstance(data, dict) else "") or ""
        except (json.JSONDecodeError, ValueError):
            prompt = ""

    started_at = parse_hook_started_at(args.hook_started_at)
    vault_path = Path(args.vault).expanduser().resolve()

    # Live config knobs (routing_*, recall dedup, excluded types) — loaded ONCE
    # for BOTH folds below AND for the legacy bare-list path. It used to be
    # loaded after the `if not envelope` return, which made the FILE switch
    # `retrieval_log_enabled` (§2.7) physically invisible to `_gist_search`.
    # A load error already degraded to dataclass defaults inside the helper;
    # None means the module itself would not import, and the memory fold then
    # falls back to the raw legacy search.
    cfg = _load_pre_action_config()

    # Legacy bare-list path: keep the exact old behavior, including the early
    # "[]" return for a missing vault. Routing is NOT run here (the deployed
    # hook does not consume route_hints yet). The retrieval log is the ONLY
    # addition, and it changes no byte of the output (contract __main__.py:136-146).
    if not envelope:
        if not vault_path.exists():
            print("[]")
            return 0
        results = _gist_search(
            vault_path, args.query, args.scope, args.limit,
            log_ctx=_hook_log_ctx("legacy_gist", vault_path, cfg, started_at=started_at),
        )
        _emit_json(results)
        return 0

    # Envelope path (Phase B / opt-in). Fold the routing engine in and emit
    # {memory_hits, route_hints}. Every step is fail-open.

    route_hint_list: list = []
    try:
        from symbiosis_brain import tool_routing as tr

        if cfg is None or not cfg.routing_enabled:
            # Routing disabled by config (or config unreadable) → emit empty
            # hints, skip the engine.
            route_hint_list = []
        else:
            routes = tr.load_routes(vault=vault_path if vault_path.exists() else None)
            matched = tr.match_routes(
                prompt, routes, scope=args.scope, vault=vault_path,
                roster=tr._roster_set(args.session_id), cap=cfg.routing_cap,
            )
            matched = tr.dedup_augment(
                matched, args.session_id,
                ttl_seconds=cfg.routing_seen_ttl_seconds,
            )
            route_hint_list = tr.route_hints(matched)
            # Tier-0 telemetry via the engine appender (the canonical writer for
            # the CLI fold). Task 6 owns the env-reading _append_route_events
            # variant — we do NOT call it here, so events are not double-written.
            tr.append_route_fired(
                args.session_id, matched, monotonic_turn=args.monotonic_turn,
                routing_mode=args.routing_mode, rules_emitted=args.rules_emitted,
                prompt=prompt,
            )
    except Exception:
        route_hint_list = []

    memory_hits: list = []
    if not args.skip_memory and vault_path.exists() and prompt:
        if cfg is None:
            # Config module would not even import: the legacy raw search is the
            # fallback, and it logs as `legacy_gist` — same source, same
            # unknown origin, because that row means one thing everywhere (Д2).
            memory_hits = _gist_search(
                vault_path, prompt, args.scope, args.limit,
                log_ctx=_hook_log_ctx("legacy_gist", vault_path, None,
                                      started_at=started_at),
            )
        else:
            memory_hits = _prompt_recall_hits(
                vault_path, prompt, args.scope, args.limit, args.session_id, cfg,
                log_ctx=_hook_log_ctx(
                    "hook_prompt", vault_path, cfg,
                    session_id=args.session_id,
                    origin=_detect_origin(payload),
                    started_at=started_at,
                ),
            )

    _emit_json({"memory_hits": memory_hits, "route_hints": route_hint_list})
    return 0


def _shape_hits(rows: list) -> list:
    """Shape search rows into the `{path,title,scope,gist}` objects the DEPLOYED
    bash hook parses (`brain-save-trigger.sh` reads `n['path']` and
    `n.get('gist','')`). This shape is a hard contract — do not add, rename or
    reorder keys. Shared by the legacy bare-list path and the envelope path so
    both keep emitting byte-identical hit objects."""
    return [
        {
            "path": r["path"],
            "title": r["title"],
            "scope": r["scope"],
            "gist": r.get("gist", ""),
        }
        for r in rows
    ]


def _gist_search(vault_path, query, scope, limit, *, log_ctx=None) -> list:
    """LEGACY bare-list path only: raw gist-mode top-N, no dedup, no type
    filter, no over-fetch.

    Kept byte-behaviour-identical on purpose — a pre-Phase-B copy of the hook
    still calls `search-gist` without `--envelope`/`--prompt-from-stdin` and
    parses exactly this. The envelope path goes through `_prompt_recall_hits`;
    do NOT "unify" the two.

    `log_ctx` (§2.9): this path has NO post-processing between search() and the
    caller, so the surfacing point IS the return of search() — unlike the two
    recall paths, which log from run_recall after the cap. Its rows always carry
    origin='unknown' and session_id NULL: one `legacy_gist` row means one thing
    everywhere, and on the bare-list entry there is no payload to read at all
    (stdin is consumed only under --prompt-from-stdin, __main__.py:150-157).
    """
    from symbiosis_brain.storage import Storage
    from symbiosis_brain.search import FTS_MODE_ALL_THEN_ANY, SearchEngine
    from symbiosis_brain.sync import VaultSync

    db_path = vault_path / ".index" / "brain.db"
    storage = Storage(db_path)
    _sync_result = VaultSync(vault_path, storage).sync_all()
    search = SearchEngine(storage)
    for _p in _sync_result.removed:
        search.delete_vec(_p)
    # Additions stay unembedded here (hook budget); the next serve start
    # repairs them at O(drift) via repair_index().
    # Note: we DO NOT re-index_all() here — too slow for hook (~3-5s).
    # Fall back to FTS-only if vector index isn't fresh.
    results = search.search(query=query, scope=scope, limit=limit, mode="gist",
                            fts_mode=FTS_MODE_ALL_THEN_ANY, log_ctx=log_ctx)
    return _shape_hits(results)


def _prompt_recall_hits(vault_path, prompt, scope, limit, session_id, cfg, *,
                        log_ctx=None) -> list:
    """Envelope-path prompt recall: the SAME pipeline PreToolUse runs.

    Buys three things the raw `_gist_search` never had on the UserPromptSubmit
    path — session dedup, `excluded_note_types`, and over-fetch (`run_recall`
    pulls `hit_limit*2` and BACKFILLS the freed slots with fresh hits instead
    of just cutting repeats out and showing 2 of 5).

    The seen store gets its OWN prefix: sharing `brain-recall-seen-` with
    pre-action recall would let the two paths strangle each other — different
    limits (5 vs 3), different purpose, different cadence. TTL comes from
    `prompt_recall_dedup_ttl_seconds` (1800 s); 120 s is far too short for a
    path that fires once per user prompt.

    Fail-open at every step: any error → [] and silence. This is the hottest
    hook path in the product; it must never raise into the turn.
    """
    from dataclasses import replace

    try:
        from symbiosis_brain.pre_action_recall import run_recall
        from symbiosis_brain.search import FTS_MODE_ALL_THEN_ANY, SearchEngine
        from symbiosis_brain.storage import Storage
        from symbiosis_brain.sync import VaultSync

        db_path = vault_path / ".index" / "brain.db"
        storage = Storage(db_path)
        sync_result = VaultSync(vault_path, storage).sync_all()
        engine = SearchEngine(storage)
        for _p in sync_result.removed:
            engine.delete_vec(_p)
    except Exception:
        return []

    seen = None
    if cfg.recall_dedup_enabled and session_id:
        try:
            from symbiosis_brain.recall_dedup import SeenStore

            seen = SeenStore(
                session_id,
                ttl_seconds=cfg.prompt_recall_dedup_ttl_seconds,
                prefix="brain-prompt-recall-seen-",
            )
        except Exception:
            seen = None  # dedup is best-effort, never a reason to drop recall

    try:
        hits = run_recall(
            query=prompt,
            scope=scope,
            config=replace(cfg, hit_limit=limit),
            engine=engine,
            seen=seen,
            # Q3: инжект в контекст — «все слова, при нуле любое». Явным
            # аргументом, а не дефолтом run_recall (§4.2). CP-1 заменяет
            # литерал CP-3 канонической константой search.py (I-17).
            fts_mode=FTS_MODE_ALL_THEN_ANY,
            log_ctx=log_ctx,
        )
    except Exception:
        return []
    return _shape_hits(hits)


def _run_prewarm(argv: list[str]) -> int:
    """Pre-warm fastembed model + sqlite-vec extension + vault DB.

    Hook spawns this in background at SessionStart so that the first real
    prompt-check invocation hits a warm OS page cache instead of paying full
    cold-start (~25s → ~6-8s observed). Subprocess Python heap is discarded —
    this only warms file-level caches, not the embedder object itself.

    Silent on success; logs to _debug_log_path() on unexpected
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

    from symbiosis_brain.pre_action_config import _debug_log_path
    debug = _debug_log_path()

    try:
        from symbiosis_brain.storage import Storage
        from symbiosis_brain.search import (
            SearchEngine, _MODEL_NAME, _get_embedder, _resolve_model_name, _set_active_model,
        )

        # Touch DB if it already exists, so sqlite-vec extension loads + WAL gets paged in.
        # Skip Storage init when DB doesn't exist — it would create empty tables on a
        # fresh install, which is not our job here.
        db_path = vault / ".index" / "brain.db"
        storage = Storage(db_path) if db_path.exists() else None

        # З3: resolve the model to warm from the VAULT'S OWN DB, never from
        # SYMBIOSIS_BRAIN_EMBED_MODEL — this hook subprocess gets its
        # environment from CLAUDE_ENV_FILE, not from the server's MCP
        # registration env, so it could apply a switch request the server
        # hasn't (yet, or ever) migrated the index to, and end up warming a
        # model the stored vectors don't match. Only the server applies that
        # request (server.py:_init). A vault with no DB yet has nothing to
        # resolve, so warm the default.
        model_name = _resolve_model_name(storage) if storage is not None else _MODEL_NAME
        # Triggers fastembed import + onnx file IO into page cache.
        _set_active_model(model_name)
        emb = _get_embedder()
        list(emb.embed(["warmup"]))

        if storage is not None:
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

    # errors="backslashreplace" + utf-8 stdin: same defense-in-depth as
    # search-gist (bug #1) — never crash the hook on a lone surrogate.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
    if hasattr(sys.stdin, "reconfigure"):
        sys.stdin.reconfigure(encoding="utf-8", errors="replace")

    # Kill-switch (env var; no config-file roundtrip needed)
    if os.environ.get("SYMBIOSIS_BRAIN_PRE_ACTION_DISABLED") == "1":
        return 0

    parser = argparse.ArgumentParser(prog="symbiosis_brain pre-action-recall")
    parser.add_argument("--vault", required=True)
    # str, never type=float (I-9, §2.8): the comma locale would turn telemetry
    # into an empty recall (here the failure is quiet — `except SystemExit`
    # below returns 0 — which makes it WORSE, not better, to diagnose).
    parser.add_argument("--hook-started-at", default="")
    try:
        # parse_known_args: a flag added by a newer bash hook must be ignored,
        # not kill the recall of an older package (§8.5).
        args, _unknown = parser.parse_known_args(argv)
    except SystemExit:
        # argparse calls sys.exit(2) on bad args — convert to fail-open exit 0
        return 0

    started_at = parse_hook_started_at(args.hook_started_at)

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
    if tool_name in ("Bash", "PowerShell"):
        # bash_whitelist is reused as-is for PowerShell (same mechanism, not
        # renamed — see PreActionConfig.bash_whitelist docstring).
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
        from symbiosis_brain.search import FTS_MODE_ALL_THEN_ANY, SearchEngine
        from symbiosis_brain.sync import VaultSync

        vault_path = Path(args.vault).expanduser().resolve()
        if not vault_path.exists():
            return 0
        db_path = vault_path / ".index" / "brain.db"
        storage = Storage(db_path)
        _sync_result = VaultSync(vault_path, storage).sync_all()
        engine = SearchEngine(storage)
        for _p in _sync_result.removed:
            engine.delete_vec(_p)
        # Additions stay unembedded here (hook budget); the next serve start
        # repairs them at O(drift) via repair_index().
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

        # origin is computed HERE, in the hook process of THIS tool call: the
        # CP-3 preflight measured that PreToolUse DOES run inside a subagent
        # for its own tool calls (review/preflight-step-b/README.md) — the
        # payload's `agent_id` is what tells the two apart (see
        # retrieval_log.detect_origin). `tool` carries tool_name in the row
        # regardless, so "the hint went to Task/Agent" stays visible without a
        # new column (I-1).
        hits = run_recall(
            query=query, scope=scope, config=cfg, engine=engine, seen=seen,
            fts_mode=FTS_MODE_ALL_THEN_ANY,
            log_ctx=_hook_log_ctx(
                "hook_pre_action", vault_path, cfg,
                session_id=session_id,
                origin=_detect_origin(payload),
                tool=tool_name,
                started_at=started_at,
            ),
        )
        recall_block = format_recall_block(query, hits)

        # C3: route hints on a SUBAGENT prompt. The user's prompt has had these
        # since Stage 4 (the search-gist fold), but a subagent brief never saw
        # them — so a route whose whole point is "read this BEFORE you launch the
        # agent" only fired when the user happened to type the trigger himself.
        # Bash/PowerShell are excluded deliberately: their warnings come from the
        # compiled action-rule path inside brain-pre-action-trigger.sh.
        agent_block = ""
        if tool_name in ("Task", "Agent") and cfg.routing_enabled:
            try:
                from symbiosis_brain import tool_routing as tr
                from symbiosis_brain.pre_action_recall import agent_route_block

                agent_block = agent_route_block(
                    tool_input.get("prompt") or "",
                    tr.load_routes(vault=vault_path),
                    scope=scope,
                    vault=vault_path,
                    roster=tr._roster_set(session_id),
                    cap=cfg.routing_cap,
                    session_id=session_id,
                    seen_ttl_seconds=cfg.routing_seen_ttl_seconds,
                )
            except Exception:
                agent_block = ""  # fail-open: routing never blocks a tool call

        # F4: Serena pre-edit advisory (action-time). Fold into the same
        # additionalContext so a code edit gets the "map dependencies first"
        # nudge even when recall returned nothing. Advisory-only, fail-open.
        advisory = ""
        if cfg.serena_advisory_enabled and tool_name in {"Edit", "Write", "MultiEdit"}:
            try:
                from symbiosis_brain.pre_action_recall import serena_advisory
                from symbiosis_brain.tool_routing import _roster_set
                roster = _roster_set(session_id)
                serena_present = bool(roster) and any("serena" in r for r in roster)
                adv_seen = None
                if serena_present and session_id:
                    from symbiosis_brain.recall_dedup import SeenStore
                    adv_seen = SeenStore(
                        session_id,
                        ttl_seconds=10**9,  # never expire within a session (per-file-once advice)
                        prefix="brain-serena-advised-",
                    )
                advisory = serena_advisory(
                    tool_name, tool_input, serena_present=serena_present, seen=adv_seen
                ) or ""
            except Exception:
                advisory = ""  # fail-open: advisory must never block the edit

        parts = [p for p in (recall_block, agent_block, advisory) if p]
        if not parts:
            return 0
        output = {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "additionalContext": "\n\n".join(parts),
            }
        }
        _emit_json(output)
        return 0
    except Exception:
        return 0  # fail-open on any runtime error


def _run_compile_action_rules(argv: list[str]) -> int:
    """`compile-action-rules --vault X` — (re)compile action-rules.tsv (Stage 1).

    Called from `brain_sync` (server.py) and from `setup claude-code`
    (install_cli.py) to keep <vault>/.index/action-rules.tsv fresh. Fail-open:
    compile_action_rules() itself never raises, but this wrapper still
    guards against a bad --vault path or an import-time surprise so a CLI
    caller never sees a traceback for what is a best-effort refresh.
    """
    import argparse
    from pathlib import Path

    parser = argparse.ArgumentParser(prog="symbiosis_brain compile-action-rules")
    parser.add_argument("--vault", required=True)
    try:
        args = parser.parse_args(argv)
    except SystemExit:
        return 0  # fail-open on bad args

    try:
        from symbiosis_brain.action_rules import compile_action_rules

        vault = Path(args.vault).expanduser()
        path = compile_action_rules(vault)
        print(str(path))
        return 0
    except Exception:
        return 0  # fail-open


def main():
    argv = sys.argv[1:]
    if argv and argv[0] in ("search-gist", "pre-action-recall"):
        # Both hook entries are short-lived processes, and close() is flush
        # point (в) of §2.4 п. 4: the accumulated skip delta must not die with
        # them. One finally here covers every `return` inside the two
        # subcommands instead of a dozen scattered try/finally blocks (Д3).
        runner = (_run_search_gist if argv[0] == "search-gist"
                  else _run_pre_action_recall)
        try:
            code = runner(argv[1:])
        finally:
            _close_retrieval_log()
        sys.exit(code)
    if argv and argv[0] == "prewarm":
        sys.exit(_run_prewarm(argv[1:]))
    if argv and argv[0] == "compile-action-rules":
        sys.exit(_run_compile_action_rules(argv[1:]))
    # Default — MCP server
    from symbiosis_brain.server import main as server_main
    server_main()


if __name__ == "__main__":
    main()
