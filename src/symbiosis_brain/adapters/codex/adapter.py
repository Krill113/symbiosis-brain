"""Codex lifecycle dispatcher.

The adapter is deliberately thin: trust, bootstrap, recall, session state and
sync process safety remain owned by their existing Codex modules.  This module
only joins them at the hook boundary and fails open on every hook path.
"""
from __future__ import annotations

import hashlib
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from symbiosis_brain import pre_action_config
from symbiosis_brain.adapters.codex.bootstrap import build_bootstrap
from symbiosis_brain.adapters.codex.config import CodexConfig, load_codex_config
from symbiosis_brain.adapters.codex.protocol import (
    HookEvent,
    HookRequest,
    context_response,
    continue_response,
    continuation_response,
    emit_response,
    parse_hook_request,
)
from symbiosis_brain.adapters.codex.scope import ScopeResolution, resolve_scope
from symbiosis_brain.adapters.codex.session_state import (
    SessionState,
    delete_state,
    load_state,
    save_state,
    session_lock,
)
from symbiosis_brain.adapters.codex.sync_launcher import launch_auto_sync
from symbiosis_brain.adapters.codex.tool_normalizer import normalize_tool_call


_DISCIPLINE_INTERVAL = 10
_MIN_PROMPT_CHARS = 15
_BARE_AFFIRMATIONS = frozenset(
    {
        "ok",
        "okay",
        "yes",
        "y",
        "no",
        "nope",
        "sure",
        "thanks",
        "thank you",
        "done",
        "got it",
        "looks good",
        "lgtm",
    }
)
_SAVE_TOOL_MARKERS = {
    f"{prefix}{name}": marker
    for prefix in (
        "mcp__symbiosis-brain__",
        "mcp__symbiosis_brain__",
    )
    for name, marker in {
        "brain_write": "Saved:",
        "brain_append": "Appended",
        "brain_patch": "Patched",
        "brain_sync": "Sync complete:",
    }.items()
}
_INTERNAL_DEADLINES = {
    HookEvent.SESSION_START: 25.0,
    HookEvent.USER_PROMPT_SUBMIT: 12.0,
    HookEvent.PRE_TOOL_USE: 12.0,
    HookEvent.POST_TOOL_USE: 3.0,
    HookEvent.PRE_COMPACT: 3.0,
    HookEvent.POST_COMPACT: 3.0,
    HookEvent.STOP: 3.0,
    HookEvent.SESSION_END: 2.0,
}


def open_recall_runtime(*args, **kwargs):
    from symbiosis_brain.adapters.codex.recall import open_recall_runtime as impl

    return impl(*args, **kwargs)


def prompt_recall(*args, **kwargs):
    from symbiosis_brain.adapters.codex.recall import prompt_recall as impl

    return impl(*args, **kwargs)


def action_recall(*args, **kwargs):
    from symbiosis_brain.adapters.codex.recall import action_recall as impl

    return impl(*args, **kwargs)


def match_codex_routes(*args, **kwargs):
    from symbiosis_brain.adapters.codex.recall import match_codex_routes as impl

    return impl(*args, **kwargs)


def resolve_codex_routing_environment(*args, **kwargs):
    from symbiosis_brain.adapters.codex.recall import (
        resolve_codex_routing_environment as impl,
    )

    return impl(*args, **kwargs)


def codex_discipline_hint(*args, **kwargs):
    from symbiosis_brain.adapters.codex.recall import codex_discipline_hint as impl

    return impl(*args, **kwargs)


@dataclass
class AdapterContext:
    vault: Path
    config: CodexConfig
    now: datetime


def _debug_log(message: str) -> None:
    """Write one adapter-local diagnostic through the existing safe sink."""
    pre_action_config._debug_log(f"codex adapter: {message}")


def _session_hash(session_id: str | None) -> str:
    if not session_id:
        return "none"
    return hashlib.sha256(session_id.encode("utf-8", errors="replace")).hexdigest()[:16]


def _duration_bucket(seconds: float) -> str:
    if seconds < 0.01:
        return "lt10ms"
    if seconds < 0.1:
        return "lt100ms"
    if seconds < 1:
        return "lt1s"
    return "gte1s"


def _diagnostic(
    request: HookRequest | None,
    *,
    scope_source: str | None,
    started: float,
    hit_count: int = 0,
    degraded: str = "none",
    error_class: str | None = None,
) -> None:
    event = request.event.value if request is not None else "unknown"
    session = _session_hash(request.session_id if request is not None else None)
    fields = [
        f"event={event}",
        f"session={session}",
        f"scope_source={scope_source or 'unknown'}",
        f"duration={_duration_bucket(time.perf_counter() - started)}",
        f"hits={max(0, int(hit_count))}",
        f"degraded={degraded}",
    ]
    if error_class:
        fields.extend(
            [
                f"error_class={error_class}",
                "error_code=internal_error",
            ]
        )
    try:
        _debug_log(" ".join(fields))
    except Exception:
        pass


def _fallback_resolution() -> ScopeResolution:
    return ScopeResolution(
        scope=None,
        source="none",
        marker_path=None,
        depth=None,
        warning="scope resolution failed; using global memory",
        trusted=True,
        allow_global=True,
        memory_authorized=True,
    )


def _resolve(request: HookRequest, context: AdapterContext) -> ScopeResolution:
    try:
        return resolve_scope(
            request.cwd,
            context.vault,
            context.config.trusted_projects,
        )
    except Exception:
        return _fallback_resolution()


def _recall_scope(resolution: ScopeResolution) -> str:
    """Keep automatic recall global-only when no project scope resolves."""
    return resolution.scope or "global"


def _state_update(
    session_id: str | None,
    update: Callable[[SessionState], SessionState],
) -> SessionState | None:
    if not session_id:
        return None
    try:
        with session_lock(session_id):
            state = update(load_state(session_id))
            save_state(state)
            return state
    except Exception:
        return None


def _state_read(session_id: str | None) -> SessionState | None:
    if not session_id:
        return None
    try:
        with session_lock(session_id):
            return load_state(session_id)
    except Exception:
        return None


def _launch_prewarm(vault: Path) -> bool:
    """Start the existing prewarm path without delaying the hook response."""
    kwargs: dict[str, object] = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "close_fds": True,
    }
    if os.name == "nt":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        subprocess.Popen(
            [
                sys.executable,
                "-B",
                "-m",
                "symbiosis_brain",
                "prewarm",
                "--vault",
                str(vault),
            ],
            **kwargs,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return True


def _status_message(
    config: CodexConfig,
    *,
    warning: str | None,
) -> str | None:
    if warning:
        return f"SB ⚠ {warning}"
    if config.bootstrap_visibility == "one-line":
        return "SB ✓"
    return None


def _session_start(
    request: HookRequest,
    context: AdapterContext,
    resolution: ScopeResolution | None = None,
) -> dict[str, Any]:
    resolution = resolution or _resolve(request, context)

    source = (
        "compact"
        if request.payload.get("source") == "compact"
        else "startup"
    )
    result = build_bootstrap(
        context.vault,
        resolution,
        context.config,
        source=source,
        today=context.now.date(),
    )
    if request.session_id:
        _state_update(request.session_id, lambda state: state)
    if source == "startup":
        try:
            _launch_prewarm(context.vault)
        except Exception:
            pass

    system_message = _status_message(context.config, warning=result.warning)
    return context_response(
        request.event,
        additional_context=result.context,
        system_message=system_message,
    )


def _prompt_text(request: HookRequest) -> str:
    value = request.payload.get("prompt")
    return value if isinstance(value, str) else ""


def _is_slash_or_affirmation(prompt: str) -> bool:
    clean = " ".join(prompt.strip().split()).casefold()
    return clean.startswith("/") or clean in _BARE_AFFIRMATIONS


def _render_routes(route_hints: Sequence[Mapping[str, Any]]) -> list[str]:
    rendered: list[str] = []
    seen: set[str] = set()
    for item in route_hints:
        hint = item.get("hint")
        if not isinstance(hint, str):
            continue
        clean = hint.strip()
        if clean and clean not in seen:
            seen.add(clean)
            rendered.append(clean)
    return rendered


def _render_prompt_context(
    hits: Sequence[Mapping[str, Any]],
    route_hints: Sequence[Mapping[str, Any]],
    discipline: str,
) -> str:
    parts: list[str] = []
    if hits:
        parts.append(f"[memory: {len(hits)} hits]")
        for hit in hits:
            path = hit.get("path", "?")
            title = hit.get("title") or path
            gist = hit.get("gist") or "(no gist)"
            if isinstance(path, str) and isinstance(title, str) and isinstance(gist, str):
                parts.append(f"- {title} ({path}) — {gist}")
    routes = _render_routes(route_hints)
    if routes:
        parts.append("[tool guidance]")
        parts.extend(f"- {hint}" for hint in routes)
    if discipline:
        parts.append(discipline)

    rendered = "\n".join(parts).strip()
    return rendered


def _routing_inputs(
    request: HookRequest,
    context: AdapterContext,
    *,
    resolution: ScopeResolution,
) -> tuple[list[Any], Any]:
    if not context.config.recall.routing_enabled:
        return [], None
    try:
        from symbiosis_brain import tool_routing

        routes = tool_routing.load_routes(vault=context.vault)
        environment = resolve_codex_routing_environment(cwd=request.cwd)
        return routes, environment
    except Exception:
        return [], None


def _route_only(
    prompt: str,
    request: HookRequest,
    context: AdapterContext,
    resolution: ScopeResolution,
) -> list[dict[str, Any]]:
    from symbiosis_brain import tool_routing

    routes, environment = _routing_inputs(request, context, resolution=resolution)
    if not routes:
        return []
    try:
        matched = match_codex_routes(
            prompt,
            routes,
            scope=_recall_scope(resolution),
            environment=environment,
            cap=context.config.recall.routing_cap,
            session_id=request.session_id or "",
            seen_ttl_seconds=context.config.recall.routing_seen_ttl_seconds,
        )
        return tool_routing.route_hints(matched)
    except Exception:
        return []


def _prompt(
    request: HookRequest,
    context: AdapterContext,
    resolution: ScopeResolution | None = None,
) -> dict[str, Any]:
    resolution = resolution or _resolve(request, context)

    state = _state_update(
        request.session_id,
        lambda current: replace_state(current, turn=current.turn + 1),
    )
    turn = state.turn if state is not None else 0
    prompt = _prompt_text(request)
    skip_recall = (
        len(prompt.strip()) < _MIN_PROMPT_CHARS
        or _is_slash_or_affirmation(prompt)
    )
    hits: list[dict[str, Any]] = []
    routes: list[dict[str, Any]] = []
    if skip_recall:
        if (
            context.config.recall.routing_enabled
            and prompt.strip()
            and not _is_slash_or_affirmation(prompt)
        ):
            routes = _route_only(prompt, request, context, resolution)
    else:
        route_inputs, environment = _routing_inputs(
            request,
            context,
            resolution=resolution,
        )
        runtime_context = open_recall_runtime(context.vault)
        try:
            from symbiosis_brain.adapters.codex.recall import RecallRuntime

            with runtime_context as runtime:
                if isinstance(runtime, RecallRuntime):
                    hits, routes = prompt_recall(
                        prompt,
                        scope=_recall_scope(resolution),
                        allow_global=True,
                        memory_authorized=True,
                        config=context.config.recall,
                        runtime=runtime,
                        session_id=request.session_id or "",
                        routes=route_inputs,
                        routing_environment=environment,
                        vault=context.vault,
                    )
                elif context.config.recall.routing_enabled:
                    routes = _route_only(prompt, request, context, resolution)
        except Exception:
            raise

    discipline = codex_discipline_hint(
        turn=turn,
        interval=_DISCIPLINE_INTERVAL,
        routes_fired=bool(routes),
    )
    rendered = _render_prompt_context(hits, routes, discipline)
    return context_response(request.event, additional_context=rendered)


def _pre_tool(
    request: HookRequest,
    context: AdapterContext,
    resolution: ScopeResolution | None = None,
) -> dict[str, Any]:
    resolution = resolution or _resolve(request, context)
    tool_name = request.payload.get("tool_name")
    tool_input = request.payload.get("tool_input")
    if not isinstance(tool_name, str) or not isinstance(tool_input, Mapping):
        return context_response(request.event)
    query = normalize_tool_call(
        tool_name,
        tool_input,
        max_chars=context.config.recall.query_max_chars,
    )
    if not query:
        return context_response(request.event)
    try:
        with open_recall_runtime(context.vault) as runtime:
            block = action_recall(
                query,
                scope=_recall_scope(resolution),
                allow_global=True,
                memory_authorized=True,
                config=context.config.recall,
                runtime=runtime,
                session_id=request.session_id or "",
            )
    except Exception:
        block = ""
    return context_response(request.event, additional_context=block)


def _response_text(response: object) -> str:
    if not isinstance(response, Mapping):
        return ""
    content = response.get("content")
    if not isinstance(content, list):
        return ""
    texts: list[str] = []
    for item in content:
        if isinstance(item, Mapping) and isinstance(item.get("text"), str):
            texts.append(item["text"])
    return "\n".join(texts)


def _is_successful_save(payload: Mapping[str, Any]) -> bool:
    tool_name = payload.get("tool_name")
    if not isinstance(tool_name, str):
        return False
    marker = _SAVE_TOOL_MARKERS.get(tool_name)
    if marker is None:
        return False
    response = payload.get("tool_response")
    if not isinstance(response, Mapping):
        return False
    if response.get("isError") is not False:
        return False
    if response.get("error"):
        return False
    if any(
        response.get(key) is True
        for key in ("timeout", "timed_out", "cancelled", "canceled")
    ):
        return False
    text = _response_text(response)
    if not text or any(line.lstrip().startswith("Error:") for line in text.splitlines()):
        return False
    return any(line.lstrip().startswith(marker) for line in text.splitlines())


def _post_tool(
    request: HookRequest,
    context: AdapterContext,
    resolution: ScopeResolution | None = None,
) -> dict[str, Any]:
    if request.session_id and _is_successful_save(request.payload):
        _state_update(
            request.session_id,
            lambda state: replace_state(state, last_save_turn=state.turn),
        )
    return context_response(request.event)


def replace_state(state: SessionState, **changes: Any) -> SessionState:
    """Small local wrapper so all state writes remain allowlisted."""
    return SessionState(
        session_key=state.session_key,
        turn=changes.get("turn", state.turn),
        last_save_turn=changes.get("last_save_turn", state.last_save_turn),
        save_reminded_turn=changes.get(
            "save_reminded_turn",
            state.save_reminded_turn,
        ),
        compaction_pending=changes.get(
            "compaction_pending",
            state.compaction_pending,
        ),
    )


def _pre_compact(
    request: HookRequest,
    context: AdapterContext,
    resolution: ScopeResolution | None = None,
) -> dict[str, Any]:
    if request.session_id:
        _state_update(
            request.session_id,
            lambda state: replace_state(state, compaction_pending=True),
        )
    return context_response(request.event)


def _post_compact(
    request: HookRequest,
    context: AdapterContext,
    resolution: ScopeResolution | None = None,
) -> dict[str, Any]:
    if request.session_id:
        _state_update(
            request.session_id,
            lambda state: replace_state(state, compaction_pending=False),
        )
    return context_response(request.event)


def _stop(
    request: HookRequest,
    context: AdapterContext,
    resolution: ScopeResolution | None = None,
) -> dict[str, Any]:
    if request.payload.get("stop_hook_active") is True:
        return continue_response()
    try:
        with session_lock(request.session_id):
            state = load_state(request.session_id)
            interval = context.config.save_reminder_turns
            if state.turn < interval or state.turn % interval:
                return continue_response()
            zone = state.turn // interval
            if state.save_reminded_turn // interval >= zone:
                return continue_response()
            if (
                state.last_save_turn > 0
                and state.last_save_turn >= state.save_reminded_turn
            ):
                save_state(
                    replace_state(
                        state,
                        save_reminded_turn=state.turn,
                    )
                )
                return continue_response()
            save_state(
                replace_state(
                    state,
                    save_reminded_turn=state.turn,
                )
            )
            return continuation_response("Save a decision or handoff before stopping.")
    except Exception:
        return continue_response()


def _session_end(
    request: HookRequest,
    context: AdapterContext,
    resolution: ScopeResolution | None = None,
) -> dict[str, Any]:
    if request.session_id:
        try:
            with session_lock(request.session_id):
                delete_state(request.session_id)
        except Exception:
            pass
    if context.config.sync_mode == "auto":
        try:
            launch_auto_sync(context.vault, detach=False)
        except Exception:
            pass
    return continue_response()


HANDLERS: dict[HookEvent, Callable[..., dict[str, Any]]] = {
    HookEvent.SESSION_START: _session_start,
    HookEvent.USER_PROMPT_SUBMIT: _prompt,
    HookEvent.PRE_TOOL_USE: _pre_tool,
    HookEvent.POST_TOOL_USE: _post_tool,
    HookEvent.PRE_COMPACT: _pre_compact,
    HookEvent.POST_COMPACT: _post_compact,
    HookEvent.STOP: _stop,
    HookEvent.SESSION_END: _session_end,
}


def handle_event(
    request: HookRequest,
    context: AdapterContext,
) -> dict[str, Any]:
    started = time.perf_counter()
    resolution: ScopeResolution | None = None
    scope_source: str | None = None
    hits = 0
    failed = False
    try:
        handler = HANDLERS.get(request.event)
        if handler is None:
            return continue_response()
        if request.event in {
            HookEvent.SESSION_START,
            HookEvent.USER_PROMPT_SUBMIT,
            HookEvent.PRE_TOOL_USE,
        }:
            resolution = _resolve(request, context)
            scope_source = resolution.source
        result = handler(request, context, resolution)
        additional = result.get("hookSpecificOutput", {}).get("additionalContext")
        if isinstance(additional, str):
            hits = additional.count("\n- ")
        return result
    except Exception as exc:
        failed = True
        _diagnostic(
            request,
            scope_source=scope_source,
            started=started,
            hit_count=hits,
            degraded="internal_error",
            error_class=type(exc).__name__,
        )
        return continue_response()
    finally:
        if not failed:
            _diagnostic(
                request,
                scope_source=scope_source,
                started=started,
                hit_count=hits,
            )


def _safe_emit(value: Mapping[str, Any]) -> None:
    try:
        emit_response(value)
    except Exception:
        try:
            sys.stdout.write("{}\n")
        except Exception:
            pass


def _parse_cli_arguments(
    argv: Sequence[str],
) -> tuple[str, HookEvent] | None:
    values: dict[str, str] = {}
    items = list(argv)
    index = 0
    while index < len(items):
        option = items[index]
        if option not in {"--vault", "--event"} or option in values:
            return None
        index += 1
        if index >= len(items):
            return None
        value = items[index]
        if not isinstance(value, str) or not value or "\x00" in value:
            return None
        values[option] = value
        index += 1
    if set(values) != {"--vault", "--event"}:
        return None
    try:
        event = HookEvent(values["--event"])
    except (TypeError, ValueError):
        return None
    return values["--vault"], event


def _configure_standard_streams() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
        except (AttributeError, OSError):
            pass


def _worker_main_from_raw(argv: Sequence[str], raw: str) -> int:
    """Execute one already-bounded and validated hook request."""
    _configure_standard_streams()
    request: HookRequest | None = None
    started = time.perf_counter()
    try:
        parsed = _parse_cli_arguments(argv)
        if parsed is None:
            _safe_emit({})
            return 0
        vault_value, expected_event = parsed
        request = parse_hook_request(raw)
        if request is None or request.event is not expected_event:
            _safe_emit({})
            return 0
        vault = Path(vault_value).expanduser().resolve(strict=False)
        context = AdapterContext(
            vault=vault,
            config=load_codex_config(),
            now=datetime.now(timezone.utc),
        )
        _safe_emit(handle_event(request, context))
        return 0
    except Exception as exc:
        _diagnostic(
            request,
            scope_source=None,
            started=started,
            degraded="internal_error",
            error_class=type(exc).__name__,
        )
        _safe_emit(continue_response())
        return 0


def _worker_main(argv: Sequence[str]) -> int:
    """Compatibility entry for tests and the private worker module."""
    from symbiosis_brain.adapters.codex.supervisor import worker_main

    return worker_main(argv)


def run_cli(argv: Sequence[str]) -> int:
    """Compatibility wrapper around the stdlib-only cold supervisor."""
    from symbiosis_brain.adapters.codex.supervisor import run_cli as supervise

    return supervise(argv)
