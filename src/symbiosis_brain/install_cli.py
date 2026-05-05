"""CLI entry-point for Symbiosis Brain user-facing commands.

Subcommands:
  serve            — launch MCP server (delegates to server.main)
  setup            — install Symbiosis Brain into Claude Code
  doctor           — health-check current installation
  uninstall        — remove Symbiosis Brain (vault preserved)
  migrate-hooks    — bash → python hook cutover for legacy users
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

# Force UTF-8 on stdout/stderr — Windows defaults to CP1251 which crashes on
# argparse arrows (→), doctor checkmarks (✓/✗), and Cyrillic user-facing copy.
# Same guard as in hooks/brain-session-start.py and hooks/brain-save-trigger.py.
for _stream in (sys.stdout, sys.stderr):
    if _stream.encoding and _stream.encoding.lower() not in ("utf-8", "utf8"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError):
            pass

from symbiosis_brain import install_lib

DEFAULT_VAULT = Path.home() / "symbiosis-brain-vault"

SB_PERMISSIONS = [
    "mcp__symbiosis-brain__brain_read",
    "mcp__symbiosis-brain__brain_search",
    "mcp__symbiosis-brain__brain_write",
    "mcp__symbiosis-brain__brain_context",
    "mcp__symbiosis-brain__brain_list",
    "mcp__symbiosis-brain__brain_status",
    "mcp__symbiosis-brain__brain_sync",
    "mcp__symbiosis-brain__brain_lint",
]

PROMPT_TEXT = """
Symbiosis Brain — общая память тебя и Claude. Заметки живут
в обычной папке (markdown-файлы), которую ты можешь:
  • открыть в Obsidian для красивой визуализации
  • положить в git и синхронизировать между машинами

Где разместить папку с заметками?
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


def _resolve_vault_path() -> Path | None:
    """Read vault path from existing MCP config (claude mcp list output)."""
    try:
        proc = subprocess.run(["claude", "mcp", "list"], capture_output=True, text=True, timeout=10)
        for line in proc.stdout.splitlines():
            if "symbiosis-brain" in line and "--vault" in line:
                # naive parse: --vault <path> [other args]
                parts = line.split("--vault", 1)[1].strip().split()
                if parts:
                    return Path(parts[0])
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


SKILL_NAMES = ("brain-init", "brain-recall", "brain-save", "brain-project-init", "brain-welcome")

HOOK_FILES_PY = ("brain-session-start.py", "brain-save-trigger.py")
HOOK_FILES_SH = ("sb-statusline.sh", "sb-line.sh", "sb-base-statusline.sh")


def _packaged_skills_dir() -> Path:
    """Path to skills/ shipped with the package (development layout)."""
    return Path(__file__).parent.parent.parent / "skills"


def _packaged_hooks_dir() -> Path:
    return Path(__file__).parent.parent.parent / "hooks"


def _register_mcp(vault_path: Path) -> None:
    """Run `claude mcp add -s user symbiosis-brain ...` if not already registered."""
    try:
        listing = subprocess.run(
            ["claude", "mcp", "list"], capture_output=True, text=True, timeout=10,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        print(f"WARN: `claude mcp list` failed ({e}). Пропускаю MCP-регистрацию — "
              f"добавь вручную: claude mcp add -s user symbiosis-brain -- "
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


def _copy_skills(target_dir: Path) -> None:
    target_dir.mkdir(parents=True, exist_ok=True)
    src_root = _packaged_skills_dir()
    for name in SKILL_NAMES:
        src = src_root / name / "SKILL.md"
        if not src.exists():
            print(f"WARN: skill {name} not found in package, skipping")
            continue
        dst_dir = target_dir / name
        dst_dir.mkdir(exist_ok=True)
        dst = dst_dir / "SKILL.md"
        if dst.exists() and dst.read_text(encoding="utf-8") == src.read_text(encoding="utf-8"):
            continue  # identical, skip
        if dst.exists():
            install_lib.backup_file(dst)
        shutil.copyfile(src, dst)


def _copy_hooks(target_dir: Path) -> None:
    target_dir.mkdir(parents=True, exist_ok=True)
    src_root = _packaged_hooks_dir()
    for name in HOOK_FILES_PY + HOOK_FILES_SH:
        src = src_root / name
        if not src.exists():
            print(f"WARN: hook {name} missing in package, skipping")
            continue
        dst = target_dir / name
        if dst.exists() and dst.read_text(encoding="utf-8", errors="replace") == src.read_text(encoding="utf-8", errors="replace"):
            continue
        if dst.exists():
            install_lib.backup_file(dst)
        shutil.copyfile(src, dst)
        if name in HOOK_FILES_SH or name in HOOK_FILES_PY:
            try:
                dst.chmod(dst.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
            except OSError:
                pass  # Windows etc. — chmod is no-op


def _ask_vault_path(default: Path) -> Path:
    answer = input(PROMPT_TEXT.format(default=default)).strip()
    return Path(answer).expanduser() if answer else default


def _restore_latest_bak(target: Path) -> bool:
    backups = sorted(target.parent.glob(f"{target.name}.bak.*"))
    if not backups:
        return False
    shutil.copyfile(backups[-1], target)
    return True


def cmd_setup(args):
    if getattr(args, "vault", None):
        vault = Path(args.vault).expanduser()
    elif getattr(args, "repair", False):
        vault = _resolve_vault_path() or DEFAULT_VAULT
    else:
        vault = _ask_vault_path(DEFAULT_VAULT)

    settings = _settings_path()
    claude_md = _claude_md_path()
    skill_dir = _skill_dir()
    hook_dir = Path(_hook_dir_str().replace("~", str(Path.home())))

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
        )
        install_lib.append_claude_md_block(claude_md)

        # Track pre-existing skill/hook files BEFORE copying so we don't delete unrelated user files
        skills_pre_existing: set[Path] = set()
        hooks_pre_existing: set[Path] = set()
        for name in SKILL_NAMES:
            f = skill_dir / name / "SKILL.md"
            if f.exists():
                skills_pre_existing.add(f)
        for name in HOOK_FILES_PY + HOOK_FILES_SH:
            f = hook_dir / name
            if f.exists():
                hooks_pre_existing.add(f)

        _copy_skills(skill_dir)
        _copy_hooks(hook_dir)

        # After copy, anything new (not pre-existing) is ours to rollback
        for name in SKILL_NAMES:
            f = skill_dir / name / "SKILL.md"
            if f.exists() and f not in skills_pre_existing:
                created_files.append(f)
        for name in HOOK_FILES_PY + HOOK_FILES_SH:
            f = hook_dir / name
            if f.exists() and f not in hooks_pre_existing:
                created_files.append(f)

        _register_mcp(vault)
        mcp_registered = True
    except Exception as e:
        print(f"setup упал: {e}\nОткатываю изменения...", file=sys.stderr)

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

        print(f"Vault сохранён в {vault} (не удалён).", file=sys.stderr)
        sys.exit(1)

    print(
        f"Готово. Vault: {vault}\n"
        "Если Obsidian не найден — после рестарта Claude Code я предложу установить.\n"
        "Перезапусти Claude Code, дальше я тебя представлю."
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
    missing_hooks = [h for h in ("brain-session-start.py", "brain-save-trigger.py", "sb-statusline.sh")
                     if not (hook_dir / h).exists()]
    if not missing_hooks:
        print("✓ Hooks          OK (3/3 present)")
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

    # 5. Vault
    vault = _resolve_vault_path()
    if vault and vault.exists() and (vault / "reference" / "scope-taxonomy.md").exists():
        print(f"✓ Vault          OK ({vault})")
    else:
        print(f"✗ Vault          FAIL ({vault or 'not configured'})")
        issues += 1

    # 6. CLAUDE.md
    cm = _claude_md_path()
    if install_lib.has_marker(cm, install_lib.CLAUDE_MD_MARKER):
        print("✓ CLAUDE.md      OK (Symbiosis Brain block present)")
    else:
        print("✗ CLAUDE.md      FAIL (block missing)")
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
    for h in HOOK_FILES_PY + HOOK_FILES_SH:
        f = hook_dir / h
        if f.exists():
            f.unlink()

    # Unregister MCP
    try:
        subprocess.run(["claude", "mcp", "remove", "symbiosis-brain"],
                       capture_output=True, text=True, timeout=10)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass

    print("Symbiosis Brain удалён. Vault сохранён, можешь снести вручную.")
    return 0


BASH_TO_PY = {
    "bash ~/.claude/hooks/brain-session-start.sh": "python ~/.claude/hooks/brain-session-start.py",
    "bash ~/.claude/hooks/brain-save-trigger.sh stop": "python ~/.claude/hooks/brain-save-trigger.py stop",
    "bash ~/.claude/hooks/brain-save-trigger.sh precompact": "python ~/.claude/hooks/brain-save-trigger.py precompact",
    "bash ~/.claude/hooks/brain-save-trigger.sh prompt-check": "python ~/.claude/hooks/brain-save-trigger.py prompt-check",
}
PY_TO_BASH = {v: k for k, v in BASH_TO_PY.items()}


def _swap_hook_commands(data: dict, mapping: dict) -> dict:
    """Walk hooks structure and replace `command` strings via mapping."""
    hooks = data.get("hooks", {})
    for ev_list in hooks.values():
        for ev in ev_list:
            for h in ev.get("hooks", []):
                cmd = h.get("command")
                if cmd in mapping:
                    h["command"] = mapping[cmd]
    return data


def cmd_migrate_hooks(args) -> int:
    s = _settings_path()
    if not s.exists():
        print("settings.json missing, nothing to migrate")
        return 1
    install_lib.backup_file(s)
    data = json.loads(s.read_text(encoding="utf-8"))
    mapping = PY_TO_BASH if getattr(args, "rollback", False) else BASH_TO_PY
    data = _swap_hook_commands(data, mapping)
    install_lib.atomic_write_json(s, data)
    direction = "rolled back to bash" if args.rollback else "migrated to Python"
    print(f"Hooks {direction}. Backup: latest .bak.* in {s.parent}.")
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
    p_doctor.set_defaults(func=cmd_doctor)

    p_uninstall = sub.add_parser("uninstall", help="Remove Symbiosis Brain")
    p_uninstall.set_defaults(func=cmd_uninstall)

    p_migrate = sub.add_parser("migrate-hooks", help="bash → python hook cutover")
    p_migrate.add_argument("--rollback", action="store_true",
                           help="Restore bash hooks from .bak")
    p_migrate.set_defaults(func=cmd_migrate_hooks)

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
