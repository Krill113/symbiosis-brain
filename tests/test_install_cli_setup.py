import ast
import json
import re
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
    monkeypatch.setattr(install_cli, "_command_dir", lambda: tmp_path / "commands")
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
    monkeypatch.setattr(install_cli, "_command_dir", lambda: tmp_path / "commands")
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
    monkeypatch.setattr(install_cli, "_command_dir", lambda: tmp_path / "commands")
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
    monkeypatch.setattr(install_cli, "_command_dir", lambda: tmp_path / "commands")

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


def _user_facing_strings(node):
    """Yield (text, lineno) for string literals that end up in front of a user:
    print() arguments, the message of a raised RuntimeError, and module-level
    *_TEXT constants (PROMPT_TEXT reaches the user through input(), not print).
    Comments and docstrings are deliberately out of scope."""
    if isinstance(node, ast.Call):
        func = node.func
        name = getattr(func, "id", None) or getattr(func, "attr", None)
        if name in ("print", "RuntimeError"):
            for arg in node.args:
                for const in ast.walk(arg):
                    if isinstance(const, ast.Constant) and isinstance(const.value, str):
                        yield const.value, const.lineno
    elif isinstance(node, ast.Assign):
        for target in node.targets:
            if isinstance(target, ast.Name) and target.id.endswith("_TEXT"):
                for const in ast.walk(node.value):
                    if isinstance(const, ast.Constant) and isinstance(const.value, str):
                        yield const.value, const.lineno


def test_copy_commands_installs_slash_command(tmp_path, monkeypatch):
    """/brain-sync existed only on the owner's machine: the command file was never in
    the package, so a fresh install had the manual sync documented and unavailable
    (lens A, finding 1)."""
    src = tmp_path / "src_commands"
    src.mkdir()
    for name in install_cli.COMMAND_FILES:
        (src / name).write_text(f"# {name}\n", encoding="utf-8")
    monkeypatch.setattr(install_cli, "_packaged_commands_dir", lambda: src)

    target = tmp_path / "claude_commands"
    assert install_cli._copy_commands(target) == []
    for name in install_cli.COMMAND_FILES:
        assert (target / name).read_text(encoding="utf-8") == f"# {name}\n"


def test_copy_commands_reports_missing(tmp_path, monkeypatch):
    src = tmp_path / "empty_commands"
    src.mkdir()
    monkeypatch.setattr(install_cli, "_packaged_commands_dir", lambda: src)
    assert install_cli._copy_commands(tmp_path / "out") == list(install_cli.COMMAND_FILES)


def test_brain_sync_command_is_packaged():
    """The wheel must actually carry it (force-include in pyproject.toml)."""
    src = install_cli._packaged_commands_dir() / "brain-sync.md"
    assert src.exists(), f"missing packaged command: {src}"
    assert "brain-sync.sh manual" in src.read_text(encoding="utf-8")


def test_skill_names_covers_every_skill_dir():
    """brain-backfill-gists shipped in the repo but was absent from SKILL_NAMES, so
    --repair never refreshed it and the installed copy stayed pre-sanitisation for
    months (lens A §A5)."""
    src = install_cli._packaged_skills_dir()
    on_disk = {p.name for p in src.iterdir() if p.is_dir() and (p / "SKILL.md").exists()}
    assert on_disk == set(install_cli.SKILL_NAMES)


def test_user_facing_output_has_no_cyrillic():
    """`symbiosis-brain setup/doctor/uninstall` is the first thing a new user of
    an OSS product sees — it must speak English. Comments and docstrings stay as
    they are; only text that reaches the user is checked."""
    cyrillic = re.compile(r"[\u0400-\u04FF]")   # ASCII-only source, no literal Cyrillic
    offenders: list[str] = []
    for module in (install_cli, install_lib):
        source_path = Path(module.__file__)
        tree = ast.parse(source_path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            for text, lineno in _user_facing_strings(node):
                if cyrillic.search(text):
                    offenders.append(f"{source_path.name}:{lineno}: {text[:60]!r}")
    assert not offenders, "Cyrillic in user-facing output:\n" + "\n".join(offenders)


def test_brain_autolearn_ships_with_its_references():
    """D4: the repetition\u2192artifact skill was the owner's personal one, so a fresh
    install got a brain-save Step 0 pointing at nothing. It ships now \u2014 and it is
    the first skill with reference files, which the copier used to drop."""
    src = install_cli._packaged_skills_dir() / "brain-autolearn"
    assert (src / "SKILL.md").exists(), f"missing packaged skill: {src}"
    assert "brain-autolearn" in install_cli.SKILL_NAMES
    head = (src / "SKILL.md").read_text(encoding="utf-8").split("---")[1]
    assert re.search(r"^name:\s*brain-autolearn\s*$", head, re.M)
    for ref in ("action-rule-recipe.md", "automation-recipe.md"):
        assert (src / "references" / ref).exists(), f"missing reference: {ref}"


def test_copy_skills_copies_references_recursively(tmp_path, monkeypatch):
    """_copy_skills copied SKILL.md only; a skill with references/ arrived crippled."""
    src = tmp_path / "src_skills"
    (src / "brain-autolearn" / "references").mkdir(parents=True)
    (src / "brain-autolearn" / "SKILL.md").write_text("# skill\n", encoding="utf-8")
    (src / "brain-autolearn" / "references" / "a.md").write_text("A\n", encoding="utf-8")
    (src / "brain-autolearn" / "references" / "b.md").write_text("B\n", encoding="utf-8")
    monkeypatch.setattr(install_cli, "_packaged_skills_dir", lambda: src)
    monkeypatch.setattr(install_cli, "SKILL_NAMES", ("brain-autolearn",))

    target = tmp_path / "claude_skills"
    assert install_cli._copy_skills(target) == []

    assert (target / "brain-autolearn" / "SKILL.md").read_text(encoding="utf-8") == "# skill\n"
    assert (target / "brain-autolearn" / "references" / "a.md").read_text(encoding="utf-8") == "A\n"
    assert (target / "brain-autolearn" / "references" / "b.md").read_text(encoding="utf-8") == "B\n"


def test_copy_skills_skips_evals_dir(tmp_path, monkeypatch):
    """evals/ holds real session digests \u2014 synthetic-fixtures rule (CLAUDE.md) keeps
    them out of the repo, and nothing must ever push them into a user's ~/.claude."""
    src = tmp_path / "src_skills"
    (src / "brain-autolearn" / "evals").mkdir(parents=True)
    (src / "brain-autolearn" / "SKILL.md").write_text("# skill\n", encoding="utf-8")
    (src / "brain-autolearn" / "evals" / "digest.md").write_text("private\n", encoding="utf-8")
    monkeypatch.setattr(install_cli, "_packaged_skills_dir", lambda: src)
    monkeypatch.setattr(install_cli, "SKILL_NAMES", ("brain-autolearn",))

    target = tmp_path / "claude_skills"
    install_cli._copy_skills(target)

    assert (target / "brain-autolearn" / "SKILL.md").exists()
    assert not (target / "brain-autolearn" / "evals").exists()
    assert "evals" in install_cli.SKILL_COPY_EXCLUDE_DIRS


def test_copy_skills_backs_up_modified_user_file(tmp_path, monkeypatch):
    """Per-file semantics survive the recursive rewrite: identical file \u2014 untouched,
    modified file \u2014 .bak then overwrite. A user's edited reference is not silently lost."""
    src = tmp_path / "src_skills"
    (src / "brain-autolearn" / "references").mkdir(parents=True)
    (src / "brain-autolearn" / "SKILL.md").write_text("SAME\n", encoding="utf-8")
    (src / "brain-autolearn" / "references" / "a.md").write_text("NEW\n", encoding="utf-8")
    monkeypatch.setattr(install_cli, "_packaged_skills_dir", lambda: src)
    monkeypatch.setattr(install_cli, "SKILL_NAMES", ("brain-autolearn",))

    target = tmp_path / "claude_skills"
    (target / "brain-autolearn" / "references").mkdir(parents=True)
    (target / "brain-autolearn" / "SKILL.md").write_text("SAME\n", encoding="utf-8")
    (target / "brain-autolearn" / "references" / "a.md").write_text("OLD\n", encoding="utf-8")

    install_cli._copy_skills(target)

    assert (target / "brain-autolearn" / "references" / "a.md").read_text(encoding="utf-8") == "NEW\n"
    baks = list((target / "brain-autolearn" / "references").glob("a.md.bak.*"))
    assert len(baks) == 1
    assert baks[0].read_text(encoding="utf-8") == "OLD\n"
    # identical file must NOT produce a backup
    assert not list((target / "brain-autolearn").glob("SKILL.md.bak.*"))


def test_brain_save_step0_guards_the_optional_pass():
    """Decision 2: brain-self-critique stays personal, so Step 0 must be able to skip it."""
    text = (install_cli._packaged_skills_dir() / "brain-save" / "SKILL.md").read_text(encoding="utf-8")
    assert "brain-autolearn" in text
    critique_lines = [ln for ln in text.splitlines() if "brain-self-critique" in ln]
    assert critique_lines, "Step 0 lost the optional pass entirely"
    for line in critique_lines:
        assert "is installed" in line, line
