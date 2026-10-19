"""Codex-specific configuration with privilege-reducing environment overrides."""
from __future__ import annotations

import copy
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Mapping

from symbiosis_brain import pre_action_config
from symbiosis_brain.pre_action_config import PreActionConfig

DEFAULT_CODEX_CONFIG = {
    "bootstrap_visibility": "one-line",
    "handoff_stale_days": 30,
    "save_reminder_turns": 10,
    "status_line_policy": "recommend-only",
    "sync_mode": "manual",
    "sync_bash_path": None,
}

ENV_MAP = {
    "SYMBIOSIS_BRAIN_CODEX_VISIBILITY": "bootstrap_visibility",
}

_RECALL_FIELDS = (
    "excluded_note_types",
    "hit_limit",
    "query_max_chars",
    "recall_dedup_enabled",
    "recall_dedup_ttl_seconds",
    "prompt_recall_dedup_ttl_seconds",
    "routing_enabled",
    "routing_mode",
    "routing_cap",
    "routing_seen_ttl_seconds",
)
_CODEX_KEYS = frozenset(DEFAULT_CODEX_CONFIG)
_DEPRECATED_KEYS = frozenset({"trusted_projects"})


@dataclass(frozen=True)
class TrustedProject:
    root: Path
    allowed_scopes: frozenset[str]
    allow_global: bool = False


@dataclass(frozen=True)
class CodexConfig:
    bootstrap_visibility: Literal["silent", "one-line", "verbose"]
    trusted_projects: tuple[TrustedProject, ...]
    handoff_stale_days: int
    save_reminder_turns: int
    status_line_policy: Literal["recommend-only", "disabled"]
    sync_mode: Literal["manual", "auto"]
    sync_bash_path: Path | None
    recall: PreActionConfig


def resolve_codex_home(environ: Mapping[str, str] | None = None) -> Path:
    """Return the active physical CODEX_HOME, defaulting to ``~/.codex``."""
    env = os.environ if environ is None else environ
    configured = env.get("CODEX_HOME", "").strip()
    path = Path(configured).expanduser() if configured else Path.home() / ".codex"
    return path.resolve(strict=False)


def _shared_recall_config() -> PreActionConfig:
    shared = pre_action_config.load_config(
        Path.home() / ".claude" / "symbiosis-brain-pre-action.json"
    )
    recall = PreActionConfig()
    for name in _RECALL_FIELDS:
        setattr(recall, name, copy.deepcopy(getattr(shared, name)))
    return recall


def _log_invalid(key: str, value: object) -> None:
    pre_action_config._debug_log(
        f"invalid Codex config value for '{key}': {type(value).__name__}"
    )


def _load_file(path: Path) -> dict[str, object]:
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        pre_action_config._debug_log(
            f"Codex config load failed: {type(exc).__name__}"
        )
        return {}
    if not isinstance(value, dict):
        pre_action_config._debug_log(
            f"Codex config root is not an object: {type(value).__name__}"
        )
        return {}
    for key in value:
        if key in _DEPRECATED_KEYS:
            pre_action_config._debug_log(
                f"ignored deprecated Codex config key: {key}"
            )
        elif key not in _CODEX_KEYS:
            pre_action_config._debug_log(f"unknown Codex config key: {key}")
    return value


def _absolute_path(value: object) -> Path | None | Literal[False]:
    if value is None:
        return None
    if not isinstance(value, str) or not value or "\x00" in value:
        return False
    path = Path(value).expanduser()
    if not path.is_absolute():
        return False
    try:
        return path.resolve(strict=False)
    except OSError:
        return False


def load_codex_config(
    codex_home: Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> CodexConfig:
    """Load Codex config without inheriting Claude-specific privileges."""
    env = os.environ if environ is None else environ
    home = resolve_codex_home(env) if codex_home is None else Path(codex_home).resolve(strict=False)
    data = _load_file(home / "symbiosis-brain.json")

    visibility = DEFAULT_CODEX_CONFIG["bootstrap_visibility"]
    value = data.get("bootstrap_visibility", visibility)
    if isinstance(value, str) and value in {"silent", "one-line", "verbose"}:
        visibility = value
    elif "bootstrap_visibility" in data:
        _log_invalid("bootstrap_visibility", value)

    handoff_stale_days = DEFAULT_CODEX_CONFIG["handoff_stale_days"]
    value = data.get("handoff_stale_days", handoff_stale_days)
    if type(value) is int and value >= 0:
        handoff_stale_days = value
    elif "handoff_stale_days" in data:
        _log_invalid("handoff_stale_days", value)

    save_reminder_turns = DEFAULT_CODEX_CONFIG["save_reminder_turns"]
    value = data.get("save_reminder_turns", save_reminder_turns)
    if type(value) is int and value > 0:
        save_reminder_turns = value
    elif "save_reminder_turns" in data:
        _log_invalid("save_reminder_turns", value)

    status_line_policy = DEFAULT_CODEX_CONFIG["status_line_policy"]
    value = data.get("status_line_policy", status_line_policy)
    if isinstance(value, str) and value in {"recommend-only", "disabled"}:
        status_line_policy = value
    elif "status_line_policy" in data:
        _log_invalid("status_line_policy", value)

    sync_mode = DEFAULT_CODEX_CONFIG["sync_mode"]
    value = data.get("sync_mode", sync_mode)
    if isinstance(value, str) and value in {"manual", "auto"}:
        sync_mode = value
    elif "sync_mode" in data:
        _log_invalid("sync_mode", value)

    sync_bash_path: Path | None = None
    if "sync_bash_path" in data:
        parsed_path = _absolute_path(data["sync_bash_path"])
        if parsed_path is False:
            _log_invalid("sync_bash_path", data["sync_bash_path"])
        else:
            sync_bash_path = parsed_path

    requested_visibility = env.get("SYMBIOSIS_BRAIN_CODEX_VISIBILITY", "")
    if requested_visibility == "silent":
        visibility = "silent"
    elif requested_visibility:
        pre_action_config._debug_log(
            "ignored non-reducing SYMBIOSIS_BRAIN_CODEX_VISIBILITY override"
        )

    return CodexConfig(
        bootstrap_visibility=visibility,
        # Kept as a harmless construction/import compatibility field for the
        # copied 0.7.0 adapter. Codex no longer owns a project authorization
        # policy; canonical scope resolution and global fallback apply.
        trusted_projects=(),
        handoff_stale_days=handoff_stale_days,
        save_reminder_turns=save_reminder_turns,
        status_line_policy=status_line_policy,
        sync_mode=sync_mode,
        sync_bash_path=sync_bash_path,
        recall=_shared_recall_config(),
    )
