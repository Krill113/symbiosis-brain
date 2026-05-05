import json
from pathlib import Path
from symbiosis_brain import install_lib


def test_backup_creates_timestamped_copy(tmp_path):
    target = tmp_path / "settings.json"
    target.write_text('{"foo": 1}', encoding="utf-8")
    backup = install_lib.backup_file(target)
    assert backup.exists()
    assert backup.name.startswith("settings.json.bak.")
    assert backup.read_text(encoding="utf-8") == '{"foo": 1}'


def test_backup_skips_missing_file(tmp_path):
    target = tmp_path / "missing.json"
    assert install_lib.backup_file(target) is None


def test_deep_merge_combines_nested_dicts():
    base = {"a": {"x": 1}, "b": [1, 2]}
    overlay = {"a": {"y": 2}, "b": [3]}
    result = install_lib.deep_merge(base, overlay)
    assert result == {"a": {"x": 1, "y": 2}, "b": [3]}


def test_deep_merge_extends_lists_when_marked():
    base = {"permissions": {"allow": ["a"]}}
    overlay = {"permissions": {"allow": ["b", "c"]}}
    result = install_lib.deep_merge(base, overlay, list_extend_keys={"allow"})
    assert result["permissions"]["allow"] == ["a", "b", "c"]


def test_atomic_write_json_roundtrips(tmp_path):
    target = tmp_path / "out.json"
    install_lib.atomic_write_json(target, {"a": 1, "b": [2]})
    assert json.loads(target.read_text(encoding="utf-8")) == {"a": 1, "b": [2]}


def test_has_marker_returns_true_when_present(tmp_path):
    f = tmp_path / "claude.md"
    f.write_text("hello\n<!-- symbiosis-brain v1: global -->\n", encoding="utf-8")
    assert install_lib.has_marker(f, "symbiosis-brain v1: global")


def test_has_marker_returns_false_when_absent(tmp_path):
    f = tmp_path / "claude.md"
    f.write_text("hello\n", encoding="utf-8")
    assert not install_lib.has_marker(f, "symbiosis-brain v1: global")


def test_scaffold_vault_creates_structure(tmp_path):
    vault = tmp_path / "v"
    install_lib.scaffold_vault(vault)
    for d in ("projects", "wiki", "decisions", "patterns", "mistakes",
              "feedback", "research", "reference"):
        assert (vault / d).is_dir()
    assert (vault / "README.md").exists()
    assert (vault / "reference" / "scope-taxonomy.md").exists()
    assert (vault / "MEMORY.md").exists()


def test_scaffold_vault_idempotent_preserves_existing_content(tmp_path):
    vault = tmp_path / "v"
    install_lib.scaffold_vault(vault)
    # User adds content
    (vault / "projects" / "foo.md").write_text("# Foo", encoding="utf-8")
    custom_readme = "# my custom readme"
    (vault / "README.md").write_text(custom_readme, encoding="utf-8")

    install_lib.scaffold_vault(vault)  # second call
    assert (vault / "projects" / "foo.md").read_text() == "# Foo"
    assert (vault / "README.md").read_text() == custom_readme


def test_merge_settings_writes_full_block_in_empty_settings(tmp_path):
    settings = tmp_path / "settings.json"
    install_lib.atomic_write_json(settings, {})
    install_lib.merge_settings_json(
        settings,
        hook_dir="~/.claude/hooks",
        statusline_cmd="bash ~/.claude/hooks/sb-statusline.sh",
        permissions=["mcp__symbiosis-brain__brain_read"],
    )
    data = json.loads(settings.read_text())
    assert data["statusLine"]["command"] == "bash ~/.claude/hooks/sb-statusline.sh"
    assert "SessionStart" in data["hooks"]
    assert "Stop" in data["hooks"]
    assert "PreCompact" in data["hooks"]
    assert "UserPromptSubmit" in data["hooks"]
    assert "mcp__symbiosis-brain__brain_read" in data["permissions"]["allow"]


def test_merge_settings_preserves_user_statusline_in_env(tmp_path):
    settings = tmp_path / "settings.json"
    install_lib.atomic_write_json(settings, {
        "statusLine": {"type": "command", "command": "bash ~/my-status.sh"},
    })
    install_lib.merge_settings_json(
        settings,
        hook_dir="~/.claude/hooks",
        statusline_cmd="bash ~/.claude/hooks/sb-statusline.sh",
        permissions=[],
    )
    data = json.loads(settings.read_text())
    assert data["env"]["SYMBIOSIS_BRAIN_USER_STATUSLINE_CMD"] == "bash ~/my-status.sh"


def test_merge_settings_idempotent(tmp_path):
    settings = tmp_path / "settings.json"
    install_lib.atomic_write_json(settings, {})
    install_lib.merge_settings_json(
        settings, hook_dir="~/.claude/hooks",
        statusline_cmd="bash ~/.claude/hooks/sb-statusline.sh",
        permissions=["mcp__sb__a"],
    )
    install_lib.merge_settings_json(
        settings, hook_dir="~/.claude/hooks",
        statusline_cmd="bash ~/.claude/hooks/sb-statusline.sh",
        permissions=["mcp__sb__a"],
    )
    data = json.loads(settings.read_text())
    # Permissions list is deduplicated
    assert data["permissions"]["allow"].count("mcp__sb__a") == 1


def test_merge_settings_does_not_backup_caller_owns_it(tmp_path):
    """merge_settings_json no longer backs up — caller (cmd_setup) is the single owner."""
    settings = tmp_path / "settings.json"
    install_lib.atomic_write_json(settings, {"foo": "bar"})
    install_lib.merge_settings_json(
        settings, hook_dir="~/.claude/hooks",
        statusline_cmd="bash ~/.claude/hooks/sb-statusline.sh",
        permissions=[],
    )
    backups = list(tmp_path.glob("settings.json.bak.*"))
    assert len(backups) == 0


def test_append_claude_md_block_creates_file_when_missing(tmp_path):
    target = tmp_path / "CLAUDE.md"
    install_lib.append_claude_md_block(target)
    content = target.read_text(encoding="utf-8")
    assert "# Global Rules" in content
    assert "Symbiosis Brain" in content
    assert "<!-- symbiosis-brain v1: global -->" in content


def test_append_claude_md_block_appends_when_marker_absent(tmp_path):
    target = tmp_path / "CLAUDE.md"
    target.write_text("# Global Rules\n\nMy own rules\n", encoding="utf-8")
    install_lib.append_claude_md_block(target)
    content = target.read_text(encoding="utf-8")
    assert "My own rules" in content
    assert "<!-- symbiosis-brain v1: global -->" in content


def test_append_claude_md_block_idempotent_when_marker_present(tmp_path):
    target = tmp_path / "CLAUDE.md"
    install_lib.append_claude_md_block(target)
    first = target.read_text(encoding="utf-8")
    install_lib.append_claude_md_block(target)
    second = target.read_text(encoding="utf-8")
    assert first == second
