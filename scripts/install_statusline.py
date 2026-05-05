"""Install Symbiosis Brain statusline wrapper into ~/.claude/settings.json.

Auto-detects existing user statusline; saves it in env-var so the wrapper
delegates to it. Idempotent. Backs up settings.json before each write.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from symbiosis_brain import install_lib

DEFAULT_SETTINGS = Path.home() / ".claude" / "settings.json"
# Tilde form preferred — Claude Code on Windows mishandles absolute backslash paths
# in statusLine.command (the statusline silently disappears). Bash expands ~ to $HOME
# which works on every platform.
DEFAULT_HOOK_PATH = "~/.claude/hooks/sb-statusline.sh"


def install(settings_path: Path = DEFAULT_SETTINGS,
            hook_path: str = DEFAULT_HOOK_PATH) -> None:
    if not settings_path.exists():
        settings_path.parent.mkdir(parents=True, exist_ok=True)
        install_lib.atomic_write_json(settings_path, {})
    settings = json.loads(settings_path.read_text(encoding="utf-8"))

    cmd = f"bash {hook_path}"
    current = (settings.get("statusLine") or {}).get("command")

    # Idempotent: already pointing at our wrapper
    if current and "sb-statusline" in current:
        return

    install_lib.backup_file(settings_path)

    if current:
        # User had a custom statusline — preserve it for the wrapper to delegate
        env = settings.setdefault("env", {})
        env["SYMBIOSIS_BRAIN_USER_STATUSLINE_CMD"] = current

    settings["statusLine"] = {
        "type": "command",
        "command": cmd,
        "refreshInterval": 10,
    }

    install_lib.atomic_write_json(settings_path, settings)


def main():
    parser = argparse.ArgumentParser(description="Install SB statusline wrapper")
    parser.add_argument("--settings", default=str(DEFAULT_SETTINGS))
    parser.add_argument("--hook", default=DEFAULT_HOOK_PATH)
    args = parser.parse_args()
    install(Path(args.settings), args.hook)
    print(f"Installed: {args.settings}")


if __name__ == "__main__":
    main()
