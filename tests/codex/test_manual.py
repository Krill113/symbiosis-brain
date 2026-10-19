from __future__ import annotations

import importlib
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys

import pytest


def manual_module():
    return importlib.import_module("symbiosis_brain.adapters.codex.manual")


def test_render_hooks_requires_absolute_paths(tmp_path):
    manual = manual_module()
    with pytest.raises(ValueError, match="absolute"):
        manual.render_hooks(Path("relative-program"), tmp_path / "vault")
    with pytest.raises(ValueError, match="absolute"):
        manual.render_hooks(tmp_path / "program", Path("relative-vault"))


def test_renderer_preserves_event_suffix_owned_by_template(tmp_path, monkeypatch):
    manual = manual_module()
    directory = tmp_path / "codex"
    directory.mkdir()
    (directory / "hooks.json").write_text(json.dumps({
        "hooks": {"Stop": [{"hooks": [{
            "command": "{{COMMAND}} --event Stop",
            "commandWindows": "{{COMMAND_WINDOWS}} --event Stop",
        }]}]},
    }), encoding="utf-8")
    monkeypatch.setattr(manual, "packaged_dir", lambda *args: tmp_path)
    hook = manual.render_hooks(tmp_path / "program.exe", tmp_path / "vault")["hooks"]["Stop"][0]["hooks"][0]
    assert hook["command"].endswith(" --event Stop")
    assert hook["commandWindows"].endswith(" --event Stop")


def test_render_hooks_preserves_plain_path_argv_and_event_budgets(tmp_path):
    manual = manual_module()
    executable = tmp_path / "Program Files" / "symbiosis-brain.exe"
    vault = tmp_path / "My Vault"
    hooks = manual.render_hooks(executable, vault)["hooks"]
    expected = {
        "SessionStart": 30, "UserPromptSubmit": 15, "PreToolUse": 15,
        "PostToolUse": 5, "PreCompact": 5, "PostCompact": 5, "Stop": 5,
        "SessionEnd": 3,
    }
    for event, budget in expected.items():
        hook = hooks[event][0]["hooks"][0]
        assert shlex.split(hook["command"]) == [
            str(executable), "codex-hook", "--vault", str(vault),
            "--event", event,
        ]
        assert hook["commandWindows"] == (
            f"& '{executable}' codex-hook --vault '{vault}' --event {event}"
        )
        assert hook["timeout"] == budget
    assert "PowerShell" in hooks["PreToolUse"][0]["matcher"].split("|")


@pytest.mark.parametrize("character", ['%', '!', '^', '&', '|', '<', '>', '"', '\n', '\r', '(', ')'])
def test_renderer_rejects_unsupported_windows_shell_characters(tmp_path, character):
    manual = manual_module()
    with pytest.raises(ValueError, match="unsupported"):
        manual.render_hooks(tmp_path / "program.exe", tmp_path / f"vault{character}suffix")


def test_manual_cli_emits_json_without_writing_profiles(tmp_path, monkeypatch, capsys):
    manual = manual_module()
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("CODEX_HOME", str(home / ".codex"))
    result = manual.run_cli([
        "hooks", "--executable", str(tmp_path / "program.exe"),
        "--vault", str(tmp_path / "vault"),
    ])
    output = capsys.readouterr()
    assert result == 0
    assert "SessionStart" in json.loads(output.out)["hooks"]
    assert not output.err
    assert list(home.iterdir()) == []


def test_manual_cli_fails_before_emitting_unsafe_configuration(tmp_path, capsys):
    result = manual_module().run_cli([
        "hooks", "--executable", str(tmp_path / "program.exe"),
        "--vault", str(tmp_path / "vault%PATH%"),
    ])
    output = capsys.readouterr()
    assert result == 2
    assert output.out == ""
    assert "unsupported" in output.err


def test_public_console_renderer_matches_manual_without_profile_writes(
    tmp_path, monkeypatch, capsys
):
    from symbiosis_brain import install_cli

    monkeypatch.setattr(sys, "argv", [
        "symbiosis-brain", "codex-hooks",
        "--executable", str(tmp_path / "program.exe"),
        "--vault", str(tmp_path / "vault"),
    ])
    with pytest.raises(SystemExit) as exited:
        install_cli.main()
    assert exited.value.code == 0
    value = json.loads(capsys.readouterr().out)
    hook = value["hooks"]["Stop"][0]["hooks"][0]
    assert hook["commandWindows"].endswith(" --event Stop")
    assert not (tmp_path / "vault").exists()


def test_public_scaffold_and_resources_do_not_install_client_profiles(
    tmp_path, monkeypatch, capsys
):
    from symbiosis_brain import install_cli

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("CODEX_HOME", str(home / ".codex"))
    vault = tmp_path / "vault"
    monkeypatch.setattr(sys, "argv", [
        "symbiosis-brain", "init-vault", "--vault", str(vault),
    ])
    with pytest.raises(SystemExit) as exited:
        install_cli.main()
    assert exited.value.code == 0
    assert (vault / "reference" / "scope-taxonomy.md").is_file()
    assert not (vault / ".sb-initialized").exists()
    assert not (vault / ".index" / "brain.db").exists()
    capsys.readouterr()
    monkeypatch.setattr(sys, "argv", [
        "symbiosis-brain", "resources", "--skills",
    ])
    with pytest.raises(SystemExit) as exited:
        install_cli.main()
    assert exited.value.code == 0
    skill_root = Path(capsys.readouterr().out.strip())
    assert (skill_root / "brain-init" / "SKILL.md").is_file()
    assert list(home.iterdir()) == []


@pytest.mark.skipif(os.name != "nt", reason="Windows PowerShell argv round-trip")
@pytest.mark.parametrize("vault_style", ["spaces", "drive-root", "unc-root"])
def test_windows_powershell_round_trip_preserves_supported_paths(tmp_path, vault_style):
    """Exercise the pinned native Codex Windows shell, not a cmd approximation."""
    manual = manual_module()
    work = tmp_path / "recorder"
    work.mkdir()
    # Python receives the exact hook-shaped argv. This script uses stdlib only.
    (work / "codex-hook").write_text(
        "import json,sys\nprint(json.dumps(sys.argv[1:]))\n", encoding="utf-8"
    )
    vault = {
        "spaces": tmp_path / "vault with spaces",
        "drive-root": Path("C:/"),
        "unc-root": Path("\\\\synthetic-server\\synthetic-share\\"),
    }[vault_style]
    command = manual.render_hooks(Path(sys.executable), vault)["hooks"]["SessionStart"][0]["hooks"][0]["commandWindows"]
    environment = dict(os.environ, PYTHONNOUSERSITE="1")
    environment.pop("PYTHONPATH", None)
    shell = (Path(os.environ["SystemRoot"]) / "System32" /
             "WindowsPowerShell" / "v1.0" / "powershell.exe")
    result = subprocess.run(
        [str(shell), "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", command],
        cwd=work, env=environment, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=10, check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout, (result.args, result.returncode, result.stderr)
    assert json.loads(result.stdout) == ["--vault", str(vault), "--event", "SessionStart"]
