"""W4 migration: rewrite frontmatter `type:` to match folder convention.

Dry-run by default. Pass --apply to write changes.
Respects `allow_type_mismatch: true` escape hatch.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

# Ensure package import when running from repo root.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from symbiosis_brain.taxonomy import load_folder_type_map

FRONTMATTER_RE = re.compile(r"^---\r?\n(.*?)\r?\n---\r?\n", re.DOTALL)
TYPE_LINE_RE = re.compile(r"^(type\s*:\s*)(\S.*?)\s*$", re.MULTILINE)
ALLOW_LINE_RE = re.compile(r"^allow_type_mismatch\s*:\s*true\s*$", re.MULTILINE | re.IGNORECASE)


def migrate(vault_path: Path, apply: bool) -> int:
    folder_type_map = load_folder_type_map(vault_path)
    changes: list[tuple[Path, str, str]] = []  # (file, old_type, new_type)

    for md_file in vault_path.rglob("*.md"):
        rel = md_file.relative_to(vault_path)
        parts = rel.parts
        if len(parts) < 2:  # root-level note
            continue
        folder = parts[0]
        expected_type = folder_type_map.get(folder)
        if not expected_type:
            continue  # folder not in taxonomy (e.g. .index/, log.md path)

        text = md_file.read_text(encoding="utf-8")
        m = FRONTMATTER_RE.match(text)
        if not m:
            continue
        fm_body = m.group(1)

        if ALLOW_LINE_RE.search(fm_body):
            continue  # escape hatch honored

        type_m = TYPE_LINE_RE.search(fm_body)
        if not type_m:
            continue
        actual_type = type_m.group(2).strip().strip("\"'")
        if actual_type == expected_type:
            continue

        new_fm_body = TYPE_LINE_RE.sub(
            lambda m2, nt=expected_type: f"{m2.group(1)}{nt}",
            fm_body,
            count=1,
        )
        new_text = f"---\n{new_fm_body}\n---\n" + text[m.end():]
        changes.append((md_file, actual_type, expected_type))

        if apply:
            md_file.write_text(new_text, encoding="utf-8")

    for md_file, old, new in changes:
        print(f"{'APPLIED' if apply else 'WOULD'}: {md_file.relative_to(vault_path)} : {old} -> {new}")
    print(f"\nTotal: {len(changes)} note(s). Apply: {apply}")
    return len(changes)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--vault", required=True, type=Path)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    migrate(args.vault.resolve(), apply=args.apply)


if __name__ == "__main__":
    main()
