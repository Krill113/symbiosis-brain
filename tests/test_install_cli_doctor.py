import json
import sqlite3
from pathlib import Path

from symbiosis_brain import install_cli, install_lib


def test_doctor_reports_all_ok(tmp_path, monkeypatch, capsys):
    settings = tmp_path / "settings.json"
    install_lib.atomic_write_json(settings, {
        "hooks": {"SessionStart": [{"hooks": [{"command": "bash ~/.claude/hooks/brain-session-start.sh"}]}]},
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
    for h in ("brain-session-start.sh", "brain-save-trigger.sh", "brain-sync.sh", "sb-statusline.sh"):
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
        "hooks": {"SessionStart": [{"hooks": [{"command": "bash ~/.claude/hooks/brain-session-start.sh"}]}]},
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
    # brain-save-trigger.sh missing
    (hooks / "brain-session-start.sh").write_text("ok", encoding="utf-8")
    (hooks / "brain-sync.sh").write_text("ok", encoding="utf-8")
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
    assert "brain-save-trigger.sh" in out
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


def _args(**kwargs):
    return type("A", (), kwargs)()


def _green_install(tmp_path, monkeypatch):
    """Build a fully healthy fake install and point doctor at it.

    Hooks, skills and permissions come from install_cli's own tuples, so a later
    checkpoint that ships one more hook or skill does not turn this fixture red.
    Returns the vault path.

    SHARED FIXTURE: this is the only doctor fixture in this file. CP-7 extends it
    (slash-command dir + _command_dir monkeypatch) instead of adding a second one —
    see review/stitch-log.md S1. Keep the return value (the vault path) stable.
    """
    settings = tmp_path / "settings.json"
    install_lib.atomic_write_json(settings, {
        "hooks": {"SessionStart": [
            {"hooks": [{"command": "bash ~/.claude/hooks/brain-session-start.sh"}]}]},
        "statusLine": {"command": "bash ~/.claude/hooks/sb-statusline.sh"},
        "permissions": {"allow": list(install_cli.SB_PERMISSIONS)},
    })
    claude_md = tmp_path / "CLAUDE.md"
    claude_md.write_text("<!-- symbiosis-brain v1: global -->\n", encoding="utf-8")

    skills = tmp_path / "skills"
    for name in install_cli.SKILL_NAMES:
        (skills / name).mkdir(parents=True, exist_ok=True)
        (skills / name / "SKILL.md").write_text("ok", encoding="utf-8")

    hooks = tmp_path / "hooks"
    hooks.mkdir(exist_ok=True)
    for name in install_cli.HOOK_FILES_SH:
        (hooks / name).write_text("ok", encoding="utf-8")

    vault = tmp_path / "vault"
    install_lib.scaffold_vault(vault)

    monkeypatch.setattr(install_cli, "_settings_path", lambda: settings)
    monkeypatch.setattr(install_cli, "_claude_md_path", lambda: claude_md)
    monkeypatch.setattr(install_cli, "_skill_dir", lambda: skills)
    monkeypatch.setattr(install_cli, "_hook_dir", lambda: hooks)
    monkeypatch.setattr(install_cli, "_resolve_vault_path", lambda: vault)
    monkeypatch.setattr(install_cli, "_check_mcp_running", lambda: True)
    return vault


def test_doctor_prints_sqlite_version(tmp_path, monkeypatch, capsys):
    _green_install(tmp_path, monkeypatch)

    rc = install_cli.cmd_doctor(_args())
    out = capsys.readouterr().out

    assert "SQLite" in out
    assert sqlite3.sqlite_version in out
    assert rc == 0


def test_doctor_warns_but_does_not_fail_on_vulnerable_sqlite(tmp_path, monkeypatch, capsys):
    _green_install(tmp_path, monkeypatch)
    monkeypatch.setattr(sqlite3, "sqlite_version", "3.50.4")

    rc = install_cli.cmd_doctor(_args())
    out = capsys.readouterr().out

    assert "⚠ SQLite" in out
    assert "3.50.4" in out
    assert "WAL-Reset" in out
    # Parking is deliberate (owner decision 3, confirmed by the lead in 00-plan 11.4):
    # a vulnerable build is a warning, it must not paint doctor red and must not be
    # counted as an issue. The reminder that the bug is parked lives in the handoff
    # and in the plan, not in a permanently red doctor.
    assert "✗" not in out
    assert rc == 0


def _seed_db(vault):
    db = vault / ".index" / "brain.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db))
    conn.execute("CREATE TABLE t(a)")
    conn.commit()
    conn.close()
    return db


def test_doctor_runs_quick_check_by_default(tmp_path, monkeypatch, capsys):
    """Owner decision 3 (2026-08-25) is literal: doctor "показывает версию движка,
    помечает уязвимую …, гоняет quick_check". No opt-in. Measured cost on the live
    24 MB vault: 1.87 s — against the ~10 s `claude mcp list` that doctor already
    pays for on the same run."""
    vault = _green_install(tmp_path, monkeypatch)
    _seed_db(vault)

    rc = install_cli.cmd_doctor(_args())
    out = capsys.readouterr().out

    assert "✓ quick_check" in out
    assert "✗" not in out
    assert rc == 0


def test_doctor_deep_runs_integrity_check(tmp_path, monkeypatch, capsys):
    """`--deep` is what stays opt-in: PRAGMA integrity_check walks every page and
    every index, which is the expensive check — quick_check skips index cross-refs."""
    vault = _green_install(tmp_path, monkeypatch)
    _seed_db(vault)

    rc = install_cli.cmd_doctor(_args(deep=True))
    out = capsys.readouterr().out

    assert "✓ quick_check" in out          # the default check still runs
    assert "✓ integrity_check" in out
    assert "✗" not in out
    assert rc == 0


def test_doctor_without_deep_skips_integrity_check(tmp_path, monkeypatch, capsys):
    """Guard against the opposite mistake — silently making the expensive check
    unconditional too."""
    vault = _green_install(tmp_path, monkeypatch)
    _seed_db(vault)

    install_cli.cmd_doctor(_args())

    assert "integrity_check" not in capsys.readouterr().out
