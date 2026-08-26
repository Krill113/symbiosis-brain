"""CLI entry-point for Symbiosis Brain user-facing commands.

Subcommands:
  serve            — launch MCP server (delegates to server.main)
  setup            — install Symbiosis Brain into Claude Code
  doctor           — health-check current installation
  uninstall        — remove Symbiosis Brain (vault preserved)
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import stat
import subprocess
import sys
from pathlib import Path

# Force UTF-8 on stdout/stderr — Windows defaults to CP1251 which crashes on
# argparse arrows (→), doctor checkmarks (✓/✗), and Cyrillic user-facing copy.
for _stream in (sys.stdout, sys.stderr):
    if _stream.encoding and _stream.encoding.lower() not in ("utf-8", "utf8"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError):
            pass

from symbiosis_brain import install_lib, sqlite_health

DEFAULT_VAULT = Path.home() / "symbiosis-brain-vault"

SB_PERMISSIONS = [
    "mcp__symbiosis-brain__brain_read",
    "mcp__symbiosis-brain__brain_search",
    "mcp__symbiosis-brain__brain_write",
    "mcp__symbiosis-brain__brain_context",
    "mcp__symbiosis-brain__brain_list",
    "mcp__symbiosis-brain__brain_status",
    "mcp__symbiosis-brain__brain_sync",
    "mcp__symbiosis-brain__brain_append",
    "mcp__symbiosis-brain__brain_patch",
    "mcp__symbiosis-brain__brain_lint",
    "mcp__symbiosis-brain__brain_rename",
    "mcp__symbiosis-brain__brain_delete",
    "mcp__symbiosis-brain__brain_rotate_handoffs",
]

PROMPT_TEXT = """
Symbiosis Brain — shared memory for you and Claude. Notes live in a plain
folder of markdown files, which you can:
  • open in Obsidian for a graph view of everything you know
  • keep under git and sync between machines

Where should the notes folder live?
[default: {default}]
> """


def _settings_path() -> Path:
    return Path.home() / ".claude" / "settings.json"


def _claude_md_path() -> Path:
    return Path.home() / ".claude" / "CLAUDE.md"


def _hook_dir_str() -> str:
    return "~/.claude/hooks"


def _skill_dir() -> Path:
    return Path.home() / ".claude" / "skills"


def _hook_dir() -> Path:
    return Path.home() / ".claude" / "hooks"


def _command_dir() -> Path:
    return Path.home() / ".claude" / "commands"


def _resolve_vault_path() -> Path | None:
    """Resolve vault path with fallback chain:

    1. `SYMBIOSIS_BRAIN_VAULT` env var — set on every live install and free to read.
    2. Parse `claude mcp list` output looking for symbiosis-brain registration.
       Handles paths-with-spaces (quoted or unquoted-as-tail). Deliberately second:
       the command health-checks every MCP server (~7-10s) and starts a second
       `symbiosis-brain serve` against the live vault while doing it.
    3. `DEFAULT_VAULT` if it exists on disk.
    4. None.
    """
    import shlex

    env_vault = os.environ.get("SYMBIOSIS_BRAIN_VAULT")
    if env_vault:
        return Path(env_vault).expanduser()

    try:
        proc = subprocess.run(
            ["claude", "mcp", "list"], capture_output=True, text=True, timeout=10
        )
        for line in proc.stdout.splitlines():
            if "symbiosis-brain" not in line or "--vault" not in line:
                continue
            tail = line.split("--vault", 1)[1].strip()
            try:
                tokens = shlex.split(tail, posix=False)
            except ValueError:
                tokens = tail.split()
            if tokens:
                # First token is the vault path (quoted forms come back unstripped from shlex non-posix).
                return Path(tokens[0].strip('"').strip("'"))
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass

    if DEFAULT_VAULT.exists():
        return DEFAULT_VAULT
    return None


def _check_mcp_running() -> bool:
    """Best-effort check that the MCP server starts and responds."""
    # For v0.1 we just verify the package is importable; deep MCP-roundtrip in v0.2.
    try:
        from symbiosis_brain import server  # noqa
        return True
    except ImportError:
        return False


SKILL_NAMES = ("brain-init", "brain-recall", "brain-save", "brain-project-init",
               "brain-welcome", "brain-tools", "brain-backfill-gists", "brain-autolearn")

# Subdirectories of a skill that never leave the maintainer's machine. `evals/` holds
# real session digests; CLAUDE.md forbids shipping non-synthetic material, and a user
# has no use for someone else's transcripts.
SKILL_COPY_EXCLUDE_DIRS = ("evals",)

# Bash is the single source of truth. All shipped hooks are .sh (the Python hook
# shims were removed to kill dual-maintenance drift). brain-pre-action-trigger.sh
# also runs from the tools repo via $SYMBIOSIS_BRAIN_TOOLS, but we ship it too so a
# fresh install has it locally; brain-sync.sh backs the SessionEnd vault sync.
HOOK_FILES_SH = (
    "sb-hooklib.sh",
    "brain-session-start.sh",
    "brain-save-trigger.sh",
    "brain-save-marker.sh",
    "brain-pre-action-trigger.sh",
    "brain-sync.sh",
    "sb-statusline.sh",
    "sb-export.sh",
    "sb-line.sh",
    "sb-base-statusline.sh",
)

# Slash commands shipped with the package. /brain-sync used to exist only on the
# author's machine — hooks/README.md documented the manual sync mode and a fresh
# install had no way to reach it.
COMMAND_FILES = ("brain-sync.md",)


def _packaged_skills_dir() -> Path:
    """Path to skills/ shipped with the package (wheel force-include or dev checkout)."""
    return install_lib.packaged_dir(__file__, "skills")


def _packaged_hooks_dir() -> Path:
    """Path to hooks/ shipped with the package (wheel force-include or dev checkout)."""
    return install_lib.packaged_dir(__file__, "hooks")


def _packaged_commands_dir() -> Path:
    """Path to commands/ shipped with the package (wheel force-include or dev checkout)."""
    return install_lib.packaged_dir(__file__, "commands")


def _register_mcp(vault_path: Path) -> None:
    """Run `claude mcp add -s user symbiosis-brain ...` if not already registered."""
    try:
        listing = subprocess.run(
            ["claude", "mcp", "list"], capture_output=True, text=True, timeout=30,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        # `claude mcp list` health-checks every registered server, so 10s was simply
        # too tight and the scary wording made a harmless timeout look like a failed
        # install. Nothing is lost: on an existing install the server is already there.
        print(f"WARN: `claude mcp list` did not answer in time ({e}). "
              f"MCP registration skipped (the server is most likely already "
              f"registered). Verify with `claude mcp list`, or add it manually: "
              f"claude mcp add -s user symbiosis-brain -- "
              f"symbiosis-brain serve --vault {vault_path}")
        return

    if "symbiosis-brain" in (listing.stdout or ""):
        return  # already registered

    add = subprocess.run(
        ["claude", "mcp", "add", "-s", "user", "symbiosis-brain", "--",
         "symbiosis-brain", "serve", "--vault", str(vault_path)],
        capture_output=True, text=True, timeout=15,
    )
    if add.returncode != 0:
        raise RuntimeError(f"`claude mcp add` failed: {add.stderr}")


def _copy_skills(target_dir: Path) -> list[str]:
    """Copy shipped skills into target_dir. Returns names missing from the package.

    Copies the whole skill directory — SKILL.md plus references/** recursively —
    skipping SKILL_COPY_EXCLUDE_DIRS. Per file: identical → skip, different →
    backup_file() then overwrite. Until brain-autolearn shipped, every skill was a
    lone SKILL.md and this copied exactly that one file; its two reference files
    would have been dropped on the floor.
    """
    target_dir.mkdir(parents=True, exist_ok=True)
    src_root = _packaged_skills_dir()
    missing: list[str] = []
    for name in SKILL_NAMES:
        src_dir = src_root / name
        if not (src_dir / "SKILL.md").exists():
            print(f"WARN: skill {name} not found in package, skipping")
            missing.append(name)
            continue
        for src in sorted(src_dir.rglob("*")):
            if not src.is_file():
                continue
            rel = src.relative_to(src_dir)
            if any(part in SKILL_COPY_EXCLUDE_DIRS for part in rel.parts[:-1]):
                continue
            dst = target_dir / name / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            if dst.exists():
                if dst.read_text(encoding="utf-8", errors="replace") == \
                        src.read_text(encoding="utf-8", errors="replace"):
                    continue  # identical, skip
                install_lib.backup_file(dst)
            shutil.copyfile(src, dst)
    return missing


def _copy_hooks(target_dir: Path) -> list[str]:
    """Copy shipped hooks into target_dir. Returns names missing from the package."""
    target_dir.mkdir(parents=True, exist_ok=True)
    src_root = _packaged_hooks_dir()
    missing: list[str] = []
    for name in HOOK_FILES_SH:
        src = src_root / name
        if not src.exists():
            print(f"WARN: hook {name} missing in package, skipping")
            missing.append(name)
            continue
        dst = target_dir / name
        if dst.exists() and dst.read_text(encoding="utf-8", errors="replace") == src.read_text(encoding="utf-8", errors="replace"):
            continue
        if dst.exists():
            install_lib.backup_file(dst)
        shutil.copyfile(src, dst)
        try:
            dst.chmod(dst.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
        except OSError:
            pass  # Windows etc. — chmod is no-op
    return missing


def _copy_commands(target_dir: Path) -> list[str]:
    """Copy shipped slash commands into target_dir. Returns names missing from the package."""
    target_dir.mkdir(parents=True, exist_ok=True)
    src_root = _packaged_commands_dir()
    missing: list[str] = []
    for name in COMMAND_FILES:
        src = src_root / name
        if not src.exists():
            print(f"WARN: command {name} missing in package, skipping")
            missing.append(name)
            continue
        dst = target_dir / name
        if dst.exists() and dst.read_text(encoding="utf-8") == src.read_text(encoding="utf-8"):
            continue
        if dst.exists():
            install_lib.backup_file(dst)
        shutil.copyfile(src, dst)
    return missing


def _ask_vault_path(default: Path) -> Path:
    try:
        answer = input(PROMPT_TEXT.format(default=default)).strip()
    except EOFError:
        print(
            "\nNo interactive stdin (headless/CI) — cannot ask where the vault should live.\n"
            "Pass it explicitly: `symbiosis-brain setup claude-code --vault <path>`\n"
            "(or `--repair`, if it is already configured).",
            file=sys.stderr,
        )
        sys.exit(1)
    return Path(answer).expanduser() if answer else default


def _restore_latest_bak(target: Path) -> bool:
    backups = sorted(target.parent.glob(f"{target.name}.bak.*"))
    if not backups:
        return False
    shutil.copyfile(backups[-1], target)
    return True


def cmd_setup(args):
    repair = getattr(args, "repair", False)
    if getattr(args, "vault", None):
        vault = Path(args.vault).expanduser()
    elif repair:
        vault = _resolve_vault_path() or DEFAULT_VAULT
    else:
        vault = _ask_vault_path(DEFAULT_VAULT)

    # After the priority swap in _resolve_vault_path (env var before `claude mcp
    # list`), an inherited SYMBIOSIS_BRAIN_VAULT silently steers the install to a
    # different vault than the one already registered. Name the source so that
    # is visible instead of a quiet surprise, and refuse a --repair that would
    # otherwise scaffold a brand-new vault at a path the owner did not ask for.
    origin = ("from SYMBIOSIS_BRAIN_VAULT"
              if os.environ.get("SYMBIOSIS_BRAIN_VAULT") else "resolved")
    print(f"Vault: {vault} ({origin})")
    if repair and not vault.exists():
        print(f"ERROR: --repair points at a vault that does not exist: {vault}. "
              f"Run without --repair to create it, or unset SYMBIOSIS_BRAIN_VAULT.")
        return 1

    settings = _settings_path()
    claude_md = _claude_md_path()
    skill_dir = _skill_dir()
    hook_dir = Path(_hook_dir_str().replace("~", str(Path.home())))
    command_dir = _command_dir()

    settings_existed = settings.exists()
    claude_md_existed = claude_md.exists()
    settings_pre_backup = install_lib.backup_file(settings) if settings_existed else None
    claude_md_pre_backup = install_lib.backup_file(claude_md) if claude_md_existed else None

    # Track files we create so we can clean them up on rollback
    created_files: list[Path] = []

    mcp_registered = False

    try:
        install_lib.scaffold_vault(vault)
        install_lib.merge_settings_json(
            settings,
            hook_dir=_hook_dir_str(),
            statusline_cmd=f"bash {_hook_dir_str()}/sb-statusline.sh",
            permissions=SB_PERMISSIONS,
            vault_path=str(vault),
            tools_path=str(_packaged_hooks_dir().parent),
        )
        install_lib.append_claude_md_block(claude_md)

        # Track pre-existing skill/hook/command files BEFORE copying so we don't
        # delete unrelated user files
        skills_pre_existing: set[Path] = set()
        hooks_pre_existing: set[Path] = set()
        commands_pre_existing: set[Path] = set()
        # Whole tree, not just SKILL.md: a skill ships references/** too (brain-autolearn
        # has two), and a rollback that only knows about SKILL.md leaves those behind in
        # ~/.claude/skills/<name>/references/.
        for name in SKILL_NAMES:
            d = skill_dir / name
            if d.is_dir():
                skills_pre_existing.update(f for f in d.rglob("*") if f.is_file())
        for name in HOOK_FILES_SH:
            f = hook_dir / name
            if f.exists():
                hooks_pre_existing.add(f)
        for name in COMMAND_FILES:
            f = command_dir / name
            if f.exists():
                commands_pre_existing.add(f)

        missing_skills = _copy_skills(skill_dir)
        missing_hooks = _copy_hooks(hook_dir)
        missing_commands = _copy_commands(command_dir)

        # After copy, anything new (not pre-existing) is ours to rollback
        for name in SKILL_NAMES:
            d = skill_dir / name
            if d.is_dir():
                created_files.extend(f for f in sorted(d.rglob("*"))
                                     if f.is_file() and f not in skills_pre_existing)
        for name in HOOK_FILES_SH:
            f = hook_dir / name
            if f.exists() and f not in hooks_pre_existing:
                created_files.append(f)
        for name in COMMAND_FILES:
            f = command_dir / name
            if f.exists() and f not in commands_pre_existing:
                created_files.append(f)

        if missing_skills or missing_hooks or missing_commands:
            # Пакет не довёз часть файлов (например баг сборки wheel). Бросаем
            # ДО регистрации MCP — сработает существующий except-блок ниже:
            # восстановит settings.json/CLAUDE.md из бэкапа, удалит уже
            # скопированные наши файлы и завершится sys.exit(1) вместо «Готово».
            parts = []
            if missing_skills:
                parts.append(f"skills: {', '.join(missing_skills)}")
            if missing_hooks:
                parts.append(f"hooks: {', '.join(missing_hooks)}")
            if missing_commands:
                parts.append(f"commands: {', '.join(missing_commands)}")
            raise RuntimeError(
                "the package is missing part of the setup payload (" + "; ".join(parts) + "). "
                "This looks like a build bug — reinstall the package and re-run "
                "`symbiosis-brain setup claude-code --repair`."
            )

        _register_mcp(vault)
        mcp_registered = True

        # Stage-1 action-recall: compile action-rules.tsv now so the very
        # first PreToolUse hook after setup already has fresh rules to match
        # against, instead of waiting for the first brain_sync. Best-effort —
        # a failure here must not roll back an otherwise-successful setup.
        try:
            from symbiosis_brain.action_rules import compile_action_rules
            compile_action_rules(vault)
        except Exception:
            pass
    except Exception as e:
        print(f"setup failed: {e}\nRolling back...", file=sys.stderr)

        # Restore settings.json
        if settings_pre_backup:
            shutil.copyfile(settings_pre_backup, settings)
        elif settings.exists() and not settings_existed:
            settings.unlink()

        # Restore CLAUDE.md
        if claude_md_pre_backup:
            shutil.copyfile(claude_md_pre_backup, claude_md)
        elif claude_md.exists() and not claude_md_existed:
            claude_md.unlink()

        # Remove skill/hook files we created
        for f in created_files:
            try:
                f.unlink()
            except OSError:
                pass

        # Unregister MCP if we registered it
        if mcp_registered:
            try:
                subprocess.run(["claude", "mcp", "remove", "symbiosis-brain"],
                               capture_output=True, text=True, timeout=10)
            except (FileNotFoundError, subprocess.TimeoutExpired):
                pass

        print(f"Vault kept at {vault} (not deleted).", file=sys.stderr)
        sys.exit(1)

    print(
        f"Done. Vault: {vault}\n"
        "If Obsidian is missing, I will offer to install it after you restart Claude Code.\n"
        "Restart Claude Code and I will introduce myself."
    )


def cmd_doctor(args) -> int:
    issues = 0
    sb_perms: list[str] = []

    # 1. MCP server
    if _check_mcp_running():
        print("✓ MCP server     OK (package imports)")
    else:
        print("✗ MCP server     FAIL (cannot import symbiosis_brain)")
        issues += 1

    # 2. settings.json
    s = _settings_path()
    settings_ok = False
    if s.exists():
        try:
            data = json.loads(s.read_text(encoding="utf-8"))
            hooks = data.get("hooks", {})
            sl = (data.get("statusLine") or {}).get("command", "")
            perms = (data.get("permissions") or {}).get("allow", [])
            sb_perms = [p for p in perms if p.startswith("mcp__symbiosis-brain__")]
            settings_ok = bool(hooks.get("SessionStart")) and "sb-statusline" in sl and len(sb_perms) >= 7
        except Exception:
            pass
    if settings_ok:
        print(f"✓ Settings.json  OK (hooks + statusLine + {len(sb_perms)} permissions)")
    else:
        print("✗ Settings.json  FAIL (missing hooks/statusLine/permissions)")
        issues += 1

    # 3. Hooks
    hook_dir = _hook_dir()
    required_hooks = ("brain-session-start.sh", "brain-save-trigger.sh",
                      "brain-sync.sh", "sb-statusline.sh",
                      "sb-hooklib.sh", "sb-export.sh", "brain-save-marker.sh")
    missing_hooks = [h for h in required_hooks if not (hook_dir / h).exists()]
    if not missing_hooks:
        print(f"✓ Hooks          OK ({len(required_hooks)}/{len(required_hooks)} present)")
    else:
        print(f"✗ Hooks          MISSING: {', '.join(missing_hooks)}")
        issues += 1

    # 4. Skills
    skill_dir = _skill_dir()
    missing_skills = [s for s in SKILL_NAMES if not (skill_dir / s / "SKILL.md").exists()]
    if not missing_skills:
        print(f"✓ Skills         OK ({len(SKILL_NAMES)}/{len(SKILL_NAMES)} present)")
    else:
        print(f"✗ Skills         MISSING: {', '.join(missing_skills)}")
        issues += 1

    # 5. Slash commands
    command_dir = _command_dir()
    missing_commands = [c for c in COMMAND_FILES if not (command_dir / c).exists()]
    if not missing_commands:
        print(f"✓ Commands       OK ({len(COMMAND_FILES)}/{len(COMMAND_FILES)} present)")
    else:
        print(f"✗ Commands       MISSING: {', '.join(missing_commands)}")
        issues += 1

    # Resolved once and reused by both blocks below: _resolve_vault_path() may
    # shell out to `claude mcp list`, which is the most expensive thing doctor does.
    vault = _resolve_vault_path()

    # 6. SQLite engine (WAL-Reset detector — report only, never patch: the fix
    # ships with CPython 3.15, and patching the interpreter's sqlite3.dll or
    # swapping in APSW was rejected deliberately).
    sqlite_version = sqlite3.sqlite_version
    sqlite_note = sqlite_health.sqlite_warning(sqlite_version)
    if sqlite_note is None:
        print(f"✓ SQLite         {sqlite_version} (WAL-Reset fix present)")
    else:
        # A warning, NOT an issue: the parking is a decision, not a defect to fix here.
        print(f"⚠ SQLite         {sqlite_note}")

    # quick_check runs ALWAYS — owner decision 3 names it as part of what doctor does.
    # Measured 1.87 s on the live 24 MB vault, against the ~10 s `claude mcp list`
    # doctor already pays for above. Guarded on db_path.exists(): a fresh install that
    # has never run `serve` has no brain.db yet, and that is not corruption to report —
    # it is the same "nothing to check yet" case as vault being unconfigured below.
    if vault is not None:
        db_path = vault / ".index" / "brain.db"
        if db_path.exists():
            ok, detail = sqlite_health.quick_check(db_path)
            if ok:
                print("✓ quick_check    ok")
            else:
                print(f"✗ quick_check    {detail}")
                issues += 1

            # --deep adds the expensive one: integrity_check also walks index-vs-table
            # cross-references, which quick_check skips.
            if getattr(args, "deep", False):
                ok, detail = sqlite_health.integrity_check(db_path)
                if ok:
                    print("✓ integrity_check ok")
                else:
                    print(f"✗ integrity_check {detail}")
                    issues += 1

    # 7. Vault
    if vault and vault.exists() and (vault / "reference" / "scope-taxonomy.md").exists():
        print(f"✓ Vault          OK ({vault})")
    else:
        print(f"✗ Vault          FAIL ({vault or 'not configured'})")
        issues += 1

    # 8. CLAUDE.md
    cm = _claude_md_path()
    if install_lib.has_marker(cm, install_lib.CLAUDE_MD_MARKER):
        print("✓ CLAUDE.md      OK (Symbiosis Brain block present)")
    else:
        print("✗ CLAUDE.md      FAIL (block missing)")
        issues += 1

    # 9. Action-recall matcher — only meaningful once our PreToolUse hook is
    # actually installed. `setup claude-code` widened the matcher to include
    # PowerShell, but merge_settings_json only runs from `setup`; an install
    # that predates that change keeps its old Bash-only matcher forever
    # unless someone re-runs setup, and nothing else told them to.
    if s.exists():
        try:
            pre_tool_use = (json.loads(s.read_text(encoding="utf-8"))
                             .get("hooks", {}).get("PreToolUse", []))
        except Exception:
            pre_tool_use = []
        our_entries = [
            e for e in pre_tool_use
            if isinstance(e, dict)
            and any("brain-pre-action-trigger.sh" in (h.get("command") or "")
                    for h in e.get("hooks", []) if isinstance(h, dict))
        ]
        if our_entries:
            if any("PowerShell" in (e.get("matcher") or "") for e in our_entries):
                print("✓ Action-recall  OK (PreToolUse matcher covers PowerShell)")
            else:
                print("✗ Action-recall  STALE (PreToolUse matcher predates PowerShell support)")
                issues += 1

    print()
    if issues:
        print(f"{issues} issue(s) found. Run `symbiosis-brain setup claude-code --repair` to fix.")
        return 1
    print("All OK.")
    return 0


def cmd_uninstall(args) -> int:
    s = _settings_path()
    cm = _claude_md_path()
    skill_dir = _skill_dir()
    hook_dir = _hook_dir()

    # Restore from latest .bak
    for target in (s, cm):
        backups = sorted(target.parent.glob(f"{target.name}.bak.*"))
        if backups:
            shutil.copyfile(backups[-1], target)

    # Remove our skills
    for name in SKILL_NAMES:
        d = skill_dir / name
        if d.exists():
            shutil.rmtree(d)

    # Remove our hooks (not sb-statusline.sh — others might depend on it; but spec says clean)
    for h in HOOK_FILES_SH:
        f = hook_dir / h
        if f.exists():
            f.unlink()

    # Remove our slash commands
    for c in COMMAND_FILES:
        f = _command_dir() / c
        if f.exists():
            f.unlink()

    # Unregister MCP
    try:
        subprocess.run(["claude", "mcp", "remove", "symbiosis-brain"],
                       capture_output=True, text=True, timeout=10)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass

    print("Symbiosis Brain removed. The vault is preserved — delete it by hand "
          "if you want a clean slate.")
    return 0


def cmd_serve(args):
    # Delegate to existing MCP server entry-point
    from symbiosis_brain import server
    sys.argv = ["symbiosis-brain"] + args.passthrough
    server.main()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="symbiosis-brain")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_serve = sub.add_parser("serve", help="Launch MCP server", add_help=False)
    p_serve.add_argument("passthrough", nargs=argparse.REMAINDER,
                         help="Args forwarded to MCP server (e.g. --vault PATH)")
    p_serve.set_defaults(func=cmd_serve)

    p_setup = sub.add_parser("setup", help="Install into Claude Code")
    p_setup.add_argument("target", choices=["claude-code"])
    p_setup.add_argument("--repair", action="store_true",
                         help="Fix only broken pieces, skip interactive question")
    p_setup.add_argument("--vault", help="Override vault path (skips prompt)")
    p_setup.set_defaults(func=cmd_setup)

    p_doctor = sub.add_parser("doctor", help="Health check")
    p_doctor.add_argument("--deep", action="store_true",
                          help="also run PRAGMA integrity_check on the vault database")
    p_doctor.set_defaults(func=cmd_doctor)

    p_uninstall = sub.add_parser("uninstall", help="Remove Symbiosis Brain")
    p_uninstall.set_defaults(func=cmd_uninstall)

    return parser


def main():
    # Legacy compat: if first arg starts with `--vault`, treat the whole call as `serve <args>`.
    # Old MCP registrations call `symbiosis-brain --vault PATH`; new is `symbiosis-brain serve --vault PATH`.
    if len(sys.argv) > 1 and sys.argv[1].startswith("--vault"):
        sys.argv = [sys.argv[0], "serve", *sys.argv[1:]]

    # Special-case `serve`: pass all remaining args through to server.main()
    # without argparse validation (argparse REMAINDER doesn't capture --options in subparsers).
    if len(sys.argv) > 1 and sys.argv[1] == "serve":

        class _ServeArgs:
            passthrough = sys.argv[2:]
            func = staticmethod(cmd_serve)

        cmd_serve(_ServeArgs())
        return

    parser = build_parser()
    args = parser.parse_args()
    sys.exit(args.func(args) or 0)


if __name__ == "__main__":
    main()
