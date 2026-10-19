"""Codex hook request and response protocol primitives."""

from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping


class HookEvent(str, Enum):
    SESSION_START = "SessionStart"
    USER_PROMPT_SUBMIT = "UserPromptSubmit"
    PRE_TOOL_USE = "PreToolUse"
    POST_TOOL_USE = "PostToolUse"
    PRE_COMPACT = "PreCompact"
    POST_COMPACT = "PostCompact"
    STOP = "Stop"
    SESSION_END = "SessionEnd"


@dataclass(frozen=True)
class HookRequest:
    event: HookEvent
    session_id: str | None
    turn_id: str | None
    cwd: Path
    model: str | None
    permission_mode: str | None
    payload: Mapping[str, Any]


ADDITIONAL_CONTEXT_LIMITS: Mapping[HookEvent, int] = MappingProxyType({
    HookEvent.SESSION_START: 4000,
    HookEvent.USER_PROMPT_SUBMIT: 1500,
    HookEvent.PRE_TOOL_USE: 1500,
})


def _optional_string(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _cwd(value: Any) -> Path:
    if not isinstance(value, str) or not value or "\x00" in value:
        return Path.cwd()
    try:
        return Path(value)
    except (TypeError, ValueError):
        return Path.cwd()


def parse_hook_request(raw: str) -> HookRequest | None:
    """Decode one Codex hook JSON object, failing open on invalid input."""
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(payload, dict):
        return None

    try:
        event = HookEvent(payload.get("hook_event_name"))
    except (TypeError, ValueError):
        return None

    session_id = (
        _optional_string(payload.get("session_id"))
        or _optional_string(payload.get("thread_id"))
    )
    return HookRequest(
        event=event,
        session_id=session_id,
        turn_id=_optional_string(payload.get("turn_id")),
        cwd=_cwd(payload.get("cwd")),
        model=_optional_string(payload.get("model")),
        permission_mode=_optional_string(payload.get("permission_mode")),
        payload=MappingProxyType(dict(payload)),
    )


def _estimate_tokens(value: str) -> int:
    return (len(value) + 2) // 3


def _bounded_context(event: HookEvent, value: str) -> str:
    limit = ADDITIONAL_CONTEXT_LIMITS.get(event)
    if limit is None:
        return value

    if _estimate_tokens(value) < limit:
        return value
    sentences = re.split(r"(?<=[.!?])\s+", value.strip())
    selected: list[str] = []
    for sentence in sentences:
        candidate = " ".join([*selected, sentence]).strip()
        if _estimate_tokens(candidate) >= limit:
            break
        selected.append(sentence)
    if selected:
        return " ".join(selected).rstrip()

    char_limit = max(0, limit * 3 - 4)
    candidate = value[:char_limit].rstrip()
    while candidate and _estimate_tokens(candidate) >= limit:
        candidate = candidate[:-1].rstrip()
    return candidate


def context_response(
    event: HookEvent,
    *,
    additional_context: str | None = None,
    system_message: str | None = None,
) -> dict[str, Any]:
    hook_output: dict[str, Any] = {"hookEventName": event.value}
    if additional_context:
        hook_output["additionalContext"] = _bounded_context(
            event, additional_context
        )

    response: dict[str, Any] = {
        "continue": True,
        "hookSpecificOutput": hook_output,
    }
    if system_message:
        response["systemMessage"] = system_message
    return response


def continue_response(*, system_message: str | None = None) -> dict[str, Any]:
    response: dict[str, Any] = {"continue": True}
    if system_message:
        response["systemMessage"] = system_message
    return response


def continuation_response(reason: str) -> dict[str, Any]:
    return {"decision": "block", "reason": reason}


def emit_response(value: Mapping[str, Any]) -> None:
    sys.stdout.write(json.dumps(dict(value), ensure_ascii=True) + "\n")
