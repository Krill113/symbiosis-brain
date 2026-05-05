import json
from pathlib import Path

from symbiosis_brain import install_cli, install_lib


def _bash_settings(hook_dir="~/.claude/hooks"):
    return {
        "hooks": {
            "SessionStart": [
                {"matcher": "startup", "hooks": [{"type": "command",
                    "command": f"bash {hook_dir}/brain-session-start.sh", "timeout": 5}]},
            ],
            "Stop": [{"hooks": [{"type": "command",
                "command": f"bash {hook_dir}/brain-save-trigger.sh stop", "asyncRewake": True}]}],
            "PreCompact": [{"hooks": [{"type": "command",
                "command": f"bash {hook_dir}/brain-save-trigger.sh precompact"}]}],
            "UserPromptSubmit": [{"hooks": [{"type": "command",
                "command": f"bash {hook_dir}/brain-save-trigger.sh prompt-check"}]}],
        }
    }


def test_migrate_hooks_replaces_bash_commands_with_python(tmp_path, monkeypatch):
    settings = tmp_path / "settings.json"
    install_lib.atomic_write_json(settings, _bash_settings())
    monkeypatch.setattr(install_cli, "_settings_path", lambda: settings)

    args = type("A", (), {"rollback": False})()
    install_cli.cmd_migrate_hooks(args)

    data = json.loads(settings.read_text())
    cmds = []
    for ev_list in data["hooks"].values():
        for ev in ev_list:
            for h in ev.get("hooks", []):
                cmds.append(h["command"])
    assert all("bash " not in c for c in cmds)
    assert all(c.startswith("python ") for c in cmds)


def test_migrate_hooks_rollback_restores_bash(tmp_path, monkeypatch):
    settings = tmp_path / "settings.json"
    original = _bash_settings()
    install_lib.atomic_write_json(settings, original)
    monkeypatch.setattr(install_cli, "_settings_path", lambda: settings)

    install_cli.cmd_migrate_hooks(type("A", (), {"rollback": False})())
    install_cli.cmd_migrate_hooks(type("A", (), {"rollback": True})())

    data = json.loads(settings.read_text())
    cmds = []
    for ev_list in data["hooks"].values():
        for ev in ev_list:
            for h in ev.get("hooks", []):
                cmds.append(h["command"])
    assert any("bash " in c for c in cmds), "rollback must restore bash commands"
