"""Config loader for pre-action recall hook (B1).

Loads `~/.claude/symbiosis-brain-pre-action.json` with fall-back to defaults.
Missing or malformed file → defaults + log to the path _debug_log_path() picks
(SYMBIOSIS_BRAIN_DEBUG_LOG, else <TMPDIR>/brain-hook-debug.log).
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

CONFIG_PATH = Path.home() / ".claude" / "symbiosis-brain-pre-action.json"


def _tmp_dir() -> Path:
    """Temp dir, matching the bash hooks' ${TMPDIR:-${TEMP:-/tmp}} chain.

    Single source of truth for hook-side temp artifacts (debug log, recall
    seen-files) so they stay co-located and test-isolatable via env override
    (see [[mistakes/test-subprocess-inherits-system-tmpdir]])."""
    base = os.environ.get("TMPDIR") or os.environ.get("TEMP") or "/tmp"
    return Path(base)


DEBUG_LOG_ENV = "SYMBIOSIS_BRAIN_DEBUG_LOG"


def _debug_log_path() -> Path:
    """Where hook-side debug lines land.

    `SYMBIOSIS_BRAIN_DEBUG_LOG` (an ABSOLUTE path to a file) wins; otherwise
    `_tmp_dir()/brain-hook-debug.log`, exactly as before. The override exists so
    the test suite stops appending to the live user log (A-N3): TMPDIR alone does
    not cover subprocesses that rebuild their environment from scratch.
    A relative value is ignored — a relative debug path would follow whatever cwd
    a hook happened to inherit."""
    override = os.environ.get(DEBUG_LOG_ENV, "").strip()
    if override:
        p = Path(override)
        if p.is_absolute():
            return p
    return _tmp_dir() / "brain-hook-debug.log"


def _debug_log(msg: str) -> None:
    try:
        with _debug_log_path().open("a", encoding="utf-8") as f:
            ts = datetime.now(timezone.utc).isoformat()
            f.write(f"[{ts}] pre_action_config: {msg}\n")
    except OSError:
        pass


def _data_dir() -> Path:
    """Package data dir shipping with the wheel. Unlike hooks/ and templates/
    (which live at the REPO ROOT and are copied out by the installer), routing
    data must be importable at runtime by `search-gist`, so it lives UNDER the
    package and is included in the wheel (see pyproject force-include)."""
    return Path(__file__).parent / "data"


def routing_default_path() -> Path:
    """Shipped default routing catalog (C1). Read-only; fail-open if absent."""
    return _data_dir() / "tool-routing.json"


def routing_local_path(vault_path: Path) -> Path:
    """Per-install routing override (C2). Lives under the vault and is
    git-ignored (privacy) + excluded from brain_search (sync.py is md-only)."""
    return Path(vault_path) / "tool-routing.local.json"


_DEFAULT_BASH_WHITELIST = [
    r"^git (commit|push|merge|rebase|reset|cherry-pick|revert|tag)",
    r"^(npm|pnpm|yarn|pip|uv) (install|add|remove|uninstall)",
    r"^docker (push|deploy|run)",
    r"^winget (install|uninstall|upgrade)",
    r"^claude mcp (add|remove)",
    r"^gh pr (create|merge|close|edit)",
    r"^\.?/.+\.(sh|ps1|py|bat|cmd)",
]


@dataclass
class PreActionConfig:
    enabled: bool = True
    matchers: list[str] = field(default_factory=lambda: [
        "Task", "Agent", "Edit", "Write", "MultiEdit", "NotebookEdit", "Bash", "PowerShell"
    ])
    # Name kept as `bash_whitelist` (not renamed) — it gates PowerShell commands
    # too via the same mechanism (see __main__._run_pre_action_recall, which
    # applies it to both "Bash" and "PowerShell" tool_name).
    bash_whitelist: list[str] = field(default_factory=lambda: list(_DEFAULT_BASH_WHITELIST))
    excluded_note_types: list[str] = field(default_factory=lambda: ["user"])
    hit_limit: int = 3
    query_max_chars: int = 500
    timeout_seconds: int = 30
    recall_dedup_enabled: bool = True
    recall_dedup_ttl_seconds: int = 120
    # Prompt-path (UserPromptSubmit) recall dedup window. Separate from the
    # 120 s pre-action window on purpose: a user prompt arrives once every
    # minute or two and would slip straight through that window, re-injecting
    # the same notes turn after turn. Killed by the SAME switch as the
    # pre-action dedup (`recall_dedup_enabled`) — we do not breed kill-switches.
    prompt_recall_dedup_ttl_seconds: int = 1800
    # --- Stage-4 tool-routing (C1/C2). All fields are plain JSON-overridable via
    # the same ~/.claude/symbiosis-brain-pre-action.json file; load_config()'s
    # generic hasattr+type-check loop validates them with no extra code. ---
    routing_enabled: bool = True
    # "decompose" (default, spec §5.3 decision A): RULES_TOOLS on cadence, suppressed
    # on a supersede-route match. "additive": original RULES_TEXT verbatim + routes.
    routing_mode: str = "decompose"
    # Max routes injected per turn (spec §4.3); >cap → top-K by priority DESC.
    routing_cap: int = 2
    # augment-route session dedup TTL, seconds (spec §5.2). Long so an augment hint
    # is shown ~once/session; supersede routes are NOT seen-deduped (RULES_TOOLS logic).
    routing_seen_ttl_seconds: int = 86400
    # --- Stage-4b Serena pre-edit advisory (F4). When True, code edits (Edit/Write/
    # MultiEdit) on a code file with Serena present get a one-line "map dependencies
    # first" nudge as additionalContext (never blocks). JSON-overridable like above. ---
    serena_advisory_enabled: bool = True


def load_config(path: Path = CONFIG_PATH) -> PreActionConfig:
    """Load config file; return defaults if missing or malformed."""
    cfg = PreActionConfig()
    if not path.exists():
        return cfg
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        _debug_log(f"load failed: {e}")
        return cfg
    if not isinstance(data, dict):
        _debug_log(f"config root is not a dict: {type(data).__name__}")
        return cfg
    for key, value in data.items():
        if not hasattr(cfg, key):
            _debug_log(f"unknown config key: {key}")
            continue
        expected = type(getattr(cfg, key))
        if not isinstance(value, expected):
            _debug_log(
                f"type mismatch for '{key}': expected {expected.__name__}, "
                f"got {type(value).__name__}"
            )
            continue
        setattr(cfg, key, value)
    return cfg
