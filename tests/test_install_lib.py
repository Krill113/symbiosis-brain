import json
from pathlib import Path
from symbiosis_brain import install_lib


def test_scaffold_vault_survives_a_chmod_that_fails(tmp_path, monkeypatch, capsys):
    """Mounts without POSIX metadata (CIFS, drvfs, some FUSE/NFS) make chmod raise,
    and scaffold_vault is the first statement in cmd_setup's try — unguarded, it took
    the whole install down before anything was touched. The branch runs on POSIX only,
    so on Windows this is the only place it is exercised at all.

    The POSIX branch is faked by replacing install_lib's own `os` reference, not by
    setting os.name globally: pathlib reads os.name too, and flipping it to "posix"
    on Windows makes Path() raise UnsupportedOperation before the code under test
    ever runs. install_lib touches `os` in exactly one place, so the shim is total."""
    vault = tmp_path / "vault"

    class _PosixOs:
        name = "posix"

    monkeypatch.setattr(install_lib, "os", _PosixOs)

    def refuse(self, mode):
        raise OSError(1, "Operation not permitted")

    monkeypatch.setattr(Path, "chmod", refuse)

    install_lib.scaffold_vault(vault)

    assert (vault / "README.md").exists()
    assert (vault / "reference" / "scope-taxonomy.md").exists()
    assert "WARN" in capsys.readouterr().out


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
    # All six live hook events are wired
    for event in ("SessionStart", "Stop", "PreCompact", "UserPromptSubmit",
                  "PreToolUse", "SessionEnd"):
        assert event in data["hooks"], event
    # Bash is the single source of truth — every command invokes bash
    assert data["hooks"]["SessionStart"][0]["hooks"][0]["command"].startswith("bash ")
    assert data["hooks"]["SessionEnd"][0]["hooks"][0]["command"] == "bash ~/.claude/hooks/brain-sync.sh auto"
    # PreToolUse recall resolves via $SYMBIOSIS_BRAIN_TOOLS, not hook_dir
    assert "$SYMBIOSIS_BRAIN_TOOLS" in data["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
    # Behavioural env defaults are seeded
    assert data["env"]["SYMBIOSIS_BRAIN_SAVE_THRESHOLDS"] == "25,35,45"
    assert data["env"]["SYMBIOSIS_BRAIN_RECALL_ENABLED"] == "true"
    assert "mcp__symbiosis-brain__brain_read" in data["permissions"]["allow"]


def test_merge_settings_seeds_paths_and_does_not_clobber_user_env(tmp_path):
    """VAULT/TOOLS are seeded from the passed paths; a pre-existing user knob survives."""
    settings = tmp_path / "settings.json"
    install_lib.atomic_write_json(settings, {
        "env": {"SYMBIOSIS_BRAIN_SAVE_THRESHOLDS": "40,70,90"},  # user override
    })
    install_lib.merge_settings_json(
        settings,
        hook_dir="~/.claude/hooks",
        statusline_cmd="bash ~/.claude/hooks/sb-statusline.sh",
        permissions=[],
        vault_path="/home/u/my-vault",
        tools_path="/opt/symbiosis-brain",
    )
    env = json.loads(settings.read_text())["env"]
    assert env["SYMBIOSIS_BRAIN_VAULT"] == "/home/u/my-vault"
    assert env["SYMBIOSIS_BRAIN_TOOLS"] == "/opt/symbiosis-brain"
    # Non-clobbering: the user's tuned threshold is preserved, not reset to the default
    assert env["SYMBIOSIS_BRAIN_SAVE_THRESHOLDS"] == "40,70,90"
    # RULES_ZONES is intentionally never seeded (left to the hook fallback)
    assert "SYMBIOSIS_BRAIN_RULES_ZONES" not in env


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


def test_scaffold_vault_gitignores_local_override(tmp_path):
    from symbiosis_brain.install_lib import scaffold_vault

    vault = tmp_path / "vault"
    scaffold_vault(vault)
    gi = (vault / ".gitignore").read_text(encoding="utf-8")
    assert "tool-routing.local.json" in gi
    assert ".index/" in gi
    # Idempotent — second call must not duplicate lines
    scaffold_vault(vault)
    gi2 = (vault / ".gitignore").read_text(encoding="utf-8")
    assert gi2.count("tool-routing.local.json") == 1


def test_hooks_block_session_start_has_four_matchers():
    """The harness fires SessionStart with source ∈ {startup, resume, clear, compact, fork}
    (enum extracted from claude.exe, lens A §A1). Registering only startup+compact left
    every --resume / --continue / /clear session without CLAUDE_SESSION_ID,
    SYMBIOSIS_BRAIN_SCOPE, CRITICAL_FACTS and a fresh session bridge — the real root of
    the "save marker is never written" reports. `fork` is deliberately absent (owner
    decision 2026-08-25 §1)."""
    block = install_lib._hooks_block("~/.claude/hooks")
    matchers = [e["matcher"] for e in block["SessionStart"]]
    assert matchers == ["startup", "resume", "clear", "compact"]
    for entry in block["SessionStart"]:
        hook = entry["hooks"][0]
        assert hook["command"] == "bash ~/.claude/hooks/brain-session-start.sh"
        assert hook["timeout"] == 5


def test_hooks_block_has_post_tool_use_save_marker():
    """The last-save marker is written by a hook, not by the brain-save skill: the hook
    gets session_id from its own stdin payload, so it is correct in resumed / forked /
    two-window sessions where CLAUDE_SESSION_ID is empty (owner decision §1)."""
    block = install_lib._hooks_block("~/.claude/hooks")
    entries = block["PostToolUse"]
    assert len(entries) == 1
    tools = entries[0]["matcher"].split("|")
    assert tools == [
        "mcp__symbiosis-brain__brain_write",
        "mcp__symbiosis-brain__brain_append",
        "mcp__symbiosis-brain__brain_patch",
    ]
    hook = entries[0]["hooks"][0]
    assert hook["command"] == "bash ~/.claude/hooks/brain-save-marker.sh"
    assert hook["timeout"] == 5


def test_hooks_block_session_end_timeout_fits_git_timeouts():
    """brain-sync.sh now runs two network steps (pull --rebase, push), each capped at
    SYMBIOSIS_BRAIN_SYNC_GIT_TIMEOUT (15s), plus add/commit of a large vault. At the old
    35s budget the hook was killed mid-push on a slow network (lens A, finding 5).
    Pinned exactly, not as `>= 15 + 15 + 5`: that bound is satisfied by the old 35s
    too, so the test would have been green before the fix."""
    entry = install_lib._hooks_block("~/.claude/hooks")["SessionEnd"][0]["hooks"][0]
    assert entry["command"] == "bash ~/.claude/hooks/brain-sync.sh auto"
    assert entry["timeout"] == 40


def test_merge_keeps_foreign_post_tool_use_hooks(tmp_path):
    """--repair must not delete hooks it does not own.

    deep_merge only concatenates lists whose key is in list_extend_keys, and
    merge_settings_json passes list_extend_keys={"allow"} — so for every hook EVENT the
    overlay list simply replaces the user's list. Today that already silently drops a
    third-party entry on our six events; adding PostToolUse (the save marker) extends
    the blast radius to the busiest event in the ecosystem — formatters, linters and
    audit hooks all live on PostToolUse.
    """
    settings = tmp_path / "settings.json"
    foreign_post = {"matcher": "Edit|Write",
                    "hooks": [{"type": "command", "command": "bash ~/.mytools/format.sh"}]}
    foreign_stop = {"hooks": [{"type": "command", "command": "notify-send done"}]}
    settings.write_text(json.dumps({"hooks": {"PostToolUse": [foreign_post],
                                              "Stop": [foreign_stop]}}), encoding="utf-8")

    install_lib.merge_settings_json(settings, "~/.claude/hooks", "bash ~/.claude/hooks/sb-statusline.sh", [])
    hooks = json.loads(settings.read_text(encoding="utf-8"))["hooks"]

    assert foreign_post in hooks["PostToolUse"], "foreign PostToolUse hook was dropped"
    assert foreign_stop in hooks["Stop"], "foreign Stop hook was dropped"
    assert any("brain-save-marker.sh" in h["command"]
               for e in hooks["PostToolUse"] for h in e["hooks"])


def test_merge_replaces_our_own_stale_hook_entries(tmp_path):
    """Our own entries must be REPLACED, not accumulated: a --repair after the hook dir
    moved (or after a command string changed) has to leave exactly one of ours per
    event. Both stale entries below are ours under a path that is NOT the hook_dir we
    install to — that is why ownership is decided by the SCRIPT NAME and not by the
    install directory. It is also why the fix is a custom merge and not
    `list_extend_keys |= {...}`: that de-duplicates by equality, so every changed
    command string would pile up forever.

    No timeout assertion here on purpose — SessionEnd's budget is pinned by
    test_hooks_block_session_end_timeout_fits_git_timeouts. This case must be green
    BEFORE the fix (today's wholesale list replacement gives the same result) and green
    after it, so that it works as a guard against a wrong fix instead of as a red phase.
    """
    settings = tmp_path / "settings.json"
    stale_moved = {"hooks": [{"type": "command",
                              "command": "bash /old/path/.claude/hooks/brain-sync.sh auto",
                              "timeout": 35}]}
    stale_abs = {"hooks": [{"type": "command",
                            "command": "bash /home/u/.claude/hooks/brain-sync.sh auto"}]}
    settings.write_text(json.dumps({"hooks": {"SessionEnd": [stale_moved, stale_abs]}}),
                        encoding="utf-8")

    install_lib.merge_settings_json(settings, "~/.claude/hooks",
                                    "bash ~/.claude/hooks/sb-statusline.sh", [])
    entries = json.loads(settings.read_text(encoding="utf-8"))["hooks"]["SessionEnd"]

    assert len(entries) == 1, entries
    assert entries[0]["hooks"][0]["command"] == "bash ~/.claude/hooks/brain-sync.sh auto"


def test_merge_is_idempotent_across_repairs(tmp_path):
    """N x --repair leaves exactly one of our entries per event.

    The trap is PreToolUse: its command is
    `bash "$SYMBIOSIS_BRAIN_TOOLS/hooks/brain-pre-action-trigger.sh"` — the path comes
    from an env var, not from hook_dir. An ownership test based on the install
    directory would read our OWN entry as foreign, keep it, and append a fresh copy on
    every repair (measured: 1 -> 2 -> 3 -> 4 entries after three repairs). Two live
    copies of the recall hook mean a double recall/hint injection on every
    Edit/Write/Bash/Task and doubled rows in .index/action-rule-hits.jsonl — a silent
    breach of the CP-4, CP-5 and CP-6 acceptance criteria.

    The base below is the LIVE shape of that entry, verbatim from a real install.
    """
    settings = tmp_path / "settings.json"
    live_pre = {"matcher": "Task|Agent|Edit|Write|MultiEdit|NotebookEdit|Bash|PowerShell",
                "hooks": [{"type": "command",
                           "command": 'bash "$SYMBIOSIS_BRAIN_TOOLS/hooks/'
                                      'brain-pre-action-trigger.sh"'}]}
    settings.write_text(json.dumps({"hooks": {"PreToolUse": [live_pre]}}), encoding="utf-8")
    args = (settings, "~/.claude/hooks", "bash ~/.claude/hooks/sb-statusline.sh", [])

    install_lib.merge_settings_json(*args)
    first = json.loads(settings.read_text(encoding="utf-8"))["hooks"]
    install_lib.merge_settings_json(*args)
    install_lib.merge_settings_json(*args)
    third = json.loads(settings.read_text(encoding="utf-8"))["hooks"]

    assert len(third["PreToolUse"]) == 1, "our own PreToolUse entry was duplicated by --repair"
    assert third == first, "settings.json grew across repairs"
    assert {event: len(v) for event, v in third.items()} == {
        "SessionStart": 4, "Stop": 1, "PreCompact": 1, "UserPromptSubmit": 1,
        "PreToolUse": 1, "PostToolUse": 1, "SessionEnd": 1}


def test_init_vault_seeds_gitattributes_merge_union(tmp_path):
    """log.md is an append-only journal and is the file that conflicts on every
    multi-machine sync. `merge=union` keeps both sides (the lint dedups later); every
    other note keeps the standard merge, so a real conflict still stops the sync and
    asks the owner (owner decision A3). Idempotent — --repair runs on every upgrade."""
    vault = tmp_path / "vault"
    install_lib.scaffold_vault(vault)
    ga = (vault / ".gitattributes").read_text(encoding="utf-8")
    assert "log.md merge=union" in ga
    install_lib.scaffold_vault(vault)
    assert (vault / ".gitattributes").read_text(encoding="utf-8").count("log.md merge=union") == 1


def test_init_vault_keeps_user_gitattributes(tmp_path):
    """A vault that already has its own .gitattributes must keep it — we only append."""
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / ".gitattributes").write_text("*.png binary\n", encoding="utf-8")
    install_lib.scaffold_vault(vault)
    ga = (vault / ".gitattributes").read_text(encoding="utf-8")
    assert "*.png binary" in ga
    assert "log.md merge=union" in ga


def test_backup_file_prunes_old_baks(tmp_path):
    """`--repair` runs on every upgrade and used to leave one .bak per run forever —
    12 stale copies in a live ~/.claude/hooks by 2026-08 (finding C-N2). Keep the three
    newest; the copy we just made is one of them."""
    target = tmp_path / "settings.json"
    target.write_text("v-new", encoding="utf-8")
    for stamp in ("20200101-000001", "20200102-000002", "20200103-000003", "20200104-000004"):
        (tmp_path / f"settings.json.bak.{stamp}").write_text("old", encoding="utf-8")

    fresh = install_lib.backup_file(target)

    backups = sorted(tmp_path.glob("settings.json.bak.*"))
    assert len(backups) == 3
    assert fresh in backups
    assert fresh.read_text(encoding="utf-8") == "v-new"
    # The oldest ones went first
    assert not (tmp_path / "settings.json.bak.20200101-000001").exists()
    assert not (tmp_path / "settings.json.bak.20200102-000002").exists()


def test_backup_file_keep_is_tunable(tmp_path):
    target = tmp_path / "hook.sh"
    target.write_text("new", encoding="utf-8")
    for stamp in ("20200101-000001", "20200102-000002"):
        (tmp_path / f"hook.sh.bak.{stamp}").write_text("old", encoding="utf-8")
    install_lib.backup_file(target, keep=1)
    assert len(list(tmp_path.glob("hook.sh.bak.*"))) == 1


def test_our_hook_scripts_covers_every_hook_command():
    """OUR_HOOK_SCRIPTS is the ownership test used by _merge_hook_event, so it must name
    every script the installer itself writes into settings.json. If a future event (or a
    renamed script) lands in _hooks_block and not in the set, --repair stops recognising
    that entry as ours and starts duplicating it — this test fails the moment the two
    drift apart."""
    block = install_lib._hooks_block("~/.claude/hooks")
    for event, entries in block.items():
        for entry in entries:
            for hook in entry["hooks"]:
                assert any(name in hook["command"] for name in install_lib.OUR_HOOK_SCRIPTS), \
                    f"{event}: {hook['command']} matches no name in OUR_HOOK_SCRIPTS"
