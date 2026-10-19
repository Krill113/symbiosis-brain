"""Validated, provider-neutral JSON-lines protocol for the OpenCode bridge."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, NoReturn

MAX_FRAME_BYTES = 64 * 1024
MAX_SAFE_INTEGER = 2**53 - 1
OPERATIONS = {"bootstrap", "recall", "routes", "resources", "warmup"}
ORIGINS = {"user", "task", "action", "resume"}
BLOCK_CATEGORIES = {"bootstrap", "memory", "route", "action_rule"}


class ProtocolError(ValueError):
    """Safe protocol failure whose message is a stable reason code."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class _OversizedIntegerToken:
    """Preserve rejection without converting huge JSON integer lexemes."""


def _parse_json_integer(token: str) -> int | _OversizedIntegerToken:
    digits = token[1:] if token.startswith("-") else token
    # Any integer with more than 16 decimal digits cannot be a JavaScript-safe
    # integer. Avoid Python's process-global digit limit and let field validation
    # reject it with the field's stable protocol reason.
    if len(digits) > 16:
        return _OversizedIntegerToken()
    return int(token)


def _decode_json(text: str) -> Any:
    try:
        return json.loads(text, parse_int=_parse_json_integer)
    except (json.JSONDecodeError, ValueError, TypeError):
        raise ProtocolError("malformed-json") from None


def _string(value: Any, field_name: str, *, optional: bool = False) -> str | None:
    if optional and value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ProtocolError(f"invalid-{field_name}")
    return value


def _integer(value: Any, field_name: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum or value > MAX_SAFE_INTEGER:
        raise ProtocolError(f"invalid-{field_name}")
    return value


def _inside(cwd: str, boundary: str) -> bool:
    try:
        Path(cwd).resolve(strict=False).relative_to(Path(boundary).resolve(strict=False))
        return True
    except (OSError, ValueError):
        return False


@dataclass(frozen=True)
class Action:
    tool: str
    shell: str | None = None
    command: str | None = None

    def to_wire(self) -> dict[str, str]:
        return {k: v for k, v in (("tool", self.tool), ("shell", self.shell), ("command", self.command)) if v is not None}


@dataclass(frozen=True)
class ContextRequest:
    request_id: str
    session_id: str
    generation: int
    operation: str
    cwd: str
    origin: str
    capability_revision: int
    tools: tuple[str, ...] = ()
    mcp: tuple[str, ...] = ()
    skills: tuple[str, ...] = ()
    boundary: str | None = None
    scope: str | None = None
    query: str | None = None
    action: Action | None = None

    def to_wire(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "version": 1, "requestId": self.request_id, "sessionId": self.session_id,
            "generation": self.generation, "operation": self.operation, "cwd": self.cwd,
            "origin": self.origin, "capabilityRevision": self.capability_revision,
            "capabilities": {"tools": list(self.tools), "mcp": list(self.mcp), "skills": list(self.skills)},
        }
        for key, value in (("boundary", self.boundary), ("scope", self.scope), ("query", self.query)):
            if value is not None:
                out[key] = value
        if self.action is not None:
            out["action"] = self.action.to_wire()
        return out


@dataclass(frozen=True)
class ContextBlock:
    id: str
    text: str
    priority: int
    category: str
    capability_revision: int | None = None

    def to_wire(self) -> dict[str, Any]:
        out: dict[str, Any] = {"id": self.id, "text": self.text, "priority": self.priority, "category": self.category}
        if self.capability_revision is not None:
            out["capabilityRevision"] = self.capability_revision
        return out


@dataclass(frozen=True)
class BridgeResources:
    python_exe: str
    package_dir: str
    skills_dir: str
    grep_exe: str | None
    action_rules_file: str
    mcp_command: tuple[str, ...]

    def to_wire(self) -> dict[str, Any]:
        return {
            "pythonExe": self.python_exe, "packageDir": self.package_dir,
            "skillsDir": self.skills_dir, "grepExe": self.grep_exe,
            "actionRulesFile": self.action_rules_file, "mcpCommand": list(self.mcp_command),
        }


@dataclass(frozen=True)
class ContextResult:
    request_id: str
    session_id: str
    generation: int
    capability_revision: int
    status: str
    scope: str | None
    blocks: list[ContextBlock] = field(default_factory=list)
    diagnostics: list[str] = field(default_factory=list)
    resources: BridgeResources | None = None
    action_descriptors: list[dict[str, Any]] | None = None

    def to_wire(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "version": 1, "kind": "result", "requestId": self.request_id,
            "sessionId": self.session_id, "generation": self.generation,
            "capabilityRevision": self.capability_revision, "status": self.status,
            "scope": self.scope, "blocks": [b.to_wire() for b in self.blocks],
            "diagnostics": list(self.diagnostics),
        }
        if self.resources is not None:
            out["resources"] = self.resources.to_wire()
        if self.action_descriptors is not None:
            out["actionDescriptors"] = self.action_descriptors
        return out


@dataclass(frozen=True)
class WorkerHello:
    role: str

    def to_wire(self) -> dict[str, Any]:
        return {"version": 1, "kind": "hello", "ready": True, "role": self.role}


@dataclass(frozen=True)
class ActionDescriptor:
    id: str
    shell: str
    pattern: str
    hint: str
    priority: int
    capability_revision: int

    def to_wire(self) -> dict[str, Any]:
        return {
            "id": self.id, "shell": self.shell, "pattern": self.pattern,
            "hint": self.hint, "priority": self.priority,
            "capabilityRevision": self.capability_revision,
        }


def parse_request(value: Mapping[str, Any], *, expected_session_id: str | None = None) -> ContextRequest:
    if not isinstance(value, Mapping):
        raise ProtocolError("invalid-request")
    if value.get("version") != 1 or isinstance(value.get("version"), bool):
        raise ProtocolError("unsupported-version")
    request_id = _string(value.get("requestId"), "request-id")
    session_id = _string(value.get("sessionId"), "session-id")
    if expected_session_id is not None and session_id != expected_session_id:
        raise ProtocolError("session-mismatch")
    operation = value.get("operation")
    if not isinstance(operation, str) or operation not in OPERATIONS:
        raise ProtocolError("invalid-operation")
    origin = value.get("origin")
    if not isinstance(origin, str) or origin not in ORIGINS:
        raise ProtocolError("invalid-origin")
    cwd = _string(value.get("cwd"), "cwd")
    boundary = _string(value["boundary"], "boundary") if "boundary" in value else None
    if boundary is not None and not _inside(cwd, boundary):
        raise ProtocolError("cwd-outside-boundary")
    scope = _string(value["scope"], "scope") if "scope" in value else None
    if boundary is None and scope is not None:
        raise ProtocolError("scope-without-boundary")
    capabilities = value.get("capabilities")
    if not isinstance(capabilities, Mapping):
        raise ProtocolError("invalid-capabilities")
    lists: dict[str, tuple[str, ...]] = {}
    for key in ("tools", "mcp", "skills"):
        item = capabilities.get(key)
        if not isinstance(item, list) or any(not isinstance(v, str) for v in item):
            raise ProtocolError(f"invalid-capability-{key}")
        lists[key] = tuple(item)
    action_value = value.get("action")
    action = None
    if "action" in value:
        if not isinstance(action_value, Mapping):
            raise ProtocolError("invalid-action")
        tool = _string(action_value.get("tool"), "action-tool")
        shell = action_value.get("shell")
        if "shell" in action_value and (not isinstance(shell, str) or shell not in {"bash", "powershell"}):
            raise ProtocolError("invalid-action-shell")
        command = action_value.get("command")
        if "command" in action_value and not isinstance(command, str):
            raise ProtocolError("invalid-action-command")
        action = Action(tool, shell, command)
    return ContextRequest(
        request_id=request_id, session_id=session_id,
        generation=_integer(value.get("generation"), "generation"),
        operation=operation, cwd=cwd, boundary=boundary,
        scope=scope,
        query=_string(value["query"], "query") if "query" in value else None, origin=origin,
        capability_revision=_integer(value.get("capabilityRevision"), "capability-revision"),
        tools=lists["tools"], mcp=lists["mcp"], skills=lists["skills"], action=action,
    )


def encode_frame(value: Mapping[str, Any] | ContextRequest | ContextResult | WorkerHello) -> bytes:
    payload = value.to_wire() if hasattr(value, "to_wire") else value
    try:
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"
    except (TypeError, ValueError):
        raise ProtocolError("invalid-frame") from None
    if len(encoded) > MAX_FRAME_BYTES:
        raise ProtocolError("frame-too-large")
    return encoded


def parse_request_frame(raw: bytes | str, *, expected_session_id: str | None = None) -> ContextRequest:
    try:
        size = len(raw.encode("utf-8")) if isinstance(raw, str) else len(raw)
    except (UnicodeError, AttributeError):
        raise ProtocolError("malformed-frame") from None
    if size > MAX_FRAME_BYTES:
        raise ProtocolError("frame-too-large")
    try:
        text = raw.decode("utf-8") if isinstance(raw, bytes) else raw
    except (UnicodeError, TypeError):
        raise ProtocolError("malformed-json") from None
    value = _decode_json(text)
    return parse_request(value, expected_session_id=expected_session_id)


def parse_result(raw: bytes | str, *, expected_session_id: str | None = None, expected_operation: str | None = None) -> ContextResult:
    try:
        size = len(raw.encode("utf-8")) if isinstance(raw, str) else len(raw)
    except (UnicodeError, AttributeError):
        raise ProtocolError("malformed-frame") from None
    if size > MAX_FRAME_BYTES:
        raise ProtocolError("frame-too-large")
    try:
        text = raw.decode("utf-8") if isinstance(raw, bytes) else raw
    except (UnicodeError, TypeError):
        raise ProtocolError("malformed-json") from None
    value = _decode_json(text)
    if not isinstance(value, Mapping) or value.get("version") != 1 or isinstance(value.get("version"), bool) or value.get("kind") != "result":
        raise ProtocolError("invalid-result")
    request_id = _string(value.get("requestId"), "request-id")
    session_id = _string(value.get("sessionId"), "session-id")
    if expected_session_id is not None and session_id != expected_session_id:
        raise ProtocolError("session-mismatch")
    generation = _integer(value.get("generation"), "generation")
    revision = _integer(value.get("capabilityRevision"), "capability-revision")
    status, scope = value.get("status"), value.get("scope")
    if "scope" not in value or not isinstance(status, str) or status not in {"ok", "degraded"} or (scope is not None and not isinstance(scope, str)):
        raise ProtocolError("invalid-result")
    raw_blocks, diagnostics = value.get("blocks"), value.get("diagnostics")
    if not isinstance(raw_blocks, list) or not isinstance(diagnostics, list) or any(not isinstance(d, str) for d in diagnostics):
        raise ProtocolError("invalid-result")
    blocks: list[ContextBlock] = []
    for block in raw_blocks:
        if not isinstance(block, Mapping) or not isinstance(block.get("category"), str) or block["category"] not in BLOCK_CATEGORIES or not isinstance(block.get("text"), str):
            raise ProtocolError("invalid-block")
        blocks.append(ContextBlock(
            _string(block.get("id"), "block-id"), block["text"],
            _integer(block.get("priority"), "block-priority"), block["category"],
            _integer(block["capabilityRevision"], "capability-revision") if "capabilityRevision" in block else None,
        ))
    resources = value.get("resources")
    if expected_operation == "resources" and status == "ok" and "resources" not in value:
        raise ProtocolError("missing-resources")
    if "resources" in value and not isinstance(resources, Mapping):
        raise ProtocolError("invalid-resources")
    if resources is not None:
        if "grepExe" not in resources:
            raise ProtocolError("invalid-resources")
        try:
            resources = BridgeResources(
                _string(resources.get("pythonExe"), "python-exe"),
                _string(resources.get("packageDir"), "package-dir"),
                _string(resources.get("skillsDir"), "skills-dir"),
                _string(resources.get("grepExe"), "grep-exe", optional=True),
                _string(resources.get("actionRulesFile"), "action-rules-file"),
            tuple(resources["mcpCommand"]) if isinstance(resources.get("mcpCommand"), list) and all(isinstance(v, str) for v in resources["mcpCommand"]) else _invalid_resources(),
            )
        except (TypeError, KeyError):
            raise ProtocolError("invalid-resources") from None
        if not isinstance(value["resources"].get("mcpCommand"), list):
            raise ProtocolError("invalid-resources")
    descriptors = value.get("actionDescriptors")
    parsed_descriptors: list[dict[str, Any]] | None = None
    if "actionDescriptors" in value:
        if not isinstance(descriptors, list):
            raise ProtocolError("invalid-action-descriptors")
        parsed_descriptors = [_parse_action_descriptor(item).to_wire() for item in descriptors]
    return ContextResult(request_id, session_id, generation, revision, status, scope, blocks, diagnostics, resources, parsed_descriptors)


def _invalid_resources() -> NoReturn:
    raise ProtocolError("invalid-resources")


def _parse_action_descriptor(value: Any) -> ActionDescriptor:
    if not isinstance(value, Mapping):
        raise ProtocolError("invalid-action-descriptor")
    shell = value.get("shell")
    if not isinstance(shell, str) or shell not in {"bash", "powershell"}:
        raise ProtocolError("invalid-action-descriptor")
    try:
        return ActionDescriptor(
            id=_string(value.get("id"), "action-descriptor-id"),
            shell=shell,
            pattern=_string(value.get("pattern"), "action-descriptor-pattern"),
            hint=_string(value.get("hint"), "action-descriptor-hint"),
            priority=_integer(value.get("priority"), "action-descriptor-priority"),
            capability_revision=_integer(value.get("capabilityRevision"), "action-descriptor-capability-revision"),
        )
    except ProtocolError:
        raise ProtocolError("invalid-action-descriptor") from None


def parse_worker_frame(raw: bytes | str) -> WorkerHello | ContextResult:
    try:
        size = len(raw.encode("utf-8")) if isinstance(raw, str) else len(raw)
    except (UnicodeError, AttributeError):
        raise ProtocolError("malformed-frame") from None
    if size > MAX_FRAME_BYTES:
        raise ProtocolError("frame-too-large")
    try:
        text = raw.decode("utf-8") if isinstance(raw, bytes) else raw
    except (UnicodeError, TypeError):
        raise ProtocolError("malformed-json") from None
    value = _decode_json(text)
    if not isinstance(value, Mapping) or value.get("version") != 1 or isinstance(value.get("version"), bool):
        raise ProtocolError("invalid-worker-frame")
    if value.get("kind") == "hello":
        if value.get("ready") is not True or not isinstance(value.get("role"), str) or value["role"] not in {"control", "retrieval"}:
            raise ProtocolError("invalid-hello")
        return WorkerHello(value["role"])
    if value.get("kind") == "result":
        return parse_result(raw)
    raise ProtocolError("invalid-worker-frame")
