"""Executor-local model selection for Claude PreToolUse signatures."""
from __future__ import annotations

import json
import re
from collections.abc import Mapping
from pathlib import Path

from symbiosis_brain.provenance import is_brain_write_tool

_TAIL_BYTES = 1024 * 1024
_AGENT_ID = re.compile(r"[A-Za-z0-9_-]+")


def model_from_transcript(payload: Mapping[str, object]) -> str | None:
    """Read one bounded actor-local JSONL tail, without retries or parent fallback."""
    raw_path = payload.get("transcript_path")
    if not isinstance(raw_path, str) or not raw_path:
        return None
    path = Path(raw_path)
    if "agent_id" in payload:
        agent_id = payload["agent_id"]
        if not isinstance(agent_id, str) or not _AGENT_ID.fullmatch(agent_id):
            return None
        if path.suffix != ".jsonl":
            return None
        path = path.with_suffix("") / "subagents" / f"agent-{agent_id}.jsonl"
    try:
        with path.open("rb") as stream:
            stream.seek(0, 2)
            start = max(0, stream.tell() - _TAIL_BYTES)
            previous = b"\n"
            if start:
                stream.seek(start - 1)
                previous = stream.read(1)
            stream.seek(start)
            tail = stream.read(_TAIL_BYTES)
    except (OSError, ValueError):
        return None
    if start and previous != b"\n":
        # The tail starts inside a JSON record or UTF-8 code point.
        _, _, tail = tail.partition(b"\n")
    lines = tail.split(b"\n")[:-1]  # unterminated last record is not published yet
    tool_use_id = payload.get("tool_use_id")
    latest = None
    exact = None
    for line in lines:
        try:
            record = json.loads(line)
        except (ValueError, UnicodeDecodeError):
            continue
        if not isinstance(record, dict) or record.get("type") != "assistant":
            continue
        message = record.get("message")
        if not isinstance(message, dict):
            continue
        model = message.get("model")
        if not isinstance(model, str) or not model.strip() or "\n" in model or "\r" in model:
            continue
        latest = model.strip()
        content = message.get("content")
        if isinstance(tool_use_id, str) and tool_use_id and isinstance(content, list):
            if any(isinstance(block, dict) and block.get("type") == "tool_use"
                   and block.get("id") == tool_use_id for block in content):
                exact = latest
    return exact or latest


def pre_tool_update(payload: Mapping[str, object]) -> dict[str, object] | None:
    """Copy the complete tool input, changing only the executor's model when known."""
    tool_name = payload.get("tool_name")
    if not isinstance(tool_name, str) or not is_brain_write_tool(tool_name):
        return None
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, Mapping):
        return None
    model = model_from_transcript(payload)
    if model is None:
        return None
    return {**tool_input, "model": model}
