"""Lightweight CLI for skills (brain-init, brain-project-init).

Subcommands:
  scope-resolve <project_path>   Resolve scope from marker or basename.
  parse-marker <claude_md_path>  Read marker fields from a CLAUDE.md file.
  acquire-onboard-lock <scope>   Try to lock onboarding for a scope.
  release-onboard-lock <scope>   Release onboarding lock.
  report                         Vault health report (see report.py).

Exit codes:
  0 — success.
  1 — expected non-success (parse-marker: no marker; acquire-onboard-lock: busy;
      report: nothing to show yet — no log, or an empty vault).
  2 — unexpected error (acquire-onboard-lock: lockdir unwritable; report: vault
      not found or an unexpected failure; unknown subcommand).

KEEP THE MODULE-LEVEL IMPORTS SMALL: brain-init runs `brain-cli scope-resolve` at
the start of EVERY session, so anything imported here is paid for by every
session. `report` and `Storage` are therefore imported inside _report_cmd (I-28).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from . import onboard_lock
from .scope_resolver import normalize_scope, parse_marker


def _override_lock_dir_from_env() -> None:
    """Honor SYMBIOSIS_BRAIN_LOCK_DIR for tests."""
    env = os.environ.get("SYMBIOSIS_BRAIN_LOCK_DIR")
    if env:
        onboard_lock.LOCK_DIR = Path(env)


def _scope_resolve(project_path: str) -> int:
    proj = Path(project_path)
    claude_md = proj / "CLAUDE.md"
    marker = parse_marker(claude_md) if claude_md.is_file() else None

    out: dict
    if marker is None:
        out = {
            "scope": normalize_scope(proj.name),
            "umbrella": None,
            "source": "hook",
            "marker_status": None,
            "marker_version": None,
        }
    elif marker.version == 1:
        out = {
            "scope": marker.scope,
            "umbrella": marker.umbrella,
            "source": "marker_v1",
            "marker_status": marker.status,
            "marker_version": 1,
        }
    else:
        out = {
            "scope": marker.scope,
            "umbrella": marker.umbrella,
            "source": "marker_future",
            "marker_status": marker.status,
            "marker_version": marker.version,
        }
    print(json.dumps(out))
    return 0


def _parse_marker_cmd(claude_md_path: str) -> int:
    m = parse_marker(claude_md_path)
    if m is None:
        return 1
    print(json.dumps({
        "version": m.version,
        "scope": m.scope,
        "umbrella": m.umbrella,
        "status": m.status,
    }))
    return 0


def _acquire(scope: str, timeout_s: int = 30) -> int:
    _override_lock_dir_from_env()
    try:
        if onboard_lock.acquire_lock(scope, timeout_s=timeout_s):
            sys.stderr.write(f"acquired:{scope}\n")
            return 0
    except OSError as e:
        sys.stderr.write(f"error:lockdir-unwritable:{scope}:{e}\n")
        return 2
    sys.stderr.write(f"busy:{scope}\n")
    return 1


def _release(scope: str) -> int:
    _override_lock_dir_from_env()
    onboard_lock.release_lock(scope)
    return 0


def _force_utf8_streams() -> None:
    """The report prints Cyrillic; a piped stdout on Windows defaults to CP1251 and
    dies with UnicodeEncodeError on «…». Same guard install_cli.py:21-28 applies at
    import — done lazily here so the session-hot `scope-resolve` path is untouched."""
    for stream in (sys.stdout, sys.stderr):
        enc = getattr(stream, "encoding", None)
        if enc and enc.lower() not in ("utf-8", "utf8"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except (AttributeError, OSError):
                pass


def _report_cmd(args) -> int:
    """`brain-cli report` — vault health report (I-28).

    `report` and `Storage` are imported HERE, not at module level: see the module
    docstring. Exit codes: 0 — printed; 1 — expected emptiness (the honest line is
    still printed, §6.3); 2 — vault not found or an unexpected failure.
    """
    _force_utf8_streams()
    vault_arg = args.vault or os.environ.get("SYMBIOSIS_BRAIN_VAULT")
    if not vault_arg:
        sys.stderr.write("error:no-vault: pass --vault PATH or set SYMBIOSIS_BRAIN_VAULT\n")
        return 2
    vault = Path(vault_arg).expanduser()
    if not vault.is_dir():
        sys.stderr.write(f"error:vault-not-found:{vault}\n")
        return 2

    db_path = vault / ".index" / "brain.db"
    if not db_path.exists():
        # Storage() would CREATE the file (storage.py:13-14); a read-only report
        # must not seed a database in a folder that has never run `serve`.
        print(f"Журнал выдачи пуст: базы {db_path} ещё нет.")
        return 1

    from symbiosis_brain import report as report_mod
    from symbiosis_brain.storage import Storage

    storage = None
    try:
        storage = Storage(db_path)
        data = report_mod.build_report(
            storage, vault,
            days=args.days,
            scope=args.scope,
            top=None if args.full else args.top,
        )
        if args.json:
            print(json.dumps(data, ensure_ascii=False, indent=2))
        else:
            print(report_mod.render_report(data, full=args.full))
        empty = data["coverage"]["events"] == 0 or data["summary"]["total_notes"] == 0
        return 1 if empty else 0
    except Exception as e:                    # noqa: BLE001 — exit code 2 is the contract
        sys.stderr.write(f"error:report-failed:{e}\n")
        return 2
    finally:
        if storage is not None:
            storage.close()


def main() -> int:
    parser = argparse.ArgumentParser(prog="brain-cli")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p1 = sub.add_parser("scope-resolve")
    p1.add_argument("project_path")
    p2 = sub.add_parser("parse-marker")
    p2.add_argument("claude_md_path")
    p3 = sub.add_parser("acquire-onboard-lock")
    p3.add_argument("scope")
    p3.add_argument("--timeout-s", type=int, default=30,
                    help="Stale-lock threshold in seconds (default: 30)")
    p4 = sub.add_parser("release-onboard-lock")
    p4.add_argument("scope")
    p5 = sub.add_parser("report", help="Vault health report")
    p5.add_argument("--vault", default=None,
                    help="Vault path (default: $SYMBIOSIS_BRAIN_VAULT)")
    p5.add_argument("--days", type=int, default=30,
                    help="Analysis window in days (default: 30)")
    p5.add_argument("--scope", default=None, help="Limit to one project scope")
    p5.add_argument("--top", type=int, default=10,
                    help="Rows per section (default: 10)")
    p5.add_argument("--full", action="store_true",
                    help="Print full lists and drop the line cap")
    p5.add_argument("--json", action="store_true",
                    help="Print build_report() as JSON instead of text")
    args = parser.parse_args()
    if args.cmd == "scope-resolve":
        return _scope_resolve(args.project_path)
    if args.cmd == "parse-marker":
        return _parse_marker_cmd(args.claude_md_path)
    if args.cmd == "acquire-onboard-lock":
        return _acquire(args.scope, args.timeout_s)
    if args.cmd == "release-onboard-lock":
        return _release(args.scope)
    if args.cmd == "report":
        return _report_cmd(args)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
