"""Public adapter dispatch and read-only packaged resource contracts."""
from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

from symbiosis_brain import install_cli


def test_resources_skills_prints_existing_package_directory(monkeypatch, capsys, tmp_path):
    skills = tmp_path / "skills"
    (skills / "brain-init").mkdir(parents=True)
    (skills / "brain-init" / "SKILL.md").write_text("synthetic", encoding="utf-8")
    monkeypatch.setattr(install_cli, "_packaged_skills_dir", lambda: skills)
    args = install_cli.build_parser().parse_args(["resources", "--skills"])
    assert args.func(args) == 0
    assert capsys.readouterr().out.strip() == str(skills.resolve())
    assert sorted(p.name for p in skills.iterdir()) == ["brain-init"]


def test_resources_missing_skills_has_actionable_error(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(install_cli, "_packaged_skills_dir", lambda: tmp_path / "missing")
    args = install_cli.build_parser().parse_args(["resources", "--skills"])
    assert args.func(args) != 0
    captured = capsys.readouterr()
    assert "skills" in (captured.out + captured.err).lower()


@pytest.mark.parametrize("command,module", [
    ("codex-hook", "symbiosis_brain.adapters.codex.supervisor"),
    ("packaged-sync", "symbiosis_brain.adapters.codex.sync_launcher"),
])
def test_hidden_codex_dispatch_forwards_argv_lazily(monkeypatch, command, module):
    calls = []
    backend = types.ModuleType(module)
    backend.run_cli = lambda argv: calls.append(argv) or 17
    monkeypatch.setitem(sys.modules, module, backend)
    monkeypatch.setattr(sys, "argv", ["symbiosis-brain", command, "--event", "synthetic"])
    with pytest.raises(SystemExit) as exited:
        install_cli.main()
    assert exited.value.code == 17
    assert calls == [["--event", "synthetic"]]


def test_codex_automatic_installer_is_not_exposed():
    parser = install_cli.build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["setup", "codex"])


def test_init_vault_delegates_only_to_shared_scaffold(monkeypatch, tmp_path):
    target = tmp_path / "vault"
    seen = []
    monkeypatch.setattr(install_cli.install_lib, "scaffold_vault", lambda path: seen.append(path))
    args = install_cli.build_parser().parse_args(["init-vault", "--vault", str(target)])
    assert args.func(args) == 0
    assert seen == [target.resolve()]
    assert not target.exists()


def test_init_vault_requires_explicit_path():
    with pytest.raises(SystemExit):
        install_cli.build_parser().parse_args(["init-vault"])


def test_init_vault_preserves_existing_notes(monkeypatch, tmp_path):
    target = tmp_path / "vault"
    target.mkdir()
    note = target / "README.md"
    note.write_text("user content", encoding="utf-8")
    args = install_cli.build_parser().parse_args(["init-vault", "--vault", str(target)])
    assert args.func(args) == 0
    assert args.func(args) == 0
    assert note.read_text(encoding="utf-8") == "user content"
    assert (target / "reference" / "scope-taxonomy.md").is_file()


@pytest.mark.parametrize("command,handler", [
    ("setup", "cmd_setup"), ("doctor", "cmd_doctor"), ("uninstall", "cmd_uninstall"),
])
def test_opencode_commands_delegate_without_loading_claude(monkeypatch, command, handler):
    backend = types.ModuleType("symbiosis_brain.adapters.opencode.install")
    calls = []
    setattr(backend, handler, lambda args: calls.append(args) or 7)
    monkeypatch.setitem(sys.modules, backend.__name__, backend)
    argv = [command, "opencode", "--config-dir", "synthetic-config", "--node", "synthetic-node"]
    if command == "setup":
        argv += ["--vault", "synthetic-vault", "--repair"]
    args = install_cli.build_parser().parse_args(argv)
    assert args.func(args) == 7
    assert len(calls) == 1
    assert calls[0].config_dir == "synthetic-config"
    assert calls[0].node == "synthetic-node"
    if command == "setup":
        assert calls[0].vault == "synthetic-vault"
        assert calls[0].repair


def test_legacy_claude_dispatch_defaults_are_preserved(monkeypatch):
    calls = []
    monkeypatch.setattr(install_cli, "cmd_setup", lambda args: calls.append("setup"))
    monkeypatch.setattr(install_cli, "cmd_doctor", lambda args: calls.append("doctor"))
    monkeypatch.setattr(install_cli, "cmd_uninstall", lambda args: calls.append("uninstall"))
    parser = install_cli.build_parser()
    for argv in [["setup", "claude-code"], ["doctor"], ["uninstall"]]:
        args = parser.parse_args(argv)
        args.func(args)
    assert calls == ["setup", "doctor", "uninstall"]


@pytest.mark.parametrize("command,module", [
    ("codex-hook", "symbiosis_brain.adapters.codex.supervisor"),
    ("packaged-sync", "symbiosis_brain.adapters.codex.sync_launcher"),
])
def test_module_transport_dispatch_matches_console(monkeypatch, command, module):
    import importlib
    entry = importlib.import_module("symbiosis_brain.__main__")
    server = types.ModuleType("symbiosis_brain.server")
    def wrong_transport():
        pytest.fail("Hook transport fell through to the MCP server")
    server.main = wrong_transport
    monkeypatch.setitem(sys.modules, server.__name__, server)
    backend = types.ModuleType(module)
    calls = []
    backend.run_cli = lambda argv: calls.append(argv) or 19
    monkeypatch.setitem(sys.modules, module, backend)
    monkeypatch.setattr(sys, "argv", ["symbiosis_brain", command, "--event", "synthetic"])
    with pytest.raises(SystemExit) as exited:
        entry.main()
    assert exited.value.code == 19
    assert calls == [["--event", "synthetic"]]


def test_codex_hooks_is_a_stdout_renderer_not_an_installer(monkeypatch):
    module = types.ModuleType("symbiosis_brain.adapters.codex.manual")
    calls = []
    module.run_cli = lambda argv: calls.append(argv) or 9
    monkeypatch.setitem(sys.modules, module.__name__, module)
    args = install_cli.build_parser().parse_args([
        "codex-hooks", "--executable", "synthetic-exe", "--vault", "synthetic-vault",
    ])
    assert args.func(args) == 9
    assert calls == [["hooks", "--executable", "synthetic-exe", "--vault", "synthetic-vault"]]


def test_console_entry_hook_does_not_import_install_cli(monkeypatch):
    import builtins
    import importlib
    entry = importlib.import_module("symbiosis_brain.entry_cli")
    backend = types.ModuleType("symbiosis_brain.adapters.codex.supervisor")
    calls = []
    backend.run_cli = lambda argv: calls.append(argv) or 23
    monkeypatch.setitem(sys.modules, backend.__name__, backend)
    native_import = builtins.__import__
    def guarded(name, *args, **kwargs):
        if name == "symbiosis_brain.install_cli":
            pytest.fail("Hook entry imported the heavy installer")
        return native_import(name, *args, **kwargs)
    monkeypatch.setattr(builtins, "__import__", guarded)
    monkeypatch.setattr(sys, "argv", ["symbiosis-brain", "codex-hook", "--event", "Stop"])
    with pytest.raises(SystemExit) as exited:
        entry.main()
    assert exited.value.code == 23
    assert calls == [["--event", "Stop"]]


def test_console_entry_keeps_legacy_commands(monkeypatch):
    import importlib
    entry = importlib.import_module("symbiosis_brain.entry_cli")
    calls = []
    monkeypatch.setattr(install_cli, "main", lambda: calls.append("legacy"))
    monkeypatch.setattr(sys, "argv", ["symbiosis-brain", "--version"])
    entry.main()
    assert calls == ["legacy"]


def test_resources_routing_prints_shipped_catalog_without_profile_changes(capsys):
    args = install_cli.build_parser().parse_args(["resources", "--routing"])
    assert args.func(args) == 0
    catalog = Path(capsys.readouterr().out.strip())
    assert catalog.name == "tool-routing.json"
    assert catalog.is_file()


def test_resources_rejects_ambiguous_selection():
    with pytest.raises(SystemExit):
        install_cli.build_parser().parse_args(["resources", "--skills", "--routing"])


def test_candidate_version_is_071():
    import symbiosis_brain
    assert symbiosis_brain.__version__ == "0.7.1"
