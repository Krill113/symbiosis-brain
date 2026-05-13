"""Config loader for pre-action recall hook (B1).

Loads `~/.claude/symbiosis-brain-pre-action.json` with fall-back to defaults.
Missing or malformed file → defaults + log to /tmp/brain-hook-debug.log.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

CONFIG_PATH = Path.home() / ".claude" / "symbiosis-brain-pre-action.json"


def _debug_log_path() -> Path:
    base = os.environ.get("TMPDIR") or os.environ.get("TEMP") or "/tmp"
    return Path(base) / "brain-hook-debug.log"


def _debug_log(msg: str) -> None:
    try:
        with _debug_log_path().open("a", encoding="utf-8") as f:
            ts = datetime.now(timezone.utc).isoformat()
            f.write(f"[{ts}] pre_action_config: {msg}\n")
    except OSError:
        pass


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
        "Task", "Edit", "Write", "MultiEdit", "NotebookEdit", "Bash"
    ])
    bash_whitelist: list[str] = field(default_factory=lambda: list(_DEFAULT_BASH_WHITELIST))
    excluded_note_types: list[str] = field(default_factory=lambda: ["user"])
    hit_limit: int = 3
    query_max_chars: int = 500
    timeout_seconds: int = 30


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
        if hasattr(cfg, key):
            setattr(cfg, key, value)
    return cfg
