import json
from pathlib import Path

from symbiosis_brain import install_cli, install_lib


def test_uninstall_restores_settings_and_claude_md(tmp_path, monkeypatch):
    settings = tmp_path / "settings.json"
    claude_md = tmp_path / "CLAUDE.md"
    install_lib.atomic_write_json(settings, {"original": True})
    claude_md.write_text("# original\n", encoding="utf-8")
    install_lib.backup_file(settings)
    install_lib.backup_file(claude_md)
    install_lib.atomic_write_json(settings, {"changed": True})
    claude_md.write_text("# changed\n", encoding="utf-8")

    skills = tmp_path / "skills"
    for s in install_cli.SKILL_NAMES:
        (skills / s).mkdir(parents=True)
        (skills / s / "SKILL.md").write_text("x", encoding="utf-8")
    hooks = tmp_path / "hooks"
    hooks.mkdir()
    for h in ("brain-session-start.py", "brain-save-trigger.py", "sb-statusline.sh"):
        (hooks / h).write_text("x", encoding="utf-8")

    monkeypatch.setattr(install_cli, "_settings_path", lambda: settings)
    monkeypatch.setattr(install_cli, "_claude_md_path", lambda: claude_md)
    monkeypatch.setattr(install_cli, "_skill_dir", lambda: skills)
    monkeypatch.setattr(install_cli, "_hook_dir", lambda: hooks)
    monkeypatch.setattr(install_cli.subprocess, "run",
                         lambda *a, **kw: type("P", (), {"returncode": 0, "stdout": "", "stderr": ""})())

    args = type("A", (), {})()
    install_cli.cmd_uninstall(args)

    assert json.loads(settings.read_text())["original"] is True
    assert claude_md.read_text() == "# original\n"
    for s in install_cli.SKILL_NAMES:
        assert not (skills / s / "SKILL.md").exists()
    for h in ("brain-session-start.py", "brain-save-trigger.py"):
        assert not (hooks / h).exists()
