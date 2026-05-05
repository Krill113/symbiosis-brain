"""One-off migration: append <!-- symbiosis-brain v1 --> markers to existing
projects' CLAUDE.md files based on vault projects/<scope>.md frontmatter.

Usage:
    uv run python scripts/migrate_claude_md_markers.py \\
        --vault /path/to/vault \\
        --map scope_to_path.json
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Optional

# Ensure package import when running from repo root.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import frontmatter

from symbiosis_brain.scope_resolver import parse_marker


def _load_card_meta(card_path: Path) -> tuple[Optional[str], Optional[str]]:
    """Returns (scope, umbrella)."""
    fm = frontmatter.load(card_path)
    scope = fm.get("scope")
    umbrella = fm.get("umbrella") or None
    return scope, umbrella


def _format_marker(scope: str, umbrella: Optional[str]) -> str:
    parts = [f"scope={scope}"]
    if umbrella:
        parts.append(f"umbrella={umbrella}")
    return f"<!-- symbiosis-brain v1: {', '.join(parts)} -->"


def _append_marker(claude_md: Path, project_basename: str, scope: str, umbrella: Optional[str]) -> bool:
    """Returns True if a marker was written, False if already present."""
    if claude_md.exists():
        if parse_marker(claude_md) is not None:
            return False
        body = claude_md.read_text(encoding="utf-8")
        if not body.endswith("\n"):
            body += "\n"
        body += f"\n{_format_marker(scope, umbrella)}\n"
    else:
        body = f"# {project_basename}\n\n{_format_marker(scope, umbrella)}\n"
    claude_md.write_text(body, encoding="utf-8")
    return True


def migrate(vault: Path, scope_to_path: dict[str, str]) -> dict[str, list[str]]:
    written: list[str] = []
    skipped_existing: list[str] = []
    skipped_no_path: list[str] = []
    projects_dir = vault / "projects"
    if not projects_dir.is_dir():
        raise FileNotFoundError(f"no projects/ in vault {vault}")
    for card in sorted(projects_dir.glob("*.md")):
        try:
            scope, umbrella = _load_card_meta(card)
        except Exception:
            continue
        if not scope:
            continue
        proj_path_str = scope_to_path.get(scope)
        if not proj_path_str:
            skipped_no_path.append(scope)
            continue
        proj_path = Path(proj_path_str)
        proj_path.mkdir(parents=True, exist_ok=True)
        claude_md = proj_path / "CLAUDE.md"
        wrote = _append_marker(claude_md, proj_path.name, scope, umbrella)
        (written if wrote else skipped_existing).append(scope)
    return {
        "written": written,
        "skipped_existing": skipped_existing,
        "skipped_no_path": skipped_no_path,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--vault", type=Path, required=True)
    parser.add_argument("--map", type=Path, required=True,
                        help="JSON file mapping scope -> project filesystem path")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    scope_to_path = json.loads(args.map.read_text(encoding="utf-8"))

    if args.dry_run:
        print("DRY RUN — no files will be modified")
        return 0

    result = migrate(args.vault, scope_to_path)
    print(f"Written:      {result['written']}")
    print(f"Skipped (already had marker): {result['skipped_existing']}")
    print(f"Skipped (no path provided):   {result['skipped_no_path']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
