"""Shared helpers for Symbiosis Brain installers.

Used by:
- scripts/install_statusline.py  — pre-existing statusline installer
- src/symbiosis_brain/install_cli.py — full setup CLI
"""
from __future__ import annotations

import copy
import json
import os
import shutil
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path


def backup_file(path: Path, keep: int = 3) -> Path | None:
    """Copy `path` to `path.parent/<name>.bak.<timestamp>`, keeping the `keep` newest.

    Returns the backup path, or None when the source is missing. `--repair` is the
    supported upgrade path and runs often, and every run used to leave one more .bak
    behind forever (12 of them in a live ~/.claude/hooks by 2026-08). The timestamp
    format sorts lexicographically, so "newest" is just the tail of a sorted glob.
    """
    if not path.exists():
        return None
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = path.parent / f"{path.name}.bak.{ts}"
    shutil.copyfile(path, backup)
    if keep > 0:
        existing = sorted(path.parent.glob(f"{path.name}.bak.*"))
        for stale in existing[:-keep]:
            try:
                stale.unlink()
            except OSError:
                pass  # a backup we cannot delete is not worth failing an install over
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


VAULT_FOLDERS = (# reference/ is NOT a legacy type folder: reference/scope-taxonomy.md is the
                 # taxonomy SSOT at a path hardcoded in taxonomy.py, lint.py, report.py,
                 # storage.py and the doctor health-check. Seeding it is what lets
                 # scaffold_vault write the starter taxonomy below.
                 "reference",
                 # scope-first canon (2026-09 reorg). The pre-reorg top-level type
                 # folders are no longer seeded: a fresh vault starts in the layout
                 # the product uses. Existing vaults keep theirs — nothing deletes
                 # them, and both layouts stay readable (see sync.VAULT_DIRS).
                 "global/wiki", "global/decisions", "global/patterns", "global/mistakes",
                 "global/feedback", "global/research", "global/reference", "global/projects")


def packaged_dir(pkg_file: str, name: str) -> Path:
    """Resolve a resource dir (hooks/skills/templates) shipped two ways:

    - wheel / non-editable install: force-included as a subdir of the installed
      package (`<site-packages>/symbiosis_brain/<name>`) — see
      `[tool.hatch.build.targets.wheel.force-include]` in pyproject.toml.
    - editable/dev checkout (`uv tool install -e .`): lives at the repo root,
      alongside `src/` (`<repo-root>/<name>`) — unchanged from before.

    `pkg_file` is the caller's `__file__` (a module living directly under
    `src/symbiosis_brain/`).
    """
    pkg_dir = Path(pkg_file).parent
    wheel_layout = pkg_dir / name
    if wheel_layout.exists():
        return wheel_layout
    return pkg_dir.parent.parent / name


def _templates_dir() -> Path:
    """Return path to templates/ shipped with the package."""
    return packaged_dir(__file__, "templates")


def scaffold_vault(vault_path: Path) -> None:
    """Create vault structure idempotently. Existing files preserved."""
    vault_path.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        # POSIX: личные заметки не должны быть group/world-readable независимо от
        # umask — chmod явно, это хардит и УЖЕ СУЩЕСТВОВАВШИЙ каталог, а не только
        # свежий mkdir. На Windows не трогаем: там ACL, а не POSIX-биты, и chmod()
        # создал бы ложное ощущение ограниченного доступа.
        try:
            vault_path.chmod(0o700)
        except OSError as e:
            # Монтирования без POSIX-метаданных (CIFS, drvfs, часть FUSE/NFS) или
            # чужой владелец каталога — тогда права не наш вопрос, а установка
            # не должна из-за этого падать. Симметрично chmod хуков в install_cli.
            print(f"WARN: could not set 0700 on {vault_path}: {e}")
    for folder in VAULT_FOLDERS:
        (vault_path / folder).mkdir(parents=True, exist_ok=True)

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

    # Vault .gitattributes — log.md is the append-only journal every multi-machine
    # sync collides on; `merge=union` keeps both sides (duplicates are a lint finding,
    # a lost session log is not recoverable). Every other note keeps the standard
    # merge so a real conflict still stops brain-sync.sh and asks the owner.
    # Idempotent: only appends lines that are not already present.
    gitattributes = vault_path / ".gitattributes"
    needed_attrs = ["log.md merge=union"]
    if not gitattributes.exists():
        gitattributes.write_text(
            "# Symbiosis Brain — merge policy for the vault.\n"
            + "\n".join(needed_attrs) + "\n",
            encoding="utf-8",
        )
    else:
        existing = gitattributes.read_text(encoding="utf-8")
        present = set(existing.splitlines())
        missing = [a for a in needed_attrs if a not in present]
        if missing:
            sep = "" if existing.endswith("\n") else "\n"
            gitattributes.write_text(existing + sep + "\n".join(missing) + "\n", encoding="utf-8")


def _hooks_block(hook_dir: str) -> dict:
    """Return hooks block structure for settings.json.

    Bash is the single source of truth (matches the live ~/.claude install).
    Seven events: SessionStart (startup+resume+clear+compact), Stop, PreCompact,
    UserPromptSubmit, PreToolUse (proactive recall), PostToolUse (save marker),
    SessionEnd (vault sync).
    PreToolUse runs the recall hook from the tools repo via $SYMBIOSIS_BRAIN_TOOLS.

    SessionStart sources are an enum in the harness — startup / resume / clear /
    compact / fork. Only startup+compact used to be registered, so a --resume,
    --continue or /clear session got no scope export, no CRITICAL_FACTS, no roster
    priming and no refreshed session bridge. `fork` stays out on purpose (owner
    decision 2026-08-25).

    PostToolUse writes brain-last-save-pct-<sid> on any memory write: the hook reads
    session_id from its own payload, which is correct even when CLAUDE_SESSION_ID is
    empty (resume/fork) or points at another window.

    SessionEnd is budgeted at 40s: brain-sync.sh runs pull --rebase and push, each
    capped at SYMBIOSIS_BRAIN_SYNC_GIT_TIMEOUT (15s by default), plus add/commit.
    """
    return {
        "SessionStart": [
            {"matcher": m,
             "hooks": [{"type": "command",
                        "command": f"bash {hook_dir}/brain-session-start.sh",
                        "timeout": 5}]}
            for m in ("startup", "resume", "clear", "compact")
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
            {"matcher": "Task|Agent|Edit|Write|MultiEdit|NotebookEdit|Bash|PowerShell",
             "hooks": [{"type": "command",
                        "command": 'bash "$SYMBIOSIS_BRAIN_TOOLS/hooks/brain-pre-action-trigger.sh"'}]},
        ],
        "PostToolUse": [
            {"matcher": ("mcp__symbiosis-brain__brain_write|"
                         "mcp__symbiosis-brain__brain_append|"
                         "mcp__symbiosis-brain__brain_patch"),
             "hooks": [{"type": "command",
                        "command": f"bash {hook_dir}/brain-save-marker.sh",
                        "timeout": 5}]},
        ],
        "SessionEnd": [
            {"hooks": [{"type": "command",
                        "command": f"bash {hook_dir}/brain-sync.sh auto",
                        "timeout": 40}]},
        ],
    }


# Basenames of every script this installer writes into a hooks.<Event> entry. This set
# — not the install directory — is the ownership test used by _merge_hook_event: the
# same hook is referenced by three different prefixes in the wild (~/.claude/hooks/...,
# "$SYMBIOSIS_BRAIN_TOOLS/hooks/...", and an absolute path left by an older install),
# and all three are ours. Kept in sync with _hooks_block by
# test_our_hook_scripts_covers_every_hook_command.
OUR_HOOK_SCRIPTS = frozenset({
    "brain-session-start.sh",
    "brain-save-trigger.sh",
    "brain-save-marker.sh",
    "brain-pre-action-trigger.sh",
    "brain-sync.sh",
})


def _merge_hook_event(base: list, ours: list) -> list:
    """Merge one hooks.<Event> list: drop OUR stale entries, keep everyone else's.

    An entry is ours when any of its commands names one of OUR_HOOK_SCRIPTS, whatever
    path prefix that name carries. Foreign entries keep their original order and come
    first; ours are appended fresh, so N repairs leave exactly one of ours per event.

    Why not "the command contains hook_dir": our PreToolUse entry resolves through
    $SYMBIOSIS_BRAIN_TOOLS and never mentions hook_dir, so that rule would read our own
    entry as foreign, keep it, and append a duplicate on every --repair.

    Why not `list_extend_keys |= {"PostToolUse", ...}`: that path de-duplicates by
    element equality, so a --repair after the hook dir moved, or after any command
    string or timeout changed, would keep the old entry AND add the new one — the
    settings file would grow a new dead hook on every upgrade.
    """
    def is_ours(entry: object) -> bool:
        if not isinstance(entry, dict):
            return False
        return any(
            any(name in ((h or {}).get("command") or "") for name in OUR_HOOK_SCRIPTS)
            for h in entry.get("hooks", []) if isinstance(h, dict)
        )

    return [e for e in (base or []) if not is_ours(e)] + list(ours)


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

    ours_hooks = _hooks_block(hook_dir)
    cur_hooks = settings.get("hooks") or {}
    merged_hooks = dict(cur_hooks)
    for event, entries in ours_hooks.items():
        merged_hooks[event] = _merge_hook_event(cur_hooks.get(event), entries)

    overlay = {
        "env": seed_env,
        "hooks": merged_hooks,
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
