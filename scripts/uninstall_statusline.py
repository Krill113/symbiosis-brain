"""Uninstall Symbiosis Brain statusline wrapper from ~/.claude/settings.json.

Symmetric to install: restores user's previous statusline if saved, else removes.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from symbiosis_brain import install_lib

DEFAULT_SETTINGS = Path.home() / ".claude" / "settings.json"


def uninstall(settings_path: Path = DEFAULT_SETTINGS) -> None:
    if not settings_path.exists():
        return
    settings = json.loads(settings_path.read_text(encoding="utf-8"))
    install_lib.backup_file(settings_path)

    user_cmd = (settings.get("env") or {}).pop("SYMBIOSIS_BRAIN_USER_STATUSLINE_CMD", None)
    if user_cmd is not None:
        settings.setdefault("statusLine", {})
        settings["statusLine"]["command"] = user_cmd
        settings["statusLine"].setdefault("type", "command")
        settings["statusLine"].setdefault("refreshInterval", 10)
    else:
        settings.pop("statusLine", None)

    if settings.get("env") == {}:
        settings.pop("env", None)

    install_lib.atomic_write_json(settings_path, settings)


def main():
    parser = argparse.ArgumentParser(description="Uninstall SB statusline wrapper")
    parser.add_argument("--settings", default=str(DEFAULT_SETTINGS))
    args = parser.parse_args()
    uninstall(Path(args.settings))
    print(f"Uninstalled: {args.settings}")


if __name__ == "__main__":
    main()
