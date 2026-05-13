import json
from pathlib import Path

from symbiosis_brain import install_cli, install_lib


def test_doctor_reports_all_ok(tmp_path, monkeypatch, capsys):
    settings = tmp_path / "settings.json"
    install_lib.atomic_write_json(settings, {
        "hooks": {"SessionStart": [{"hooks": [{"command": "python ~/.claude/hooks/brain-session-start.py"}]}]},
        "statusLine": {"command": "bash ~/.claude/hooks/sb-statusline.sh"},
        "permissions": {"allow": [
            "mcp__symbiosis-brain__brain_read",
            "mcp__symbiosis-brain__brain_search",
            "mcp__symbiosis-brain__brain_write",
            "mcp__symbiosis-brain__brain_context",
            "mcp__symbiosis-brain__brain_list",
            "mcp__symbiosis-brain__brain_status",
            "mcp__symbiosis-brain__brain_sync",
        ]},
    })
    claude_md = tmp_path / "CLAUDE.md"
    claude_md.write_text("foo\n<!-- symbiosis-brain v1: global -->\n", encoding="utf-8")
    skills = tmp_path / "skills"
    for s in install_cli.SKILL_NAMES:
        (skills / s).mkdir(parents=True)
        (skills / s / "SKILL.md").write_text("ok", encoding="utf-8")
    hooks = tmp_path / "hooks"
    hooks.mkdir()
    for h in ("brain-session-start.py", "brain-save-trigger.py", "sb-statusline.sh"):
        (hooks / h).write_text("ok", encoding="utf-8")
    vault = tmp_path / "vault"
    install_lib.scaffold_vault(vault)

    monkeypatch.setattr(install_cli, "_settings_path", lambda: settings)
    monkeypatch.setattr(install_cli, "_claude_md_path", lambda: claude_md)
    monkeypatch.setattr(install_cli, "_skill_dir", lambda: skills)
    monkeypatch.setattr(install_cli, "_hook_dir", lambda: hooks)
    monkeypatch.setattr(install_cli, "_resolve_vault_path", lambda: vault)
    monkeypatch.setattr(install_cli, "_check_mcp_running", lambda: True)

    args = type("A", (), {})()
    rc = install_cli.cmd_doctor(args)
    out = capsys.readouterr().out
    assert "✗" not in out
    assert rc == 0


def test_doctor_reports_missing_hook(tmp_path, monkeypatch, capsys):
    settings = tmp_path / "settings.json"
    install_lib.atomic_write_json(settings, {
        "hooks": {"SessionStart": [{"hooks": [{"command": "python ~/.claude/hooks/brain-session-start.py"}]}]},
        "statusLine": {"command": "bash ~/.claude/hooks/sb-statusline.sh"},
        "permissions": {"allow": [
            "mcp__symbiosis-brain__brain_read",
            "mcp__symbiosis-brain__brain_search",
            "mcp__symbiosis-brain__brain_write",
            "mcp__symbiosis-brain__brain_context",
            "mcp__symbiosis-brain__brain_list",
            "mcp__symbiosis-brain__brain_status",
            "mcp__symbiosis-brain__brain_sync",
        ]},
    })
    claude_md = tmp_path / "CLAUDE.md"
    claude_md.write_text("<!-- symbiosis-brain v1: global -->\n", encoding="utf-8")
    skills = tmp_path / "skills"
    for s in install_cli.SKILL_NAMES:
        (skills / s).mkdir(parents=True)
        (skills / s / "SKILL.md").write_text("ok", encoding="utf-8")
    hooks = tmp_path / "hooks"
    hooks.mkdir()
    # brain-save-trigger.py missing
    (hooks / "brain-session-start.py").write_text("ok", encoding="utf-8")
    (hooks / "sb-statusline.sh").write_text("ok", encoding="utf-8")
    vault = tmp_path / "vault"
    install_lib.scaffold_vault(vault)

    monkeypatch.setattr(install_cli, "_settings_path", lambda: settings)
    monkeypatch.setattr(install_cli, "_claude_md_path", lambda: claude_md)
    monkeypatch.setattr(install_cli, "_skill_dir", lambda: skills)
    monkeypatch.setattr(install_cli, "_hook_dir", lambda: hooks)
    monkeypatch.setattr(install_cli, "_resolve_vault_path", lambda: vault)
    monkeypatch.setattr(install_cli, "_check_mcp_running", lambda: True)

    args = type("A", (), {})()
    rc = install_cli.cmd_doctor(args)
    out = capsys.readouterr().out
    assert "✗" in out
    assert "brain-save-trigger.py" in out
    assert rc == 1


def test_resolve_vault_path_handles_path_with_spaces(monkeypatch):
    """Paths containing spaces (e.g. 'C:\\Program Files\\vault') must round-trip."""
    from symbiosis_brain import install_cli

    class _FakeProc:
        stdout = 'symbiosis-brain: symbiosis-brain serve --vault "C:\\Program Files\\my vault"\n'

    monkeypatch.setattr(install_cli.subprocess, "run", lambda *a, **kw: _FakeProc())
    monkeypatch.setattr(install_cli, "DEFAULT_VAULT", Path("/nonexistent"))
    monkeypatch.delenv("SYMBIOSIS_BRAIN_VAULT", raising=False)

    result = install_cli._resolve_vault_path()
    assert result == Path("C:\\Program Files\\my vault"), (
        f"Path-with-spaces parse failed: got {result!r}"
    )


def test_resolve_vault_path_falls_back_to_env_var(monkeypatch, tmp_path):
    """When claude mcp list yields nothing useful, fall back to SYMBIOSIS_BRAIN_VAULT env var."""
    from symbiosis_brain import install_cli

    class _FakeProc:
        stdout = ""

    monkeypatch.setattr(install_cli.subprocess, "run", lambda *a, **kw: _FakeProc())
    monkeypatch.setattr(install_cli, "DEFAULT_VAULT", Path("/nonexistent"))
    monkeypatch.setenv("SYMBIOSIS_BRAIN_VAULT", str(tmp_path))

    assert install_cli._resolve_vault_path() == tmp_path


def test_resolve_vault_path_env_var_overrides_dead_mcp_list(monkeypatch, tmp_path):
    """When `claude` binary is missing entirely, env var still works."""
    from symbiosis_brain import install_cli

    def fake_run(*a, **kw):
        raise FileNotFoundError("claude")

    monkeypatch.setattr(install_cli.subprocess, "run", fake_run)
    monkeypatch.setattr(install_cli, "DEFAULT_VAULT", Path("/nonexistent"))
    monkeypatch.setenv("SYMBIOSIS_BRAIN_VAULT", str(tmp_path))

    assert install_cli._resolve_vault_path() == tmp_path
