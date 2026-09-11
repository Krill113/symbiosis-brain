"""`setup claude-code --vault <new>` on an existing install must move BOTH
pointers or say loudly why it did not — never print "Done" over the old vault.

Before: env.SYMBIOSIS_BRAIN_VAULT in settings.json was seeded non-clobbering
(an existing value always won), and _register_mcp returned as soon as the
word "symbiosis-brain" appeared in `claude mcp list`, without comparing the
path. So an explicit move printed "Done. Vault: <new>" while every session
kept working in the old vault — and doctor, which reads the same
registration, agreed with the lie (backlog, 2026-08-29).

An explicit `--vault` is the owner saying where the vault is. It now updates
the env value and re-registers the MCP server when the path differs, and
prints that the notes were NOT copied. Without `--vault` (a repair, the
interactive prompt) nothing that already exists is overwritten, as before.
If re-registration fails half-way, the old registration is put back — a move
that fails must not leave the user with no memory server at all.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from symbiosis_brain import install_cli


class _Claude:
    """Stands in for the `claude` CLI: remembers the one registration it holds
    and records every call, so a test can see remove/add and their order."""

    def __init__(self, registered: Path | None, fail_add_for: Path | None = None):
        self.registered = registered
        self.fail_add_for = fail_add_for
        self.calls: list[list[str]] = []

    def run(self, args, **kw):
        self.calls.append(list(args))

        class P:
            returncode = 0
            stdout = ""
            stderr = ""
        p = P()
        if args[:3] == ["claude", "mcp", "list"]:
            if self.registered is not None:
                p.stdout = (f"symbiosis-brain: symbiosis-brain serve --vault "
                            f"{self.registered} - ✓ Connected\n")
        elif args[:3] == ["claude", "mcp", "remove"]:
            self.registered = None
        elif args[:3] == ["claude", "mcp", "add"]:
            target = Path(args[args.index("--vault") + 1])
            if self.fail_add_for is not None and target == self.fail_add_for:
                p.returncode = 1
                p.stderr = "simulated failure"
            else:
                self.registered = target
        return p

    def verbs(self) -> list[str]:
        return [c[2] for c in self.calls if c[:2] == ["claude", "mcp"]]


def _home(tmp_path: Path, monkeypatch, env_vault: Path | None) -> Path:
    settings = tmp_path / "settings.json"
    if env_vault is not None:
        settings.write_text(json.dumps({"env": {"SYMBIOSIS_BRAIN_VAULT": str(env_vault)}}),
                            encoding="utf-8")
    monkeypatch.setattr(install_cli, "_settings_path", lambda: settings)
    monkeypatch.setattr(install_cli, "_claude_md_path", lambda: tmp_path / "CLAUDE.md")
    monkeypatch.setattr(install_cli, "_hook_dir_str", lambda: str(tmp_path / "hooks"))
    monkeypatch.setattr(install_cli, "_skill_dir", lambda: tmp_path / "skills")
    monkeypatch.setattr(install_cli, "_command_dir", lambda: tmp_path / "commands")
    monkeypatch.setattr(install_cli, "_copy_skills", lambda *a, **kw: [])
    monkeypatch.setattr(install_cli, "_copy_hooks", lambda *a, **kw: [])
    monkeypatch.setattr(install_cli, "_copy_commands", lambda *a, **kw: [])
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.delenv("SYMBIOSIS_BRAIN_VAULT", raising=False)
    return settings


def _env_vault(settings: Path) -> str | None:
    return json.loads(settings.read_text(encoding="utf-8")).get("env", {}).get("SYMBIOSIS_BRAIN_VAULT")


def _args(vault: Path | None, repair: bool = False):
    return type("A", (), {"vault": str(vault) if vault else None, "repair": repair,
                          "target": "claude-code"})()


def test_explicit_new_vault_moves_env_and_registration(tmp_path, monkeypatch, capsys):
    old, new = tmp_path / "old-vault", tmp_path / "new-vault"
    old.mkdir()
    settings = _home(tmp_path, monkeypatch, env_vault=old)
    claude = _Claude(registered=old)
    monkeypatch.setattr(install_cli.subprocess, "run", claude.run)

    install_cli.cmd_setup(_args(new))

    assert _env_vault(settings) == str(new)
    assert claude.registered == new
    assert claude.verbs() == ["list", "remove", "add"]
    out = capsys.readouterr().out
    assert "Vault changed" in out and str(old) in out and str(new) in out
    assert "not copied" in out.lower()


def test_explicit_same_vault_touches_nothing(tmp_path, monkeypatch, capsys):
    vault = tmp_path / "vault"
    vault.mkdir()
    settings = _home(tmp_path, monkeypatch, env_vault=vault)
    claude = _Claude(registered=vault)
    monkeypatch.setattr(install_cli.subprocess, "run", claude.run)

    install_cli.cmd_setup(_args(vault))

    assert _env_vault(settings) == str(vault)
    assert claude.verbs() == ["list"]
    assert "Vault changed" not in capsys.readouterr().out


def test_repair_without_vault_overwrites_nothing(tmp_path, monkeypatch, capsys):
    """No --vault: the owner did not ask for a move, so existing values win —
    the non-clobbering rule is kept exactly where it is right."""
    old = tmp_path / "old-vault"
    old.mkdir()
    settings = _home(tmp_path, monkeypatch, env_vault=old)
    monkeypatch.setenv("SYMBIOSIS_BRAIN_VAULT", str(old))
    claude = _Claude(registered=old)
    monkeypatch.setattr(install_cli.subprocess, "run", claude.run)

    install_cli.cmd_setup(_args(None, repair=True))

    assert _env_vault(settings) == str(old)
    assert "remove" not in claude.verbs() and "add" not in claude.verbs()


def test_prompted_vault_does_not_override_existing_values(tmp_path, monkeypatch):
    """Only an explicit --vault moves. A path typed at the interactive prompt
    on an install that already has one keeps the old non-clobbering rule — the
    scope of the change is the flag the bug was reported against."""
    old, typed = tmp_path / "old-vault", tmp_path / "typed-vault"
    old.mkdir()
    settings = _home(tmp_path, monkeypatch, env_vault=old)
    monkeypatch.setattr(install_cli, "_ask_vault_path", lambda default: typed)
    claude = _Claude(registered=old)
    monkeypatch.setattr(install_cli.subprocess, "run", claude.run)

    install_cli.cmd_setup(_args(None))

    assert _env_vault(settings) == str(old)
    assert claude.registered == old
    assert "remove" not in claude.verbs()


def test_failed_re_registration_puts_the_old_one_back(tmp_path, monkeypatch):
    old, new = tmp_path / "old-vault", tmp_path / "new-vault"
    old.mkdir()
    settings = _home(tmp_path, monkeypatch, env_vault=old)
    claude = _Claude(registered=old, fail_add_for=new)
    monkeypatch.setattr(install_cli.subprocess, "run", claude.run)

    with pytest.raises(SystemExit):
        install_cli.cmd_setup(_args(new))

    assert claude.registered == old, "a failed move must not leave no memory server"
    assert claude.verbs()[:4] == ["list", "remove", "add", "add"]
    assert _env_vault(settings) == str(old), "settings.json rolled back from its backup"


@pytest.mark.parametrize("line, expected", [
    ("symbiosis-brain: symbiosis-brain serve --vault /v/one - ✓ Connected", "/v/one"),
    ('symbiosis-brain: symbiosis-brain serve --vault "/v/with space" - ✓ Connected', "/v/with space"),
    ("serena: uvx serena - ✓ Connected", None),
])
def test_one_parser_reads_the_registered_vault(line, expected):
    """_resolve_vault_path and _register_mcp read the registration through the
    same function — two parsers of one line is how they would drift apart."""
    got = install_cli._vault_from_mcp_listing(line + "\n")
    assert (str(got).replace("\\", "/") if got else None) == expected
