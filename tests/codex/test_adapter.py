from __future__ import annotations

import io
import json
import os
import time
from contextlib import nullcontext
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from symbiosis_brain.adapters.codex import adapter as adapter_module
from symbiosis_brain.adapters.codex import supervisor as supervisor_module
from symbiosis_brain.adapters.codex.adapter import (
    AdapterContext,
    handle_event,
    run_cli,
)
from symbiosis_brain.adapters.codex.bootstrap import BootstrapResult
from symbiosis_brain.adapters.codex.config import CodexConfig, TrustedProject
from symbiosis_brain.adapters.codex.protocol import HookEvent, HookRequest
from symbiosis_brain.adapters.codex.recall import RecallRuntime
from symbiosis_brain.adapters.codex.scope import ScopeResolution
from symbiosis_brain.adapters.codex.session_state import (
    SessionState,
    load_state,
    save_state,
    session_key,
    state_path,
)
from symbiosis_brain.recall_dedup import _safe_session
from symbiosis_brain.atomic_write import atomic_write_text
from symbiosis_brain.adapters.codex import session_state as state_module
from symbiosis_brain.pre_action_config import PreActionConfig


NOW = datetime(2026, 10, 10, 12, 0, tzinfo=timezone.utc)


def _config(
    project: Path | None = None,
    *,
    visibility: str = "one-line",
    sync_mode: str = "manual",
    reminder_turns: int = 10,
) -> CodexConfig:
    trusted = ()
    if project is not None:
        trusted = (
            TrustedProject(
                root=project.resolve(),
                allowed_scopes=frozenset({"demo"}),
                allow_global=False,
            ),
        )
    return CodexConfig(
        bootstrap_visibility=visibility,  # type: ignore[arg-type]
        trusted_projects=trusted,
        handoff_stale_days=30,
        save_reminder_turns=reminder_turns,
        status_line_policy="recommend-only",
        sync_mode=sync_mode,  # type: ignore[arg-type]
        sync_bash_path=None,
        recall=PreActionConfig(),
    )


def _request(
    event: HookEvent,
    *,
    session_id: str | None = "session-1",
    cwd: Path | None = None,
    payload: dict | None = None,
) -> HookRequest:
    return HookRequest(
        event=event,
        session_id=session_id,
        turn_id="turn-1",
        cwd=cwd or Path.cwd(),
        model="gpt-test",
        permission_mode="default",
        payload=payload or {},
    )


def _context(
    tmp_path: Path,
    *,
    project: Path | None = None,
    visibility: str = "one-line",
    sync_mode: str = "manual",
    reminder_turns: int = 10,
) -> AdapterContext:
    return AdapterContext(
        vault=tmp_path / "vault",
        config=_config(
            project,
            visibility=visibility,
            sync_mode=sync_mode,
            reminder_turns=reminder_turns,
        ),
        now=NOW,
    )


def _trusted_resolution(project: Path, *, scope: str | None = "demo") -> ScopeResolution:
    return ScopeResolution(
        scope=scope,
        source="basename",
        marker_path=None,
        depth=0,
        warning=None,
        trusted=True,
        allow_global=True,
        memory_authorized=True,
    )


def _bootstrap(context: str = "bootstrap", warning: str | None = None) -> BootstrapResult:
    return BootstrapResult(
        context=context,
        scope="demo",
        handoff=None,
        related_count=0,
        warning=warning,
    )


@pytest.fixture(autouse=True)
def _isolated_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))
    monkeypatch.setenv("TMPDIR", str(tmp_path / "temp"))
    monkeypatch.setenv("TEMP", str(tmp_path / "temp"))
    (tmp_path / "temp").mkdir()
    if os.name == "nt":
        monkeypatch.setattr(
            state_module,
            "_apply_and_verify_windows_acls",
            lambda *args, **kwargs: None,
        )


def test_session_start_ignores_legacy_trust_flags_and_builds_global_bootstrap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    calls: list[str] = []
    monkeypatch.setattr(
        adapter_module,
        "resolve_scope",
        lambda *args, **kwargs: ScopeResolution(
            scope=None,
            source="none",
            marker_path=None,
            depth=None,
            warning=None,
            trusted=False,
            allow_global=False,
            memory_authorized=False,
        ),
    )
    monkeypatch.setattr(
        adapter_module,
        "build_bootstrap",
        lambda *args, **kwargs: calls.append("bootstrap") or _bootstrap(),
    )
    monkeypatch.setattr(
        adapter_module,
        "open_recall_runtime",
        lambda *args, **kwargs: calls.append("runtime"),
    )
    monkeypatch.setattr(
        adapter_module,
        "_launch_prewarm",
        lambda *args, **kwargs: calls.append("prewarm"),
    )

    result = handle_event(
        _request(HookEvent.SESSION_START, cwd=tmp_path / "unknown"),
        _context(tmp_path),
    )

    assert result["hookSpecificOutput"]["hookEventName"] == "SessionStart"
    assert result["hookSpecificOutput"]["additionalContext"] == "bootstrap"
    assert calls == ["bootstrap", "prewarm"]


def test_session_start_builds_bootstrap_then_starts_nonblocking_prewarm(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    project = tmp_path / "project"
    project.mkdir()
    events: list[str] = []
    monkeypatch.setattr(
        adapter_module,
        "resolve_scope",
        lambda *args, **kwargs: events.append("resolve") or _trusted_resolution(project),
    )
    monkeypatch.setattr(
        adapter_module,
        "build_bootstrap",
        lambda *args, **kwargs: events.append(f"bootstrap:{kwargs['source']}")
        or _bootstrap("safe bootstrap"),
    )
    monkeypatch.setattr(
        adapter_module,
        "_launch_prewarm",
        lambda vault: events.append(f"prewarm:{vault.name}"),
    )

    result = handle_event(
        _request(HookEvent.SESSION_START, cwd=project),
        _context(tmp_path, project=project),
    )

    assert result["hookSpecificOutput"]["additionalContext"] == "safe bootstrap"
    assert result["systemMessage"] == "SB ✓"
    assert events == ["resolve", "bootstrap:startup", "prewarm:vault"]
    assert load_state("session-1").turn == 0
    assert not hasattr(load_state("session-1"), "scope")


def test_prewarm_stays_in_supervisor_process_group_on_posix(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    calls: list[tuple[list[str], dict[str, object]]] = []
    monkeypatch.setattr(adapter_module, "os", SimpleNamespace(name="posix"))
    monkeypatch.setattr(
        adapter_module.subprocess,
        "Popen",
        lambda command, **kwargs: calls.append((command, kwargs))
        or SimpleNamespace(),
    )

    assert adapter_module._launch_prewarm(tmp_path / "vault") is True

    assert calls == [(
        [
            adapter_module.sys.executable,
            "-B",
            "-m",
            "symbiosis_brain",
            "prewarm",
            "--vault",
            str(tmp_path / "vault"),
        ],
        {
            "stdin": adapter_module.subprocess.DEVNULL,
            "stdout": adapter_module.subprocess.DEVNULL,
            "stderr": adapter_module.subprocess.DEVNULL,
            "close_fds": True,
        },
    )]


def test_session_start_missing_session_id_returns_context_without_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setattr(
        adapter_module,
        "resolve_scope",
        lambda *args, **kwargs: _trusted_resolution(project),
    )
    monkeypatch.setattr(
        adapter_module,
        "build_bootstrap",
        lambda *args, **kwargs: _bootstrap("startup context"),
    )
    monkeypatch.setattr(adapter_module, "_launch_prewarm", lambda *args: True)
    result = handle_event(
        _request(HookEvent.SESSION_START, session_id=None, cwd=project),
        _context(tmp_path, project=project),
    )
    assert result["hookSpecificOutput"]["additionalContext"] == "startup context"
    assert not state_path("other-session").exists()


def test_compact_builds_exactly_one_compact_bootstrap_and_postcompact_does_not(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    project = tmp_path / "project"
    project.mkdir()
    sources: list[str] = []
    monkeypatch.setattr(
        adapter_module,
        "resolve_scope",
        lambda *args, **kwargs: _trusted_resolution(project),
    )
    monkeypatch.setattr(
        adapter_module,
        "build_bootstrap",
        lambda *args, **kwargs: sources.append(kwargs["source"]) or _bootstrap("compact"),
    )

    compact = handle_event(
        _request(HookEvent.SESSION_START, cwd=project, payload={"source": "compact"}),
        _context(tmp_path, project=project),
    )
    post = handle_event(
        _request(HookEvent.POST_COMPACT, cwd=project),
        _context(tmp_path, project=project),
    )

    assert compact["hookSpecificOutput"]["additionalContext"] == "compact"
    assert post["hookSpecificOutput"]["hookEventName"] == "PostCompact"
    assert sources == ["compact"]


def test_prompt_unknown_scope_still_uses_canonical_global_recall(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    project = tmp_path / "project"
    unknown = tmp_path / "unknown"
    project.mkdir()
    unknown.mkdir()
    calls: list[dict[str, object]] = []
    monkeypatch.setattr(
        adapter_module,
        "resolve_scope",
        lambda *args, **kwargs: ScopeResolution(
            scope=None,
            source="none",
            marker_path=None,
            depth=None,
            warning=None,
            trusted=False,
            allow_global=False,
            memory_authorized=False,
        ),
    )
    monkeypatch.setattr(
        adapter_module,
        "open_recall_runtime",
        lambda vault: nullcontext(
            RecallRuntime(storage=SimpleNamespace(), engine=SimpleNamespace())
        ),
    )
    monkeypatch.setattr(
        adapter_module,
        "prompt_recall",
        lambda *args, **kwargs: calls.append(dict(kwargs))
        or (
            [{
                "path": "global/reference.md",
                "title": "Global reference",
                "gist": "available without project scope",
            }],
            [],
        ),
    )

    result = handle_event(
        _request(
            HookEvent.USER_PROMPT_SUBMIT,
            cwd=unknown,
            payload={"prompt": "find the global decision"},
        ),
        _context(tmp_path, project=project),
    )

    assert "Global reference" in result["hookSpecificOutput"]["additionalContext"]
    assert len(calls) == 1
    assert calls[0]["scope"] == "global"
    assert calls[0]["allow_global"] is True
    assert calls[0]["memory_authorized"] is True


def test_prompt_skips_short_slash_and_affirmation_recall_but_can_route(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    project = tmp_path / "project"
    project.mkdir()
    prompts: list[str] = []
    monkeypatch.setattr(
        adapter_module,
        "resolve_scope",
        lambda *args, **kwargs: _trusted_resolution(project),
    )
    monkeypatch.setattr(
        adapter_module,
        "prompt_recall",
        lambda prompt, **kwargs: prompts.append(prompt) or ([], [{"id": "r1", "class": "augment", "hint": "Use tests."}]),
    )
    monkeypatch.setattr(
        adapter_module,
        "_route_only",
        lambda *args, **kwargs: [{"id": "r1", "class": "augment", "hint": "Use tests."}],
    )
    context = _context(tmp_path, project=project)
    for index, prompt in enumerate(("hi", "/help", "yes")):
        result = handle_event(
            _request(
                HookEvent.USER_PROMPT_SUBMIT,
                cwd=project,
                payload={"prompt": prompt},
            ),
            context,
        )
        rendered = result["hookSpecificOutput"].get("additionalContext", "")
        if index == 0:
            assert "Use tests." in rendered
        else:
            assert rendered == ""
    assert prompts == []


def test_prompt_context_contains_memory_routes_and_discipline_under_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setattr(
        adapter_module,
        "resolve_scope",
        lambda *args, **kwargs: _trusted_resolution(project),
    )
    monkeypatch.setattr(
        adapter_module,
        "prompt_recall",
        lambda *args, **kwargs: (
            [{"path": "demo/decision.md", "title": "Decision", "gist": "safe gist"}],
            [
                {"id": "supersede", "class": "supersede", "hint": "Use tests first."},
                {"id": "duplicate", "class": "augment", "hint": "Use tests first."},
            ],
        ),
    )
    context = _context(tmp_path, project=project)
    context.config.recall.routing_enabled = False
    monkeypatch.setattr(
        adapter_module,
        "open_recall_runtime",
        lambda vault: nullcontext(
            RecallRuntime(storage=SimpleNamespace(), engine=SimpleNamespace())
        ),
    )
    result = handle_event(
        _request(
            HookEvent.USER_PROMPT_SUBMIT,
            cwd=project,
            payload={"prompt": "A sufficiently long prompt about the decision"},
        ),
        context,
    )
    rendered = result["hookSpecificOutput"]["additionalContext"]
    assert "[memory:" in rendered
    assert "Use tests first." in rendered
    assert rendered.count("Use tests first.") == 1
    assert "Before broad code search" in rendered
    assert len(rendered.encode("utf-8")) < 1500


def test_pretool_uses_normalizer_and_returns_only_advisory_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    project = tmp_path / "project"
    project.mkdir()
    calls: list[str] = []
    monkeypatch.setattr(
        adapter_module,
        "resolve_scope",
        lambda *args, **kwargs: _trusted_resolution(project),
    )
    monkeypatch.setattr(
        adapter_module,
        "normalize_tool_call",
        lambda *args, **kwargs: calls.append("normalize") or "safe query",
    )
    monkeypatch.setattr(
        adapter_module,
        "action_recall",
        lambda *args, **kwargs: calls.append("recall") or "[recall: 1 hit]",
    )
    result = handle_event(
        _request(
            HookEvent.PRE_TOOL_USE,
            cwd=project,
            payload={"tool_name": "Bash", "tool_input": {"command": "git status"}},
        ),
        _context(tmp_path, project=project),
    )
    assert result["hookSpecificOutput"]["additionalContext"] == "[recall: 1 hit]"
    assert set(result) == {"continue", "hookSpecificOutput"}
    assert calls == ["normalize", "recall"]


@pytest.mark.parametrize(
    ("tool_name", "marker"),
    [
        ("mcp__symbiosis-brain__brain_write", "Saved:"),
        ("mcp__symbiosis-brain__brain_append", "Appended"),
        ("mcp__symbiosis-brain__brain_patch", "Patched"),
        ("mcp__symbiosis-brain__brain_sync", "Sync complete:"),
        ("mcp__symbiosis_brain__brain_write", "Saved:"),
        ("mcp__symbiosis_brain__brain_append", "Appended"),
        ("mcp__symbiosis_brain__brain_patch", "Patched"),
        ("mcp__symbiosis_brain__brain_sync", "Sync complete:"),
    ],
)
def test_posttool_success_markers_update_last_save_turn(
    tmp_path: Path,
    tool_name: str,
    marker: str,
):
    context = _context(tmp_path)
    save_state(
        SessionState(
            session_key=session_key("session-1"),
            turn=7,
        )
    )
    result = handle_event(
        _request(
            HookEvent.POST_TOOL_USE,
            payload={
                "tool_name": tool_name,
                "tool_response": {
                    "isError": False,
                    "content": [{"type": "text", "text": f"{marker} demo/decision.md"}],
                },
            },
        ),
        context,
    )
    assert result == {"continue": True, "hookSpecificOutput": {"hookEventName": "PostToolUse"}}
    assert load_state("session-1").last_save_turn == 7


@pytest.mark.parametrize(
    ("tool_name", "response"),
    [
        (
            "mcp__symbiosis-brain__brain_write",
            {"isError": True, "content": [{"type": "text", "text": "Saved: fake"}]},
        ),
        (
            "mcp__symbiosis-brain__brain_write",
            {"isError": False, "error": "failed", "content": []},
        ),
        (
            "mcp__symbiosis_brain__brain_write",
            {"isError": False, "content": [{"type": "text", "text": "Error: failed"}]},
        ),
        (
            "mcp__other__brain_write",
            {"isError": False, "content": [{"type": "text", "text": "Saved: fake"}]},
        ),
        (
            "mcp__symbiosis_brain_extra__brain_write",
            {"isError": False, "content": [{"type": "text", "text": "Saved: fake"}]},
        ),
        (
            "prefix_mcp__symbiosis_brain__brain_write",
            {"isError": False, "content": [{"type": "text", "text": "Saved: fake"}]},
        ),
        ("mcp__symbiosis-brain__brain_write", None),
    ],
)
def test_posttool_does_not_mark_failed_or_nonbrain_saves(
    tmp_path: Path, tool_name: str, response: object
):
    context = _context(tmp_path)
    save_state(
        SessionState(
            session_key=session_key("session-1"),
            turn=7,
            last_save_turn=2,
        )
    )
    handle_event(
        _request(
            HookEvent.POST_TOOL_USE,
            payload={"tool_name": tool_name, "tool_response": response},
        ),
        context,
    )
    assert load_state("session-1").last_save_turn == 2


def test_initial_zero_save_state_does_not_suppress_first_reminder(
    tmp_path: Path,
):
    context = _context(tmp_path, reminder_turns=10)
    save_state(SessionState(session_key=session_key("session-1"), turn=10))
    result = handle_event(_request(HookEvent.STOP), context)
    assert result["decision"] == "block"
    assert load_state("session-1").save_reminded_turn == 10


def test_repeated_stop_in_same_reminder_zone_continues(
    tmp_path: Path,
):
    context = _context(tmp_path, reminder_turns=10)
    save_state(SessionState(session_key=session_key("session-1"), turn=10))
    first = handle_event(_request(HookEvent.STOP), context)
    second = handle_event(_request(HookEvent.STOP), context)
    assert first["decision"] == "block"
    assert second == {"continue": True}


def test_save_before_first_reminder_suppresses_that_reminder(
    tmp_path: Path,
):
    context = _context(tmp_path, reminder_turns=10)
    save_state(SessionState(session_key=session_key("session-1"), turn=10))

    handle_event(
        _request(
            HookEvent.POST_TOOL_USE,
            payload={
                "tool_name": "mcp__symbiosis-brain__brain_write",
                "tool_response": {
                    "isError": False,
                    "content": [{"type": "text", "text": "Saved: demo/decision.md"}],
                },
            },
        ),
        context,
    )

    result = handle_event(_request(HookEvent.STOP), context)

    assert result == {"continue": True}
    assert load_state("session-1").save_reminded_turn == 10


def test_save_at_same_turn_as_reminder_suppresses_next_reminder_zone(
    tmp_path: Path,
):
    context = _context(tmp_path, reminder_turns=10)
    save_state(SessionState(session_key=session_key("session-1"), turn=10))

    first_stop = handle_event(_request(HookEvent.STOP), context)
    assert first_stop["decision"] == "block"

    handle_event(
        _request(
            HookEvent.POST_TOOL_USE,
            payload={
                "tool_name": "mcp__symbiosis-brain__brain_write",
                "tool_response": {
                    "isError": False,
                    "content": [{"type": "text", "text": "Saved: demo/decision.md"}],
                },
            },
        ),
        context,
    )
    save_state(
        replace(
            load_state("session-1"),
            turn=20,
        )
    )

    second_stop = handle_event(_request(HookEvent.STOP), context)

    assert second_stop == {"continue": True}
    assert load_state("session-1").save_reminded_turn == 20


def test_stop_hook_active_always_continues_and_precompact_never_blocks(
    tmp_path: Path,
):
    context = _context(tmp_path)
    save_state(SessionState(session_key=session_key("session-1"), turn=10))
    assert handle_event(
        _request(HookEvent.STOP, payload={"stop_hook_active": True}),
        context,
    ) == {"continue": True}
    result = handle_event(_request(HookEvent.PRE_COMPACT), context)
    assert result == {"continue": True, "hookSpecificOutput": {"hookEventName": "PreCompact"}}
    assert load_state("session-1").compaction_pending is True


def test_postcompact_clears_only_compaction_flag(tmp_path: Path):
    context = _context(tmp_path)
    save_state(
        SessionState(
            session_key=session_key("session-1"),
            turn=4,
            last_save_turn=3,
            save_reminded_turn=4,
            compaction_pending=True,
        )
    )
    handle_event(_request(HookEvent.POST_COMPACT), context)
    assert load_state("session-1") == SessionState(
        session_key=session_key("session-1"),
        turn=4,
        last_save_turn=3,
        save_reminded_turn=4,
        compaction_pending=False,
    )


def test_session_end_manual_deletes_state_without_sync(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    calls: list[Path] = []
    monkeypatch.setattr(adapter_module, "launch_auto_sync", lambda vault: calls.append(vault))
    context = _context(tmp_path, sync_mode="manual")
    save_state(SessionState(session_key=session_key("session-1"), turn=2))
    handle_event(_request(HookEvent.SESSION_END), context)
    assert not os.path.exists(state_module._native_path(state_path("session-1")))
    assert calls == []


def test_session_end_only_deletes_ending_session_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    context = _context(tmp_path, sync_mode="manual")
    save_state(SessionState(session_key=session_key("session-1"), turn=2))
    save_state(SessionState(session_key=session_key("session-2"), turn=2))

    handle_event(
        _request(HookEvent.SESSION_END, session_id="session-1"),
        context,
    )

    assert not os.path.exists(state_module._native_path(state_path("session-1")))
    assert os.path.exists(state_module._native_path(state_path("session-2")))


def test_session_end_deletes_recall_dedup_artifacts_for_ending_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    context = _context(tmp_path, sync_mode="manual")
    session_id = "session-1"
    save_state(SessionState(session_key=session_key(session_id), turn=2))
    seen_token = _safe_session(session_key(session_id)[:32])
    state_dir = state_path(session_id).parent
    for prefix in ("brain-codex-prompt-seen-", "brain-codex-action-seen-"):
        atomic_write_text(state_dir / f"{prefix}{seen_token}.json", "{}")

    handle_event(_request(HookEvent.SESSION_END, session_id=session_id), context)

    assert not os.path.exists(
        state_module._native_path(
            state_dir / f"brain-codex-prompt-seen-{seen_token}.json"
        )
    )
    assert not os.path.exists(
        state_module._native_path(
            state_dir / f"brain-codex-action-seen-{seen_token}.json"
        )
    )


def test_session_end_deletes_private_route_dedup_artifact(tmp_path: Path):
    context = _context(tmp_path, sync_mode="manual")
    session_id = "session-route"
    save_state(SessionState(session_key=session_key(session_id), turn=2))
    seen_token = _safe_session(session_key(session_id)[:32])
    route_path = state_path(session_id).parent / f"brain-codex-route-seen-{seen_token}.json"
    atomic_write_text(route_path, "{}")

    handle_event(_request(HookEvent.SESSION_END, session_id=session_id), context)

    assert not route_path.exists()


def test_session_end_auto_launches_sync_exactly_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    calls: list[tuple[Path, bool]] = []
    monkeypatch.setattr(
        adapter_module,
        "launch_auto_sync",
        lambda vault, *, detach: calls.append((vault, detach)) or True,
    )
    context = _context(tmp_path, sync_mode="auto")
    save_state(SessionState(session_key=session_key("session-1"), turn=2))
    handle_event(_request(HookEvent.SESSION_END), context)
    assert calls == [(context.vault, False)]


def test_session_end_auto_sync_does_not_run_prerequisite_probe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    context = _context(tmp_path, sync_mode="auto")
    calls: list[tuple[Path, bool]] = []
    monkeypatch.setattr(
        adapter_module,
        "launch_auto_sync",
        lambda vault, *, detach: calls.append((vault, detach)) or True,
    )
    handle_event(_request(HookEvent.SESSION_END), context)
    assert calls == [(context.vault, False)]


def test_internal_deadlines_match_codex_outer_budgets():
    assert adapter_module._INTERNAL_DEADLINES == {
        HookEvent.SESSION_START: 25.0,
        HookEvent.USER_PROMPT_SUBMIT: 12.0,
        HookEvent.PRE_TOOL_USE: 12.0,
        HookEvent.POST_TOOL_USE: 3.0,
        HookEvent.PRE_COMPACT: 3.0,
        HookEvent.POST_COMPACT: 3.0,
        HookEvent.STOP: 3.0,
        HookEvent.SESSION_END: 2.0,
    }


def test_run_cli_is_fail_open_for_malformed_missing_and_handler_failure(
    monkeypatch: pytest.MonkeyPatch,
):
    out = io.StringIO()
    monkeypatch.setattr(adapter_module.sys, "stdout", out)
    monkeypatch.setattr(adapter_module.sys, "stdin", io.StringIO("{"))
    assert run_cli(["--vault", "vault", "--event", "Stop"]) == 0
    assert json.loads(out.getvalue()) == {}

    out.seek(0)
    out.truncate(0)
    monkeypatch.setattr(adapter_module.sys, "stdin", io.StringIO(
        json.dumps({"hook_event_name": "FutureEvent"})
    ))
    assert run_cli(["--vault", "vault", "--event", "Stop"]) == 0
    assert json.loads(out.getvalue()) == {}

    out.seek(0)
    out.truncate(0)
    monkeypatch.setattr(adapter_module.sys, "stdin", io.StringIO(
        json.dumps({"hook_event_name": "Stop", "session_id": "safe"})
    ))
    monkeypatch.setattr(
        adapter_module,
        "load_codex_config",
        lambda: _config(),
    )
    monkeypatch.setattr(adapter_module, "handle_event", lambda *args: (_ for _ in ()).throw(RuntimeError("secret")))
    assert adapter_module._worker_main(
        ["--vault", "vault", "--event", "Stop"]
    ) == 0
    assert json.loads(out.getvalue()) == {"continue": True}


def test_run_cli_handles_lone_surrogate_without_traceback(
    monkeypatch: pytest.MonkeyPatch,
):
    out = io.StringIO()
    monkeypatch.setattr(adapter_module.sys, "stdout", out)
    monkeypatch.setattr(adapter_module.sys, "stdin", io.StringIO(
        '{"hook_event_name":"FutureEvent","prompt":"\\ud800"}'
    ))
    assert run_cli(["--vault", "vault", "--event", "Stop"]) == 0
    assert json.loads(out.getvalue()) == {}


def test_stdin_reader_requests_a_bounded_payload(
    monkeypatch: pytest.MonkeyPatch,
):
    requested: list[int] = []

    class BoundedInput:
        def read(self, size: int = -1) -> str:
            requested.append(size)
            return "x" * size

    monkeypatch.setattr(supervisor_module.sys, "stdin", BoundedInput())
    monkeypatch.setattr(supervisor_module, "_MAX_STDIN_BYTES", 128)

    received = supervisor_module._read_input_before(time.monotonic() + 1)

    assert requested == [supervisor_module._READ_CHUNK_CHARS]
    assert received is not None
    raw, oversized = received
    assert raw == b""
    assert oversized is True


def test_run_cli_outer_exception_diagnostic_uses_fixed_error_code_and_no_details(
    monkeypatch: pytest.MonkeyPatch,
):
    out = io.StringIO()
    diagnostics: list[str] = []
    monkeypatch.setattr(adapter_module.sys, "stdout", out)
    monkeypatch.setattr(
        adapter_module.sys,
        "stdin",
        io.StringIO(
            json.dumps({"hook_event_name": "Stop", "session_id": "PRIVATE-SESSION"})
        ),
    )
    monkeypatch.setattr(adapter_module, "_debug_log", diagnostics.append)
    monkeypatch.setattr(
        adapter_module,
        "load_codex_config",
        lambda: (_ for _ in ()).throw(
            RuntimeError("PRIVATE-EXCEPTION-DETAIL")
        ),
    )

    assert adapter_module._worker_main(
        ["--vault", "PRIVATE-VAULT", "--event", "Stop"]
    ) == 0

    assert json.loads(out.getvalue()) == {"continue": True}
    text = "\n".join(diagnostics)
    assert "event=Stop" in text
    assert "session=1b694f93825cf87f" in text
    assert "scope_source=unknown" in text
    assert "degraded=internal_error" in text
    assert "error_class=RuntimeError" in text
    assert "error_code=internal_error" in text
    assert "PRIVATE-" not in text


def test_diagnostics_are_allowlisted_and_never_include_private_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    lines: list[str] = []
    monkeypatch.setattr(adapter_module, "_debug_log", lines.append)
    project = tmp_path / "private-project"
    project.mkdir()
    request = _request(
        HookEvent.USER_PROMPT_SUBMIT,
        cwd=project,
        payload={
            "prompt": "PRIVATE-PROMPT with enough length",
            "tool_input": {"query": "PRIVATE-QUERY"},
        },
    )
    context = _context(tmp_path, project=project)
    context.config.recall.routing_enabled = False
    monkeypatch.setattr(
        adapter_module,
        "open_recall_runtime",
        lambda vault: nullcontext(
            RecallRuntime(storage=SimpleNamespace(), engine=SimpleNamespace())
        ),
    )
    monkeypatch.setattr(
        adapter_module,
        "resolve_scope",
        lambda *args, **kwargs: _trusted_resolution(project),
    )
    monkeypatch.setattr(
        adapter_module,
        "prompt_recall",
        lambda *args, **kwargs: (_ for _ in ()).throw(ValueError("PRIVATE-EXCEPTION")),
    )
    handle_event(request, context)
    text = "\n".join(lines)
    assert "PRIVATE-" not in text
    assert "private-project" not in text
    assert "event=UserPromptSubmit" in text
    assert "scope_source=basename" in text
    assert "error_class=ValueError" in text
    assert "error_code=internal_error" in text
