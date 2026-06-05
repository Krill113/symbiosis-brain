"""Shared helpers for Symbiosis Brain installers.

Used by:
- scripts/install_statusline.py  — pre-existing statusline installer
- src/symbiosis_brain/install_cli.py — full setup CLI
"""
from __future__ import annotations

import copy
import json
import shutil
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path


def backup_file(path: Path) -> Path | None:
    """Copy `path` to `path.parent/<name>.bak.<timestamp>`. Returns backup path or None if source missing."""
    if not path.exists():
        return None
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = path.parent / f"{path.name}.bak.{ts}"
    shutil.copyfile(path, backup)
    return backup


def deep_merge(base: dict, overlay: dict, list_extend_keys: Iterable[str] | None = None) -> dict:
    """Recursively merge `overlay` into a copy of `base`.

    - Nested dicts merge.
    - For keys in `list_extend_keys`: lists are concatenated and de-duplicated (preserves order from base then new).
    - For other lists: overlay replaces base (we don't accidentally append into someone else's list).
    """
    extend_keys = set(list_extend_keys or ())
    result: dict = copy.deepcopy(base)
    for k, v in overlay.items():
        if isinstance(v, dict) and isinstance(result.get(k), dict):
            result[k] = deep_merge(result[k], v, list_extend_keys=extend_keys)
        elif isinstance(v, list) and isinstance(result.get(k), list) and k in extend_keys:
            seen = list(result[k])
            for item in v:
                if item not in seen:
                    seen.append(item)
            result[k] = seen
        else:
            result[k] = v
    return result


def atomic_write_json(path: Path, data: dict) -> None:
    """Write `data` as JSON to `path`, atomically (write to .tmp, rename)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def has_marker(path: Path, marker: str) -> bool:
    """Return True if `path` exists and contains `marker` substring."""
    if not path.exists():
        return False
    return marker in path.read_text(encoding="utf-8")


VAULT_FOLDERS = ("projects", "wiki", "decisions", "patterns",
                 "mistakes", "feedback", "research", "reference")


def _templates_dir() -> Path:
    """Return path to templates/ shipped with the package."""
    # Templates ship alongside the source; for an installed package we may need
    # importlib.resources, but for our hatchling layout the relative path works.
    return Path(__file__).parent.parent.parent / "templates"


def scaffold_vault(vault_path: Path) -> None:
    """Create vault structure idempotently. Existing files preserved."""
    vault_path.mkdir(parents=True, exist_ok=True)
    for folder in VAULT_FOLDERS:
        (vault_path / folder).mkdir(exist_ok=True)

    readme = vault_path / "README.md"
    if not readme.exists():
        readme.write_text(
            (_templates_dir() / "vault-readme.md").read_text(encoding="utf-8"),
            encoding="utf-8",
        )

    taxonomy = vault_path / "reference" / "scope-taxonomy.md"
    if not taxonomy.exists():
        taxonomy.write_text(
            (_templates_dir() / "scope-taxonomy-minimal.md").read_text(encoding="utf-8"),
            encoding="utf-8",
        )

    memory = vault_path / "MEMORY.md"
    if not memory.exists():
        memory.write_text("# MEMORY\n\nFallback for when MCP is unavailable.\n",
                          encoding="utf-8")

    # Vault .gitignore — keep machine-local + private files out of git.
    # Idempotent: only appends lines that are not already present.
    gitignore = vault_path / ".gitignore"
    needed = [".index/", "tool-routing.local.json"]
    if not gitignore.exists():
        gitignore.write_text(
            "# Symbiosis Brain — keep machine-local + private files out of git.\n"
            + "\n".join(needed) + "\n",
            encoding="utf-8",
        )
    else:
        existing = gitignore.read_text(encoding="utf-8")
        present = set(existing.splitlines())
        missing = [p for p in needed if p not in present]
        if missing:
            sep = "" if existing.endswith("\n") else "\n"
            gitignore.write_text(existing + sep + "\n".join(missing) + "\n", encoding="utf-8")


def _hooks_block(hook_dir: str) -> dict:
    """Return hooks block structure for settings.json.

    Bash is the single source of truth (matches the live ~/.claude install).
    Six events: SessionStart (startup+compact), Stop, PreCompact,
    UserPromptSubmit, PreToolUse (proactive recall), SessionEnd (vault sync).
    PreToolUse runs the recall hook from the tools repo via $SYMBIOSIS_BRAIN_TOOLS.
    """
    return {
        "SessionStart": [
            {"matcher": "startup",
             "hooks": [{"type": "command",
                        "command": f"bash {hook_dir}/brain-session-start.sh",
                        "timeout": 5}]},
            {"matcher": "compact",
             "hooks": [{"type": "command",
                        "command": f"bash {hook_dir}/brain-session-start.sh",
                        "timeout": 5}]},
        ],
        "Stop": [
            {"hooks": [{"type": "command",
                        "command": f"bash {hook_dir}/brain-save-trigger.sh stop"}]},
        ],
        "PreCompact": [
            {"hooks": [{"type": "command",
                        "command": f"bash {hook_dir}/brain-save-trigger.sh precompact"}]},
        ],
        "UserPromptSubmit": [
            {"hooks": [{"type": "command",
                        "command": f"bash {hook_dir}/brain-save-trigger.sh prompt-check"}]},
        ],
        "PreToolUse": [
            {"matcher": "Task|Edit|Write|MultiEdit|NotebookEdit|Bash",
             "hooks": [{"type": "command",
                        "command": 'bash "$SYMBIOSIS_BRAIN_TOOLS/hooks/brain-pre-action-trigger.sh"'}]},
        ],
        "SessionEnd": [
            {"hooks": [{"type": "command",
                        "command": f"bash {hook_dir}/brain-sync.sh auto",
                        "timeout": 35}]},
        ],
    }


SB_STATUSLINE_MARKER = "sb-statusline"


# Behavioural env defaults seeded into settings.json (values mirror the hook-code
# fallbacks in brain-save-trigger.sh). NOT included on purpose: RULES_ZONES (leave
# to the hook fallback 30,60,85 — never bake a personal recalibration into the public
# installer), MCP_TIMEOUT and CLAUDE_CODE_DISABLE_ADAPTIVE_THINKING (harness prefs,
# not ours to impose). All seeding is non-clobbering — a pre-existing user value wins.
SB_ENV_DEFAULTS = {
    "SYMBIOSIS_BRAIN_RECALL_ENABLED": "true",
    "SYMBIOSIS_BRAIN_RECALL_TOP_K": "5",
    "SYMBIOSIS_BRAIN_RECALL_SKIP_SHORT_CHARS": "15",
    "SYMBIOSIS_BRAIN_RULES_ENABLED": "true",
    "SYMBIOSIS_BRAIN_RULES_TURN_INTERVAL": "10",
    "SYMBIOSIS_BRAIN_SAVE_THRESHOLDS": "25,35,45",
    "SYMBIOSIS_BRAIN_SAVE_DELTA_GUARD": "10",
}


def merge_settings_json(settings_path: Path,
                        hook_dir: str,
                        statusline_cmd: str,
                        permissions: list[str],
                        vault_path: str | None = None,
                        tools_path: str | None = None) -> None:
    """Idempotent deep-merge of our hooks/statusLine/permissions/env into settings.json.

    Caller (cmd_setup) is the single owner of the pre-task backup.
    - Preserves a non-SB statusLine command in env.SYMBIOSIS_BRAIN_USER_STATUSLINE_CMD.
    - Seeds SB_ENV_DEFAULTS plus VAULT/TOOLS paths, non-clobbering (existing user
      values are never overwritten) — required for PreToolUse recall and SessionEnd
      sync to resolve their paths and knobs.
    - Deduplicates permissions.allow.
    """
    if not settings_path.exists():
        settings_path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(settings_path, {})
    settings = json.loads(settings_path.read_text(encoding="utf-8"))

    # Statusline preservation
    current_status = (settings.get("statusLine") or {}).get("command")
    if current_status and SB_STATUSLINE_MARKER not in current_status:
        env = settings.setdefault("env", {})
        env["SYMBIOSIS_BRAIN_USER_STATUSLINE_CMD"] = current_status

    # Env seeding — non-clobbering: only add keys absent from the live config.
    env_defaults = dict(SB_ENV_DEFAULTS)
    if vault_path:
        env_defaults["SYMBIOSIS_BRAIN_VAULT"] = vault_path
    if tools_path:
        env_defaults["SYMBIOSIS_BRAIN_TOOLS"] = tools_path
    cur_env = settings.get("env") or {}
    seed_env = {k: v for k, v in env_defaults.items() if k not in cur_env}

    overlay = {
        "env": seed_env,
        "hooks": _hooks_block(hook_dir),
        "statusLine": {"type": "command", "command": statusline_cmd, "refreshInterval": 10},
        "permissions": {"allow": list(permissions)},
    }
    merged = deep_merge(settings, overlay, list_extend_keys={"allow"})
    atomic_write_json(settings_path, merged)


CLAUDE_MD_MARKER = "<!-- symbiosis-brain v1: global -->"


def append_claude_md_block(claude_md_path: Path) -> None:
    block_template = (_templates_dir() / "claude-md-block.md").read_text(encoding="utf-8")
    if not claude_md_path.exists():
        claude_md_path.parent.mkdir(parents=True, exist_ok=True)
        claude_md_path.write_text("# Global Rules\n\n", encoding="utf-8")

    if has_marker(claude_md_path, CLAUDE_MD_MARKER):
        return

    backup_file(claude_md_path)
    existing = claude_md_path.read_text(encoding="utf-8")
    sep = "\n" if existing.endswith("\n") else "\n\n"
    claude_md_path.write_text(existing + sep + block_template, encoding="utf-8")
