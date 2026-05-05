"""Lightweight CLI for skills (brain-init, brain-project-init).

Subcommands:
  scope-resolve <project_path>   Resolve scope from marker or basename.
  parse-marker <claude_md_path>  Read marker fields from a CLAUDE.md file.
  acquire-onboard-lock <scope>   Try to lock onboarding for a scope.
  release-onboard-lock <scope>   Release onboarding lock.

Exit codes:
  0 — success.
  1 — expected non-success (parse-marker: no marker; acquire-onboard-lock: busy).
  2 — unexpected error (acquire-onboard-lock: lockdir unwritable; unknown subcommand).
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
    args = parser.parse_args()
    if args.cmd == "scope-resolve":
        return _scope_resolve(args.project_path)
    if args.cmd == "parse-marker":
        return _parse_marker_cmd(args.claude_md_path)
    if args.cmd == "acquire-onboard-lock":
        return _acquire(args.scope, args.timeout_s)
    if args.cmd == "release-onboard-lock":
        return _release(args.scope)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
