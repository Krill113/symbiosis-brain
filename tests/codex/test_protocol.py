import io
import json
import re
import sys
from pathlib import Path
from types import ModuleType

import pytest

from symbiosis_brain.adapters.codex.protocol import (
    ADDITIONAL_CONTEXT_LIMITS,
    HookEvent,
    context_response,
    continuation_response,
    continue_response,
    emit_response,
    parse_hook_request,
)


def test_parse_live_session_id_shape():
    raw = json.dumps({
        "session_id": "thr-live",
        "turn_id": "turn-1",
        "cwd": "C:/workspace/project",
        "hook_event_name": "PreToolUse",
        "model": "gpt-test",
        "permission_mode": "default",
        "tool_name": "Bash",
        "tool_input": {"command": "git status"},
    })
    req = parse_hook_request(raw)
    assert req is not None
    assert req.event is HookEvent.PRE_TOOL_USE
    assert req.session_id == "thr-live"
    assert req.turn_id == "turn-1"
    assert req.cwd == Path("C:/workspace/project")
    assert req.model == "gpt-test"
    assert req.permission_mode == "default"
    assert req.payload["tool_name"] == "Bash"
    assert req.payload["tool_input"] == {"command": "git status"}


def test_parse_thread_id_alias():
    req = parse_hook_request(json.dumps({
        "thread_id": "thr-future",
        "cwd": "/workspace",
        "hook_event_name": "SessionStart",
        "source": "startup",
    }))
    assert req is not None
    assert req.session_id == "thr-future"


def test_session_id_wins_over_thread_id():
    req = parse_hook_request(json.dumps({
        "session_id": "preferred",
        "thread_id": "alias",
        "cwd": "/workspace",
        "hook_event_name": "SessionStart",
    }))
    assert req is not None
    assert req.session_id == "preferred"


def test_missing_ids_do_not_invent_default():
    req = parse_hook_request(json.dumps({
        "cwd": "/workspace",
        "hook_event_name": "Stop",
    }))
    assert req is not None
    assert req.session_id is None


@pytest.mark.parametrize("cwd", [None, "", 42, [], {}])
def test_missing_or_invalid_cwd_uses_process_cwd(cwd):
    payload = {"cwd": cwd, "hook_event_name": "Stop"}
    req = parse_hook_request(json.dumps(payload))
    assert req is not None
    assert req.cwd == Path.cwd()


def test_unknown_event_is_fail_open():
    assert parse_hook_request(json.dumps({
        "cwd": "/workspace",
        "hook_event_name": "FutureEvent",
    })) is None


@pytest.mark.parametrize("raw", ["", "{", "[]", "\"text\"", "{} {}"])
def test_malformed_or_non_object_is_fail_open(raw):
    assert parse_hook_request(raw) is None


def test_parse_failure_does_not_echo_raw_payload(capsys):
    assert parse_hook_request('{"secret": "do-not-echo"') is None
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


def test_context_response_uses_codex_hook_envelope():
    assert context_response(
        HookEvent.PRE_TOOL_USE,
        additional_context="memory",
        system_message="SB ✓",
    ) == {
        "continue": True,
        "systemMessage": "SB ✓",
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "additionalContext": "memory",
        },
    }


def test_response_helpers_omit_empty_optional_fields():
    assert context_response(
        HookEvent.POST_TOOL_USE,
        additional_context="",
        system_message=None,
    ) == {
        "continue": True,
        "hookSpecificOutput": {"hookEventName": "PostToolUse"},
    }
    assert continue_response(system_message="") == {"continue": True}


def test_stop_continuation_shape():
    assert continuation_response("save first") == {
        "decision": "block",
        "reason": "save first",
    }


@pytest.mark.parametrize("event", list(HookEvent))
def test_hook_event_protocol_matrix(event, monkeypatch):
    base = {
        "cwd": "/workspace",
        "hook_event_name": event.value,
        "unknown_future_field": {"kept": True},
    }

    with_session = parse_hook_request(json.dumps({
        **base,
        "session_id": "session-live",
    }))
    assert with_session is not None
    assert with_session.event is event
    assert with_session.session_id == "session-live"
    assert with_session.turn_id is None
    assert with_session.model is None
    assert with_session.permission_mode is None
    assert with_session.payload["unknown_future_field"] == {"kept": True}

    with_thread = parse_hook_request(json.dumps({
        **base,
        "thread_id": "thread-future",
    }))
    assert with_thread is not None
    assert with_thread.session_id == "thread-future"

    without_id = parse_hook_request(json.dumps(base))
    assert without_id is not None
    assert without_id.session_id is None

    assert parse_hook_request(f'{{"hook_event_name": "{event.value}"') is None
    expected_response = {
        "continue": True,
        "hookSpecificOutput": {"hookEventName": event.value},
    }
    response = context_response(event)
    assert response == expected_response

    stdout = io.StringIO()
    monkeypatch.setattr(sys, "stdout", stdout)
    emit_response(response)
    serialized = stdout.getvalue()
    assert serialized.endswith("\n")
    assert serialized.count("\n") == 1
    assert json.loads(serialized) == expected_response


@pytest.mark.parametrize("event", [
    HookEvent.SESSION_START,
    HookEvent.USER_PROMPT_SUBMIT,
    HookEvent.PRE_TOOL_USE,
])
def test_private_context_is_bounded_before_serialization(event):
    limit = ADDITIONAL_CONTEXT_LIMITS[event]
    oversized = "complete sentence. " * limit
    response = context_response(event, additional_context=oversized)
    context = response["hookSpecificOutput"]["additionalContext"]

    assert (len(context) + 2) // 3 < limit
    assert context.endswith(".")
    assert set(response) == {"continue", "hookSpecificOutput"}
    assert set(response["hookSpecificOutput"]) == {
        "hookEventName",
        "additionalContext",
    }


def test_context_limit_is_an_estimated_token_budget_not_utf8_bytes():
    cyrillic = "Привет мир. " * 500
    response = context_response(
        HookEvent.SESSION_START,
        additional_context=cyrillic,
    )
    value = response["hookSpecificOutput"]["additionalContext"]
    assert len(value.encode("utf-8")) > ADDITIONAL_CONTEXT_LIMITS[HookEvent.SESSION_START]
    assert (len(value) + 2) // 3 < ADDITIONAL_CONTEXT_LIMITS[HookEvent.SESSION_START]


def test_context_limit_stays_strictly_below_event_budget_for_ascii():
    value = context_response(
        HookEvent.PRE_TOOL_USE,
        additional_context=("complete sentence. " * 500),
    )["hookSpecificOutput"]["additionalContext"]
    assert (len(value) + 2) // 3 < ADDITIONAL_CONTEXT_LIMITS[HookEvent.PRE_TOOL_USE]


def test_emit_response_is_ascii_json_with_one_newline(monkeypatch):
    stdout = io.StringIO()
    monkeypatch.setattr(sys, "stdout", stdout)

    emit_response({"systemMessage": "SB ✓"})

    assert stdout.getvalue() == '{"systemMessage": "SB \\u2713"}\n'


def _fake_supervisor(result: int, calls: list[list[str]]) -> ModuleType:
    module = ModuleType("symbiosis_brain.adapters.codex.supervisor")

    def run_cli(argv):
        calls.append(argv)
        return result

    module.run_cli = run_cli
    return module


def test_module_main_dispatches_codex_hook_lazily(monkeypatch):
    from symbiosis_brain import __main__ as module_main

    calls = []
    monkeypatch.setitem(
        sys.modules,
        "symbiosis_brain.adapters.codex.supervisor",
        _fake_supervisor(23, calls),
    )
    monkeypatch.setattr(
        sys,
        "argv",
        ["python -m symbiosis_brain", "codex-hook", "--vault", "/vault"],
    )

    with pytest.raises(SystemExit) as exit_info:
        module_main.main()

    assert exit_info.value.code == 23
    assert calls == [["--vault", "/vault"]]


def test_installer_cli_dispatches_codex_hook_lazily(monkeypatch):
    from symbiosis_brain import install_cli

    calls = []
    monkeypatch.setitem(
        sys.modules,
        "symbiosis_brain.adapters.codex.supervisor",
        _fake_supervisor(29, calls),
    )
    monkeypatch.setattr(
        sys,
        "argv",
        ["symbiosis-brain", "codex-hook", "--vault", "/vault"],
    )

    with pytest.raises(SystemExit) as exit_info:
        install_cli.main()

    assert exit_info.value.code == 29
    assert calls == [["--vault", "/vault"]]


def test_installer_help_omits_internal_codex_hook_without_importing_adapter(
    monkeypatch,
    capsys,
):
    from symbiosis_brain import install_cli

    adapter_module = "symbiosis_brain.adapters.codex.adapter"
    monkeypatch.delitem(sys.modules, adapter_module, raising=False)
    monkeypatch.setattr(sys, "argv", ["symbiosis-brain", "--help"])

    with pytest.raises(SystemExit) as exit_info:
        install_cli.main()

    assert exit_info.value.code == 0
    help_text = capsys.readouterr().out
    assert re.search(r"\bcodex-hook\b", help_text) is None
    assert "codex-hooks" in help_text
    assert adapter_module not in sys.modules
