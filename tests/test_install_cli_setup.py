import json
import subprocess
import sys
from pathlib import Path

import pytest

from symbiosis_brain import install_cli
from symbiosis_brain import install_lib


def test_setup_with_explicit_vault_creates_structure_and_settings(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    settings = tmp_path / "settings.json"
    claude_md = tmp_path / "CLAUDE.md"
    monkeypatch.setattr(install_cli, "_settings_path", lambda: settings)
    monkeypatch.setattr(install_cli, "_claude_md_path", lambda: claude_md)
    monkeypatch.setattr(install_cli, "_hook_dir_str", lambda: "~/.claude/hooks")
    monkeypatch.setattr(install_cli, "_skill_dir", lambda: tmp_path / "skills")
    # Skip subprocess steps (MCP + copies) for this slice
    monkeypatch.setattr(install_cli, "_register_mcp", lambda *a, **kw: None)
    monkeypatch.setattr(install_cli, "_copy_skills", lambda *a, **kw: None)
    monkeypatch.setattr(install_cli, "_copy_hooks", lambda *a, **kw: None)

    args = type("A", (), {"vault": str(vault), "repair": False, "target": "claude-code"})()
    install_cli.cmd_setup(args)

    assert (vault / "reference" / "scope-taxonomy.md").exists()
    assert json.loads(settings.read_text())["statusLine"]["command"]
    assert "<!-- symbiosis-brain v1: global -->" in claude_md.read_text(encoding="utf-8")


def test_setup_idempotent(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    settings = tmp_path / "settings.json"
    claude_md = tmp_path / "CLAUDE.md"
    monkeypatch.setattr(install_cli, "_settings_path", lambda: settings)
    monkeypatch.setattr(install_cli, "_claude_md_path", lambda: claude_md)
    monkeypatch.setattr(install_cli, "_hook_dir_str", lambda: "~/.claude/hooks")
    monkeypatch.setattr(install_cli, "_skill_dir", lambda: tmp_path / "skills")
    monkeypatch.setattr(install_cli, "_register_mcp", lambda *a, **kw: None)
    monkeypatch.setattr(install_cli, "_copy_skills", lambda *a, **kw: None)
    monkeypatch.setattr(install_cli, "_copy_hooks", lambda *a, **kw: None)

    args = type("A", (), {"vault": str(vault), "repair": False, "target": "claude-code"})()
    install_cli.cmd_setup(args)
    install_cli.cmd_setup(args)

    # No duplicate marker block
    text = claude_md.read_text(encoding="utf-8")
    assert text.count("<!-- symbiosis-brain v1: global -->") == 1


def test_register_mcp_calls_claude_mcp_add_when_absent(tmp_path, monkeypatch):
    calls = []
    def fake_run(args, **kw):
        calls.append(args)
        class P: returncode = 0; stdout = ""; stderr = ""
        if "list" in args:
            P.stdout = ""  # not registered
        return P()
    monkeypatch.setattr(install_cli.subprocess, "run", fake_run)
    install_cli._register_mcp(Path("/tmp/v"))
    assert any("add" in args for args in calls)


def test_register_mcp_skips_when_already_registered(tmp_path, monkeypatch):
    calls = []
    def fake_run(args, **kw):
        calls.append(args)
        class P: returncode = 0; stdout = ""; stderr = ""
        if "list" in args:
            P.stdout = "symbiosis-brain  symbiosis-brain serve --vault /tmp/v\n"
        return P()
    monkeypatch.setattr(install_cli.subprocess, "run", fake_run)
    install_cli._register_mcp(Path("/tmp/v"))
    assert not any("add" in args for args in calls), "Must not call `add` when already listed"


def test_copy_skills_copies_all_present(tmp_path, monkeypatch):
    src = tmp_path / "src_skills"
    for s in ("brain-init", "brain-recall", "brain-save", "brain-project-init"):
        (src / s).mkdir(parents=True)
        (src / s / "SKILL.md").write_text(f"# {s}\n", encoding="utf-8")
    monkeypatch.setattr(install_cli, "_packaged_skills_dir", lambda: src)

    target = tmp_path / "claude_skills"
    install_cli._copy_skills(target)

    for s in ("brain-init", "brain-recall", "brain-save", "brain-project-init"):
        assert (target / s / "SKILL.md").read_text(encoding="utf-8") == f"# {s}\n"


def test_copy_skills_backs_up_existing_with_different_content(tmp_path, monkeypatch):
    src = tmp_path / "src_skills"
    (src / "brain-init").mkdir(parents=True)
    (src / "brain-init" / "SKILL.md").write_text("NEW\n", encoding="utf-8")
    monkeypatch.setattr(install_cli, "_packaged_skills_dir", lambda: src)

    target = tmp_path / "claude_skills"
    (target / "brain-init").mkdir(parents=True)
    (target / "brain-init" / "SKILL.md").write_text("OLD\n", encoding="utf-8")
    install_cli._copy_skills(target)

    assert (target / "brain-init" / "SKILL.md").read_text(encoding="utf-8") == "NEW\n"
    backups = list((target / "brain-init").glob("SKILL.md.bak.*"))
    assert len(backups) == 1


def test_copy_hooks_copies_all_files(tmp_path, monkeypatch):
    src = tmp_path / "src_hooks"
    src.mkdir()
    for h in ("brain-session-start.py", "brain-save-trigger.py",
              "sb-statusline.sh", "sb-line.sh", "sb-base-statusline.sh"):
        (src / h).write_text(f"# {h}\n", encoding="utf-8")
    monkeypatch.setattr(install_cli, "_packaged_hooks_dir", lambda: src)

    target = tmp_path / "claude_hooks"
    install_cli._copy_hooks(target)

    for h in ("brain-session-start.py", "brain-save-trigger.py",
              "sb-statusline.sh", "sb-line.sh", "sb-base-statusline.sh"):
        assert (target / h).exists()


def test_setup_rollback_restores_settings_on_failure(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    settings = tmp_path / "settings.json"
    claude_md = tmp_path / "CLAUDE.md"
    install_lib.atomic_write_json(settings, {"existing": "data"})
    claude_md.write_text("# original\n", encoding="utf-8")

    monkeypatch.setattr(install_cli, "_settings_path", lambda: settings)
    monkeypatch.setattr(install_cli, "_claude_md_path", lambda: claude_md)
    monkeypatch.setattr(install_cli, "_hook_dir_str", lambda: "~/.claude/hooks")
    monkeypatch.setattr(install_cli, "_skill_dir", lambda: tmp_path / "skills")

    def explode(*a, **kw):
        raise RuntimeError("simulated MCP failure")
    monkeypatch.setattr(install_cli, "_register_mcp", explode)
    monkeypatch.setattr(install_cli, "_copy_skills", lambda *a, **kw: None)
    monkeypatch.setattr(install_cli, "_copy_hooks", lambda *a, **kw: None)

    args = type("A", (), {"vault": str(vault), "repair": False, "target": "claude-code"})()
    try:
        install_cli.cmd_setup(args)
    except SystemExit:
        pass  # expected

    # settings.json and CLAUDE.md restored from backup
    assert json.loads(settings.read_text())["existing"] == "data"
    assert claude_md.read_text(encoding="utf-8") == "# original\n"


def test_register_mcp_raises_when_add_returns_nonzero(tmp_path, monkeypatch):
    """When `claude mcp add` returns non-zero, RuntimeError must propagate."""
    def fake_run(args, **kw):
        class P:
            returncode = 0
            stdout = ""
            stderr = ""
        if "list" in args:
            P.stdout = ""  # not registered
            return P()
        if "add" in args:
            P.returncode = 1
            P.stderr = "permission denied"
            return P()
        return P()
    monkeypatch.setattr(install_cli.subprocess, "run", fake_run)
    with pytest.raises(RuntimeError, match=r"claude mcp add.*failed"):
        install_cli._register_mcp(Path("/tmp/v"))
