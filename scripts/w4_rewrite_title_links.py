"""W4 migration: rewrite title-style wikilinks to canonical path.

Resolves broken `[[Title]]` links by looking up a unique note whose title matches.
Dry-run by default. Pass --apply to write changes.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from symbiosis_brain.storage import Storage

WIKILINK_RE = re.compile(r"\[\[([^\]\|\#]+?)(?:\|([^\]]+))?\]\]")


def build_title_index(storage: Storage) -> dict[str, list[str]]:
    """Map title (case-insensitive) -> list of canonical paths."""
    index: dict[str, list[str]] = {}
    for note in storage.list_notes():
        key = note["title"].strip().lower() if note.get("title") else None
        if not key:
            continue
        index.setdefault(key, []).append(note["path"].removesuffix(".md"))
    return index


def migrate(vault_path: Path, db_path: Path, apply: bool) -> None:
    storage = Storage(db_path)
    title_index = build_title_index(storage)

    rewrites: list[tuple[Path, str, str]] = []  # (file, old_link, new_link)
    ambiguous: list[tuple[Path, str, list[str]]] = []
    unresolved: list[tuple[Path, str]] = []

    for md_file in vault_path.rglob("*.md"):
        if "/.index/" in md_file.as_posix():
            continue
        text = md_file.read_text(encoding="utf-8")
        new_text = text
        file_changes: list[tuple[str, str]] = []

        for m in WIKILINK_RE.finditer(text):
            raw_target = m.group(1).strip()
            alias = m.group(2)

            # Skip if already a path-style link (contains '/' or matches canonical path in storage).
            if "/" in raw_target:
                continue
            if any(n["path"].removesuffix(".md").endswith("/" + raw_target) or n["path"].removesuffix(".md") == raw_target for n in storage.list_notes()):
                continue  # already canonical-ish; skip defensively

            candidates = title_index.get(raw_target.lower(), [])
            if len(candidates) == 1:
                canonical = candidates[0]
                old = m.group(0)
                new = f"[[{canonical}|{alias}]]" if alias else f"[[{canonical}]]"
                file_changes.append((old, new))
            elif len(candidates) > 1:
                ambiguous.append((md_file, raw_target, candidates))
            else:
                unresolved.append((md_file, raw_target))

        if file_changes:
            for old, new in file_changes:
                new_text = new_text.replace(old, new, 1)
                rewrites.append((md_file, old, new))
            if apply:
                md_file.write_text(new_text, encoding="utf-8")

    print("=== Rewrites ===")
    for f, old, new in rewrites:
        print(f"{'APPLIED' if apply else 'WOULD'}: {f.relative_to(vault_path)} : {old} -> {new}")
    print(f"Total rewrites: {len(rewrites)}\n")

    print("=== Ambiguous (multiple title matches — skipped) ===")
    for f, title, cands in ambiguous:
        print(f"  {f.relative_to(vault_path)} : [[{title}]] -> {cands}")
    print(f"Total ambiguous: {len(ambiguous)}\n")

    print("=== Unresolved (no title match — left as-is, will stay broken) ===")
    for f, title in unresolved:
        print(f"  {f.relative_to(vault_path)} : [[{title}]]")
    print(f"Total unresolved: {len(unresolved)}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--vault", required=True, type=Path)
    parser.add_argument("--db", required=True, type=Path, help="Path to brain.db (usually <vault>/.index/brain.db)")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    migrate(args.vault.resolve(), args.db.resolve(), apply=args.apply)


if __name__ == "__main__":
    main()
