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
    monkeypatch.setattr(install_cli, "_hook_dir_str", lambda: str(tmp_path / "hooks"))
    monkeypatch.setattr(install_cli, "_skill_dir", lambda: tmp_path / "skills")
    # Skip subprocess steps (MCP + copies) for this slice
    monkeypatch.setattr(install_cli, "_register_mcp", lambda *a, **kw: None)
    monkeypatch.setattr(install_cli, "_copy_skills", lambda *a, **kw: [])
    monkeypatch.setattr(install_cli, "_copy_hooks", lambda *a, **kw: [])

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
    monkeypatch.setattr(install_cli, "_hook_dir_str", lambda: str(tmp_path / "hooks"))
    monkeypatch.setattr(install_cli, "_skill_dir", lambda: tmp_path / "skills")
    monkeypatch.setattr(install_cli, "_register_mcp", lambda *a, **kw: None)
    monkeypatch.setattr(install_cli, "_copy_skills", lambda *a, **kw: [])
    monkeypatch.setattr(install_cli, "_copy_hooks", lambda *a, **kw: [])

    args = type("A", (), {"vault": str(vault), "repair": False, "target": "claude-code"})()
    install_cli.cmd_setup(args)
    install_cli.cmd_setup(args)

    # No duplicate marker block
    text = claude_md.read_text(encoding="utf-8")
    assert text.count("<!-- symbiosis-brain v1: global -->") == 1


@pytest.mark.parametrize(
    "missing_skills, missing_hooks",
    [(["brain-init"], []), ([], ["sb-statusline.sh"])],
    ids=["missing-skill", "missing-hook"],
)
def test_setup_aborts_and_rolls_back_when_package_files_are_missing(
    tmp_path, monkeypatch, missing_skills, missing_hooks
):
    """A package that did not ship its skills/hooks must abort BEFORE the MCP server
    is registered, so the rollback restores settings.json and CLAUDE.md instead of a
    final "done" being printed over a half-installed state.

    Both halves of the guard are exercised: with only the skills case, mutating
    `missing_skills or missing_hooks` down to `missing_skills` kept the whole suite
    green, so the hooks half was pinned by nothing."""
    vault = tmp_path / "vault"
    settings = tmp_path / "settings.json"
    claude_md = tmp_path / "CLAUDE.md"
    settings.write_text('{"env": {"KEEP": "me"}}', encoding="utf-8")
    claude_md.write_text("# Global Rules\n", encoding="utf-8")

    monkeypatch.setattr(install_cli, "_settings_path", lambda: settings)
    monkeypatch.setattr(install_cli, "_claude_md_path", lambda: claude_md)
    monkeypatch.setattr(install_cli, "_hook_dir_str", lambda: str(tmp_path / "hooks"))
    monkeypatch.setattr(install_cli, "_skill_dir", lambda: tmp_path / "skills")
    monkeypatch.setattr(install_cli, "_copy_skills", lambda *a, **kw: list(missing_skills))
    monkeypatch.setattr(install_cli, "_copy_hooks", lambda *a, **kw: list(missing_hooks))

    registered: list[int] = []
    monkeypatch.setattr(install_cli, "_register_mcp", lambda *a, **kw: registered.append(1))

    args = type("A", (), {"vault": str(vault), "repair": False, "target": "claude-code"})()
    with pytest.raises(SystemExit) as exc:
        install_cli.cmd_setup(args)

    assert exc.value.code == 1
    assert registered == [], "MCP must not be registered when the package is incomplete"
    assert json.loads(settings.read_text(encoding="utf-8")) == {"env": {"KEEP": "me"}}
    assert "<!-- symbiosis-brain v1: global -->" not in claude_md.read_text(encoding="utf-8")


def test_sb_permissions_cover_every_mcp_tool():
    """Three tools shipped without a permission entry because nothing tied this list
    to the server's tool set, and `doctor` is no safety net here: it only asserts
    len(sb_perms) >= 7, which passed with the incomplete list just as well."""
    import asyncio

    from symbiosis_brain import server

    served = {t.name for t in asyncio.run(server.list_tools())}
    granted = {p.rsplit("__", 1)[1] for p in install_cli.SB_PERMISSIONS}
    assert served == granted, (
        f"tools without a permission: {sorted(served - granted)}; "
        f"permissions for no tool: {sorted(granted - served)}"
    )


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
    for h in install_cli.HOOK_FILES_SH:
        (src / h).write_text(f"# {h}\n", encoding="utf-8")
    monkeypatch.setattr(install_cli, "_packaged_hooks_dir", lambda: src)

    target = tmp_path / "claude_hooks"
    install_cli._copy_hooks(target)

    for h in install_cli.HOOK_FILES_SH:
        assert (target / h).exists()


def test_setup_rollback_restores_settings_on_failure(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    settings = tmp_path / "settings.json"
    claude_md = tmp_path / "CLAUDE.md"
    install_lib.atomic_write_json(settings, {"existing": "data"})
    claude_md.write_text("# original\n", encoding="utf-8")

    monkeypatch.setattr(install_cli, "_settings_path", lambda: settings)
    monkeypatch.setattr(install_cli, "_claude_md_path", lambda: claude_md)
    monkeypatch.setattr(install_cli, "_hook_dir_str", lambda: str(tmp_path / "hooks"))
    monkeypatch.setattr(install_cli, "_skill_dir", lambda: tmp_path / "skills")

    def explode(*a, **kw):
        raise RuntimeError("simulated MCP failure")
    monkeypatch.setattr(install_cli, "_register_mcp", explode)
    monkeypatch.setattr(install_cli, "_copy_skills", lambda *a, **kw: [])
    monkeypatch.setattr(install_cli, "_copy_hooks", lambda *a, **kw: [])

    args = type("A", (), {"vault": str(vault), "repair": False, "target": "claude-code"})()
    try:
        install_cli.cmd_setup(args)
    except SystemExit:
        pass  # expected

    # settings.json and CLAUDE.md restored from backup
    assert json.loads(settings.read_text())["existing"] == "data"
    assert claude_md.read_text(encoding="utf-8") == "# original\n"


def test_brain_tools_skill_registered_and_packaged():
    from symbiosis_brain import install_cli
    assert "brain-tools" in install_cli.SKILL_NAMES
    src = install_cli._packaged_skills_dir() / "brain-tools" / "SKILL.md"
    assert src.exists(), f"missing packaged skill: {src}"
    text = src.read_text(encoding="utf-8")
    assert text.startswith("---")
    assert "name: brain-tools" in text

def test_setup_copies_brain_tools_skill(tmp_path, monkeypatch):
    from symbiosis_brain import install_cli
    target = tmp_path / "skills"
    install_cli._copy_skills(target)
    assert (target / "brain-tools" / "SKILL.md").exists()

def test_skill_frontmatter_name_matches_dir():
    import re
    from symbiosis_brain import install_cli
    src = install_cli._packaged_skills_dir() / "brain-tools" / "SKILL.md"
    head = src.read_text(encoding="utf-8").split("---")[1]
    assert re.search(r"^name:\s*brain-tools\s*$", head, re.M)


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
