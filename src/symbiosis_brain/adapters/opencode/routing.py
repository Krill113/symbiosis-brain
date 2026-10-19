"""OpenCode-local capability gates over the canonical Brain route catalog."""

from __future__ import annotations

import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

from symbiosis_brain import action_rules, tool_routing
from symbiosis_brain.adapters.opencode.protocol import ContextBlock

_CAP = 2


def _names(capabilities: dict, key: str) -> set[str]:
    value = capabilities.get(key, []) if isinstance(capabilities, dict) else []
    if not isinstance(value, (list, tuple, set)):
        return set()
    return {item.casefold() for item in value if isinstance(item, str)}


def _diagnose(diagnostics: list[str] | None, code: str) -> None:
    if diagnostics is not None and code not in diagnostics:
        diagnostics.append(code)


def _available(token: str, capabilities: dict, scope: str, cwd: Path, diagnostics: list[str] | None = None) -> bool:
    token = token.strip()
    if token.startswith("scope:"):
        expected_scope = token.split(":", 1)[1]
        if not expected_scope or not all(char.isalnum() or char in "._-" for char in expected_scope):
            _diagnose(diagnostics, f"unsupported-when-predicate:{token}")
            return False
        return scope == expected_scope
    if token == "platform:windows":
        platform = capabilities.get("platform") if isinstance(capabilities, dict) else None
        return (str(platform).casefold() in {"windows", "win32", "nt"}) if platform else sys.platform == "win32"
    if token == "catalog-present":
        return (cwd / ".claude" / "docs" / "catalog").is_dir()
    if token.startswith("skill:") and token.endswith("-present"):
        name = token[len("skill:") : -len("-present")].casefold()
        if not name:
            _diagnose(diagnostics, f"unsupported-when-predicate:{token}")
            return False
        # The host capability roster is sourced from package resources, never ~/.claude.
        return name in _names(capabilities, "skills")
    if ":" in token and not token.startswith("mcp:"):
        _diagnose(diagnostics, f"unsupported-when-predicate:{token}")
        return False
    mcp = _names(capabilities, "mcp")
    tools = _names(capabilities, "tools")
    if token.startswith("mcp:") and token.endswith("-present"):
        name = token[4:-8].casefold()
        if not name:
            _diagnose(diagnostics, f"unsupported-when-predicate:{token}")
            return False
        return name in mcp or any(tool.startswith(f"mcp__{name}__") for tool in tools)
    if token.endswith("-present"):
        name = token[:-8].casefold()
        if not name or ":" in name or not all(char.isalnum() or char in "._-" for char in name):
            _diagnose(diagnostics, f"unsupported-when-predicate:{token}")
            return False
        return name in mcp or name in tools or any(tool.startswith(f"mcp__{name}__") for tool in tools)
    # Unknown predicates must not accidentally become enabled.
    _diagnose(diagnostics, f"unsupported-when-predicate:{token}")
    return False


def _when_enabled(when: str | None, capabilities: dict, scope: str, cwd: Path, diagnostics: list[str] | None = None) -> bool:
    if not when:
        return True
    return all(_available(part, capabilities, scope, cwd, diagnostics) for part in when.split("&"))


def _route_enabled(route: Any, capabilities: dict, scope: str, cwd: Path, diagnostics: list[str] | None = None) -> bool:
    if not _when_enabled(route.when, capabilities, scope, cwd, diagnostics):
        return False
    expected = route.expected_tool
    if expected:
        return expected.casefold() in _names(capabilities, "tools")
    return True


def _routes(vault: Path):
    return tool_routing.load_routes(vault=Path(vault))


def match_opencode_routes(
    query: str, capabilities: dict, scope: str, cwd: Path, vault: Path, *, diagnostics: list[str] | None = None,
) -> list[ContextBlock]:
    """Match canonical prompt triggers after evaluating adapter-local gates."""
    if not query or not scope:
        return []
    cwd = Path(cwd)
    candidates = [
        replace(route, when=None)
        for route in _routes(Path(vault))
        if route.cls in {"augment", "supersede"}
        and _route_enabled(route, capabilities, scope, cwd, diagnostics)
    ]
    matched = tool_routing.match_routes(query, candidates, roster=None, scope=scope, vault=Path(vault), cap=_CAP)
    revision = capabilities.get("revision", 0) if isinstance(capabilities, dict) else 0
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
        revision = 0
    return [
        ContextBlock(f"route:{route.id}", route.hint, route.priority, "route", revision)
        for route in matched if route.hint.strip()
    ]


def load_action_descriptors(vault: Path, capabilities: dict, scope: str, *, diagnostics: list[str] | None = None) -> list[dict]:
    """Read compiled action rows and retain only current, capability-enabled rules."""
    if not scope:
        return []
    vault = Path(vault)
    tsv = vault / ".index" / action_rules.TSV_BASENAME
    try:
        if tsv.stat().st_size > 1024 * 1024:
            _diagnose(diagnostics, "action-rules-artifact-oversize")
            return []
        rows = [line.split("\t") for line in tsv.read_text(encoding="utf-8").splitlines()[:1000]]
    except (OSError, UnicodeError):
        _diagnose(diagnostics, "action-rules-artifact-unavailable")
        return []
    current = {route.id: route for route in _routes(vault) if route.cls == "action"}
    revision = capabilities.get("revision", 0) if isinstance(capabilities, dict) else 0
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
        revision = 0
    out = []
    seen = set()
    for row in rows:
        if len(row) != 4:
            continue
        shell, route_id, pattern, hint_escaped = row
        route = current.get(route_id)
        if route is None or shell not in {"bash", "powershell"}:
            continue
        request_cwd = capabilities.get("cwd", "") if isinstance(capabilities, dict) else ""
        if route.when and "catalog-present" in {part.strip() for part in route.when.split("&")} and not request_cwd:
            continue
        if not _route_enabled(route, capabilities, scope, Path(request_cwd), diagnostics):
            continue
        triggers = route.command_triggers.get(shell, [])
        if not any(isinstance(item, dict) and item.get("re") == pattern for item in triggers):
            continue  # stale compiled artifacts never outlive the current catalog
        try:
            import json
            hint = json.loads('"' + hint_escaped + '"')
        except (ValueError, TypeError):
            continue
        expected_hint = " ".join(route.hint.split("\n")).replace("\r", " ")
        if hint != expected_hint:
            continue
        key = (shell, route_id, pattern)
        if key in seen:
            continue
        seen.add(key)
        out.append({
            "id": route_id, "shell": shell, "pattern": pattern, "hint": hint,
            "priority": route.priority, "capabilityRevision": revision,
        })
    return sorted(out, key=lambda item: (-item["priority"], item["id"], item["shell"]))
