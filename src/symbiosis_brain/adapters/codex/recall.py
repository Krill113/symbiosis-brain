"""Codex-only recall, routing gates, and privacy-preserving dedup wiring."""
from __future__ import annotations

import json
import os
import subprocess
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, Sequence

from symbiosis_brain import tool_routing
from symbiosis_brain.adapters.codex.config import resolve_codex_home
from symbiosis_brain.adapters.codex.secure_io import secure_read_text
from symbiosis_brain.adapters.codex.session_state import (
    session_key,
    session_lock,
    state_path,
)
from symbiosis_brain.adapters.codex.tool_normalizer import normalize_tool_call
from symbiosis_brain.pre_action_config import PreActionConfig
from symbiosis_brain.pre_action_recall import format_recall_block, run_recall
from symbiosis_brain.recall_dedup import SeenStore
from symbiosis_brain.search import (
    FTS_MODE_ALL_THEN_ANY,
    SearchEngine,
)
from symbiosis_brain.storage import Storage
from symbiosis_brain.sync import VaultSync

PROMPT_SEEN_PREFIX = "brain-codex-prompt-seen-"
ACTION_SEEN_PREFIX = "brain-codex-action-seen-"
ROUTE_SEEN_PREFIX = "brain-route-seen-"
_DISCIPLINE_BRAIN = "Before broad code search, check brain_search first."
_DISCIPLINE_TOOLS = (
    "Use available subagents for large multi-file exploration; use "
    "symbol/dependency tools before blind edits."
)
@dataclass
class RecallRuntime:
    storage: Storage
    engine: SearchEngine


@dataclass(frozen=True)
class CodexRoutingEnvironment:
    effective_mcp_names: frozenset[str]
    effective_skill_names: frozenset[str]
    catalog_available: bool | None


@contextmanager
def open_recall_runtime(vault: Path) -> Iterator[RecallRuntime]:
    """Open, incrementally sync, and close the existing recall stack."""

    vault_path = Path(vault)
    storage = Storage(vault_path / ".index" / "brain.db")
    try:
        sync_result = VaultSync(vault_path, storage).sync_all()
        engine = SearchEngine(storage)
        for path in sync_result.removed:
            engine.delete_vec(path)
        for old_path, new_path in sync_result.moved:
            engine.move_vec(old_path, new_path)
        yield RecallRuntime(storage=storage, engine=engine)
    finally:
        storage._conn.close()


def _shape_hits(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "path": row.get("path", ""),
            "title": row.get("title", ""),
            "scope": row.get("scope", ""),
            "gist": row.get("gist", ""),
        }
        for row in rows
    ]


def _memory_recall(
    query: str,
    *,
    scope: str | None,
    allow_global: bool,
    memory_authorized: bool,
    config: PreActionConfig,
    runtime: RecallRuntime,
    session_id: str,
    prefix: str,
    ttl_seconds: int,
) -> list[dict[str, Any]]:
    # Legacy flags remain in the call surface while adapter.py is migrated, but
    # they no longer define a Codex-only authorization policy.
    del allow_global, memory_authorized
    if not query:
        return []
    # ``SearchEngine(scope=None)`` intentionally means all scopes for explicit
    # manual searches. Automatic hook recall must not turn a failed scope
    # resolution into cross-project injection, so unresolved auto context
    # narrows to canonical global memory.
    effective_scope = scope or "global"

    def invoke(seen: SeenStore | None) -> list[dict[str, Any]]:
        return run_recall(
            query=query,
            scope=effective_scope,
            config=config,
            engine=runtime.engine,
            seen=seen,
            fts_mode=FTS_MODE_ALL_THEN_ANY,
            log_ctx=None,
        )

    try:
        if not config.recall_dedup_enabled or not session_id:
            return invoke(None)
        with session_lock(session_id):
            seen = SeenStore(
                session_key(session_id)[:32],
                ttl_seconds=ttl_seconds,
                base_dir=state_path(session_id).parent,
                prefix=prefix,
            )
            return invoke(seen)
    except Exception:
        return []


def _codex_gate(
    token: str,
    *,
    scope: str | None,
    environment: CodexRoutingEnvironment | None,
) -> bool:
    token = token.strip()
    if token == "platform:windows":
        return os.name == "nt" or "win" in os.environ.get("OSTYPE", "").casefold()
    if token.startswith("scope:"):
        return scope == token.split(":", 1)[1]
    if token == "catalog-present":
        return environment is not None and environment.catalog_available is True
    if token.startswith("skill:") and token.endswith("-present"):
        if environment is None:
            return False
        name = token[len("skill:") : -len("-present")]
        return name in environment.effective_skill_names
    if token.endswith("-present"):
        if environment is None:
            return False
        name = token[: -len("-present")].split(":", 1)[0].casefold()
        return any(
            name in item.casefold() for item in environment.effective_mcp_names
        )
    return False


def _codex_when_ok(
    when: str | None,
    *,
    scope: str | None,
    environment: CodexRoutingEnvironment | None,
) -> bool:
    if not when:
        return True
    tokens = when.split("&")
    return bool(tokens) and all(
        _codex_gate(token, scope=scope, environment=environment)
        for token in tokens
    )


def match_codex_routes(
    prompt: str,
    routes: Sequence[tool_routing.Route],
    *,
    scope: str | None,
    environment: CodexRoutingEnvironment | None,
    cap: int,
    session_id: str = "",
    seen_ttl_seconds: int = 86400,
) -> list[tool_routing.Route]:
    """Apply Codex gates first, then reuse the unchanged shared matcher."""

    eligible = [
        replace(route, when=None)
        for route in routes
        if _codex_when_ok(route.when, scope=scope, environment=environment)
    ]
    try:
        matched = tool_routing.match_routes(
            prompt,
            eligible,
            roster=None,
            scope=scope,
            vault=None,
            cap=cap,
        )
        if not matched or not session_id:
            return matched
        with session_lock(session_id):
            seen = SeenStore(
                session_key(session_id),
                ttl_seconds=seen_ttl_seconds,
                base_dir=state_path(session_id).parent,
                prefix=ROUTE_SEEN_PREFIX,
            )
            augment = [route for route in matched if route.cls == "augment"]
            supersede = [route for route in matched if route.cls == "supersede"]
            fresh = [route for route in augment if not seen.is_seen(route.id)]
            seen.record(route.id for route in fresh)
            keep = {route.id for route in fresh} | {route.id for route in supersede}
            return [route for route in matched if route.id in keep]
    except Exception:
        return []


def prompt_recall(
    prompt: str,
    *,
    scope: str | None,
    allow_global: bool,
    memory_authorized: bool,
    config: PreActionConfig,
    runtime: RecallRuntime,
    session_id: str = "",
    routes: Sequence[tool_routing.Route] | None = None,
    routing_environment: CodexRoutingEnvironment | None = None,
    vault: Path | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Return prompt memory and route hints without persisting prompt text."""

    hits = _memory_recall(
        prompt,
        scope=scope,
        allow_global=allow_global,
        memory_authorized=memory_authorized,
        config=config,
        runtime=runtime,
        session_id=session_id,
        prefix=PROMPT_SEEN_PREFIX,
        ttl_seconds=config.prompt_recall_dedup_ttl_seconds,
    )
    matched: list[tool_routing.Route] = []
    if config.routing_enabled:
        candidates = (
            list(routes)
            if routes is not None
            else tool_routing.load_routes(vault=vault)
        )
        matched = match_codex_routes(
            prompt,
            candidates,
            scope=scope,
            environment=routing_environment,
            cap=config.routing_cap,
            session_id=session_id,
            seen_ttl_seconds=config.routing_seen_ttl_seconds,
        )
    return _shape_hits(hits), tool_routing.route_hints(matched)


def action_recall(
    query: str,
    *,
    scope: str | None,
    allow_global: bool,
    memory_authorized: bool,
    config: PreActionConfig,
    runtime: RecallRuntime,
    session_id: str = "",
) -> str:
    """Return an advisory recall block with no query telemetry."""

    safe_query = normalize_tool_call(
        "Bash",
        {"command": query},
        max_chars=config.query_max_chars,
    ) or ""
    hits = _memory_recall(
        safe_query,
        scope=scope,
        allow_global=allow_global,
        memory_authorized=memory_authorized,
        config=config,
        runtime=runtime,
        session_id=session_id,
        prefix=ACTION_SEEN_PREFIX,
        ttl_seconds=config.recall_dedup_ttl_seconds,
    )
    return format_recall_block(safe_query, hits)


def codex_discipline_hint(*, turn: int, interval: int, routes_fired: bool) -> str:
    if type(turn) is not int or type(interval) is not int or turn < 1 or interval < 1:
        return ""
    if turn != 1 and turn % interval != 0:
        return ""
    if routes_fired:
        return _DISCIPLINE_BRAIN
    return f"{_DISCIPLINE_BRAIN} {_DISCIPLINE_TOOLS}"


def _json_command(
    runner: Callable[..., subprocess.CompletedProcess[str]],
    args: list[str],
    *,
    cwd: Path,
    codex_home: Path,
) -> object | None:
    env = os.environ.copy()
    env["CODEX_HOME"] = str(codex_home)
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        result = runner(
            args,
            cwd=str(cwd),
            env=env,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="strict",
            timeout=5,
            creationflags=creationflags,
        )
        if result.returncode != 0 or result.stderr.strip():
            return None
        return json.loads(result.stdout)
    except (OSError, subprocess.SubprocessError, UnicodeError, json.JSONDecodeError):
        return None


def _items(payload: object, key: str) -> list[object] | None:
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict) and isinstance(payload.get(key), list):
        return payload[key]
    return None


def _project_override_present(cwd: Path) -> bool:
    current = cwd
    while True:
        if (current / ".codex" / "config.toml").exists():
            return True
        if current == current.parent:
            return False
        current = current.parent


def _contained(root: Path, candidate: Path) -> Path | None:
    try:
        physical_root = root.resolve(strict=True)
        physical = candidate.resolve(strict=True)
        physical.relative_to(physical_root)
    except (OSError, RuntimeError, ValueError):
        return None
    return physical


def _skill_names(skill_root: Path, codex_home: Path) -> set[str] | None:
    contained = _contained(codex_home, skill_root)
    if contained is None:
        return None
    try:
        names = {
            child.name
            for child in contained.iterdir()
            if child.is_dir()
            and not child.is_symlink()
            and (child / "SKILL.md").is_file()
            and _contained(codex_home, child / "SKILL.md") is not None
        }
    except OSError:
        return None
    return names


def _manifest_skills(codex_home: Path, plugin_root: Path) -> set[str] | None:
    contained_root = _contained(codex_home, plugin_root)
    if contained_root is None:
        return None
    try:
        relative_manifest = (
            contained_root / ".codex-plugin" / "plugin.json"
        ).relative_to(codex_home.resolve(strict=True))
        manifest = json.loads(
            secure_read_text(codex_home.resolve(strict=True), relative_manifest)
        )
    except (OSError, RuntimeError, ValueError, TypeError, json.JSONDecodeError):
        return None
    if not isinstance(manifest, dict):
        return None
    raw_skills = manifest.get("skills")
    if raw_skills is None:
        return set()
    if not isinstance(raw_skills, str) or not raw_skills:
        return None
    return _skill_names(contained_root / raw_skills, codex_home)


def resolve_codex_routing_environment(
    *,
    cwd: Path,
    codex_home: Path | None = None,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> CodexRoutingEnvironment | None:
    """Resolve effective Codex MCP/skill data, failing closed on ambiguity."""

    try:
        workspace = Path(cwd).resolve(strict=True)
        active_home = (
            resolve_codex_home()
            if codex_home is None
            else Path(codex_home).resolve(strict=True)
        )
    except (OSError, RuntimeError):
        return None
    if not workspace.is_dir() or not active_home.is_dir():
        return None
    if _project_override_present(workspace):
        return None

    mcp_payload = _json_command(
        runner,
        ["codex", "mcp", "list", "--json"],
        cwd=workspace,
        codex_home=active_home,
    )
    plugin_payload = _json_command(
        runner,
        ["codex", "plugin", "list", "--json"],
        cwd=workspace,
        codex_home=active_home,
    )
    mcp_items = _items(mcp_payload, "servers")
    plugin_items = _items(plugin_payload, "plugins")
    if mcp_items is None or plugin_items is None:
        return None

    mcp_names: set[str] = set()
    for item in mcp_items:
        if not isinstance(item, dict):
            return None
        name = item.get("name") or item.get("id")
        enabled = item.get("enabled", True)
        if not isinstance(name, str) or type(enabled) is not bool:
            return None
        if enabled:
            mcp_names.add(name)

    local_skills = _skill_names(active_home / "skills", active_home)
    if local_skills is None and (active_home / "skills").exists():
        return None
    skill_names = set(local_skills or ())
    for item in plugin_items:
        if not isinstance(item, dict):
            return None
        enabled = item.get("enabled", True)
        if type(enabled) is not bool:
            return None
        if not enabled:
            continue
        raw_path = (
            item.get("install_path")
            or item.get("path")
            or item.get("root")
        )
        if not isinstance(raw_path, str) or not raw_path:
            return None
        plugin_root = Path(raw_path)
        if not plugin_root.is_absolute():
            plugin_root = active_home / plugin_root
        plugin_skills = _manifest_skills(active_home, plugin_root)
        if plugin_skills is None:
            return None
        skill_names.update(plugin_skills)

    catalog_available: bool | None = None
    if isinstance(plugin_payload, dict) and "catalog_available" in plugin_payload:
        value = plugin_payload["catalog_available"]
        if type(value) is not bool:
            return None
        catalog_available = value
    return CodexRoutingEnvironment(
        effective_mcp_names=frozenset(mcp_names),
        effective_skill_names=frozenset(skill_names),
        catalog_available=catalog_available,
    )
