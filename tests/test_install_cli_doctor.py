import json
import sqlite3
from pathlib import Path

from symbiosis_brain import install_cli, install_lib


def test_doctor_reports_all_ok(tmp_path, monkeypatch, capsys):
    _green_install(tmp_path, monkeypatch)
    rc = install_cli.cmd_doctor(_args())
    out = capsys.readouterr().out
    assert "✗" not in out
    assert rc == 0


def test_doctor_reports_missing_hook(tmp_path, monkeypatch, capsys):
    _green_install(tmp_path, monkeypatch)
    (tmp_path / "hooks" / "brain-save-trigger.sh").unlink()
    rc = install_cli.cmd_doctor(_args())
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
    packaged = install_cli._packaged_hooks_dir()
    for name in install_cli.HOOK_FILES_SH:
        src = packaged / name
        # Real packaged content, not a placeholder: doctor now compares the two
        # installed hooks against the package (CP-6, spec §8.2), and a fixture full
        # of "ok" describes a STALE install, not a healthy one. write_text() may
        # translate newlines here — that is exactly why the comparison normalizes.
        (hooks / name).write_text(
            src.read_text(encoding="utf-8") if src.exists() else "ok",
            encoding="utf-8",
        )

    commands = tmp_path / "commands"
    commands.mkdir(exist_ok=True)
    for c in install_cli.COMMAND_FILES:
        (commands / c).write_text("ok", encoding="utf-8")

    vault = tmp_path / "vault"
    install_lib.scaffold_vault(vault)

    monkeypatch.setattr(install_cli, "_settings_path", lambda: settings)
    monkeypatch.setattr(install_cli, "_claude_md_path", lambda: claude_md)
    monkeypatch.setattr(install_cli, "_skill_dir", lambda: skills)
    monkeypatch.setattr(install_cli, "_hook_dir", lambda: hooks)
    monkeypatch.setattr(install_cli, "_command_dir", lambda: commands)
    monkeypatch.setattr(install_cli, "_resolve_vault_path", lambda: vault)
    monkeypatch.setattr(install_cli, "_check_mcp_running", lambda: True)
    # HOME *and* USERPROFILE (Stage-0 lesson, 00-plan §0.6 п. 2): Path.home() reads
    # USERPROFILE on Windows, and a path helper we forget to monkeypatch must not
    # reach the developer's live ~/.claude.
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
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


def test_resolve_vault_prefers_env_over_cli(monkeypatch, tmp_path):
    """SYMBIOSIS_BRAIN_VAULT is set on every live install and is free; `claude mcp list`
    health-checks every MCP server (~7-10s, and it starts a second `symbiosis-brain
    serve` against the live vault). Ask the env first."""
    def explode(*a, **kw):
        raise AssertionError("`claude mcp list` must not run when the env var is set")

    monkeypatch.setattr(install_cli.subprocess, "run", explode)
    monkeypatch.setattr(install_cli, "DEFAULT_VAULT", Path("/nonexistent"))
    monkeypatch.setenv("SYMBIOSIS_BRAIN_VAULT", str(tmp_path))

    assert install_cli._resolve_vault_path() == tmp_path


def test_register_mcp_warning_is_soft_and_english(tmp_path, monkeypatch, capsys):
    """The old WARN ("Пропускаю MCP-регистрацию") read like a broken install; in fact
    the server is simply already registered and the timeout was 10s (finding A4a)."""
    import subprocess as sp

    def fake_run(args, **kw):
        assert kw.get("timeout") == 30, "the list call must allow 30s"
        raise sp.TimeoutExpired(cmd="claude mcp list", timeout=30)

    monkeypatch.setattr(install_cli.subprocess, "run", fake_run)
    install_cli._register_mcp(Path("/tmp/v"))
    out = capsys.readouterr().out
    assert "MCP registration skipped" in out
    assert not any("Ѐ" <= ch <= "ӿ" for ch in out)


def test_doctor_requires_new_hooks(tmp_path, monkeypatch, capsys):
    """Every event hook sources sb-hooklib.sh, the status line sources sb-export.sh and
    PostToolUse runs brain-save-marker.sh — a missing one is a silent loss of function,
    so doctor must name it."""
    _green_install(tmp_path, monkeypatch)
    (tmp_path / "hooks" / "sb-hooklib.sh").unlink()

    rc = install_cli.cmd_doctor(_args())
    out = capsys.readouterr().out
    assert rc == 1
    assert "sb-hooklib.sh" in out


def test_doctor_requires_slash_command(tmp_path, monkeypatch, capsys):
    _green_install(tmp_path, monkeypatch)
    (tmp_path / "commands" / "brain-sync.md").unlink()

    rc = install_cli.cmd_doctor(_args())
    out = capsys.readouterr().out
    assert rc == 1
    assert "brain-sync.md" in out


def test_doctor_names_a_missing_permission(tmp_path, monkeypatch, capsys):
    """§8.2: len(sb_perms) >= 7 пропускал пропажу любого одного имени. Теперь —
    строгое включение множества, с печатью недостающих имён."""
    _green_install(tmp_path, monkeypatch)
    settings = tmp_path / "settings.json"
    data = json.loads(settings.read_text(encoding="utf-8"))
    data["permissions"]["allow"] = [
        p for p in install_cli.SB_PERMISSIONS
        if p != "mcp__symbiosis-brain__brain_report"
    ]
    install_lib.atomic_write_json(settings, data)

    rc = install_cli.cmd_doctor(_args())
    out = capsys.readouterr().out
    assert rc == 1
    assert "mcp__symbiosis-brain__brain_report" in out
    assert "--repair" in out


def test_doctor_accepts_extra_permissions(tmp_path, monkeypatch, capsys):
    """Строгое включение, а не равенство: чужие права в allow — не наша поломка."""
    _green_install(tmp_path, monkeypatch)
    settings = tmp_path / "settings.json"
    data = json.loads(settings.read_text(encoding="utf-8"))
    data["permissions"]["allow"] = list(install_cli.SB_PERMISSIONS) + ["Bash(ls:*)"]
    install_lib.atomic_write_json(settings, data)

    rc = install_cli.cmd_doctor(_args())
    assert rc == 0
    assert "✗" not in capsys.readouterr().out


def test_doctor_flags_a_stale_hook(tmp_path, monkeypatch, capsys):
    """§8.2: обновление пакета НЕ трогает ~/.claude/hooks (копирование только из
    setup/--repair, install_cli.py:245-266), поэтому доктор обязан сказать STALE,
    а не «All OK»."""
    _green_install(tmp_path, monkeypatch)
    hook = tmp_path / "hooks" / "sb-export.sh"
    hook.write_text(hook.read_text(encoding="utf-8") + "\n# stale\n", encoding="utf-8")

    rc = install_cli.cmd_doctor(_args())
    out = capsys.readouterr().out
    assert rc == 1
    assert "STALE" in out
    assert "sb-export.sh" in out
    assert "--repair" in out


def test_doctor_flags_a_stale_session_start_hook(tmp_path, monkeypatch, capsys):
    """Третий хук списка, и он не бонус: `brain-session-start.sh` правит CP-5
    (Task 5.3), регистрируется он из `hook_dir` (`install_lib.py:207-213`), то
    есть попадает в `~/.claude/hooks` только через `setup`/`--repair`. Без этой
    строки в `STALE_CHECKED_HOOKS` обновление пакета без `--repair` оставило бы
    старый мост модели рядом с новым python, а доктор сказал бы «All OK»."""
    _green_install(tmp_path, monkeypatch)
    hook = tmp_path / "hooks" / "brain-session-start.sh"
    hook.write_text(hook.read_text(encoding="utf-8") + "\n# stale\n", encoding="utf-8")

    rc = install_cli.cmd_doctor(_args())
    out = capsys.readouterr().out
    assert rc == 1
    assert "STALE" in out
    assert "brain-session-start.sh" in out
    assert "--repair" in out


def test_doctor_flags_a_stale_pre_action_trigger_hook(tmp_path, monkeypatch, capsys):
    """A6 (F12): `brain-pre-action-trigger.sh` is copied into `~/.claude/hooks` by
    every `setup`/`--repair` (it is in `HOOK_FILES_SH`) exactly like the other
    three, but it used to be left out of `STALE_CHECKED_HOOKS` on the theory that
    a fresh install always runs it straight from `$SYMBIOSIS_BRAIN_TOOLS` and
    therefore `cannot go stale`. A legacy install registered against the
    hook_dir copy instead (install_lib.py's three-prefix note) DOES read this
    file, so upgrading the package without `--repair` could leave it stale while
    doctor said `All OK`, same failure shape as the other three hooks."""
    _green_install(tmp_path, monkeypatch)
    hook = tmp_path / "hooks" / "brain-pre-action-trigger.sh"
    hook.write_text(hook.read_text(encoding="utf-8") + "\n# stale\n", encoding="utf-8")

    rc = install_cli.cmd_doctor(_args())
    out = capsys.readouterr().out
    assert rc == 1
    assert "STALE" in out
    assert "brain-pre-action-trigger.sh" in out
    assert "--repair" in out


def test_doctor_stale_check_ignores_line_endings(tmp_path, monkeypatch, capsys):
    """CRLF-безопасность (00-plan §0.6 п. 7): git может выдать хук с CRLF, это не
    расхождение содержимого."""
    _green_install(tmp_path, monkeypatch)
    hook = tmp_path / "hooks" / "brain-save-trigger.sh"
    body = hook.read_text(encoding="utf-8").replace("\r\n", "\n")
    hook.write_bytes(body.replace("\n", "\r\n").encode("utf-8"))

    rc = install_cli.cmd_doctor(_args())
    out = capsys.readouterr().out
    assert "STALE" not in out
    assert rc == 0


def test_doctor_does_not_call_a_missing_hook_stale(tmp_path, monkeypatch, capsys):
    """Отсутствующий файл — это проверка 3 (MISSING), а не STALE: две находки на
    одну поломку читаются как две поломки."""
    _green_install(tmp_path, monkeypatch)
    (tmp_path / "hooks" / "sb-export.sh").unlink()

    rc = install_cli.cmd_doctor(_args())
    out = capsys.readouterr().out
    assert rc == 1
    assert "MISSING" in out
    assert "STALE" not in out
