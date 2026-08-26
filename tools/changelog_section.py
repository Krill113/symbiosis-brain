#!/usr/bin/env python3
"""Resolve the release notes for a version tag.

Used by the `release` job in .github/workflows/publish.yml:

    python3 tools/changelog_section.py "${GITHUB_REF_NAME#v}" > notes.md

Order of preference:
  1. docs/release-notes/v<version>.md — hand-written notes prepared in the
     release PR, where they get reviewed like any other change.
  2. The `## [<version>]` section of CHANGELOG.md — dry, but never missing.

A release with dry notes beats a release with no notes, so the CHANGELOG
fallback stays. When neither source has anything the script exits 1: the job
that calls it runs after `verify`, so a red release job never un-publishes an
already-published package — it only makes the workflow red.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

_SECTION_START = re.compile(r"^## \[")


def extract_section(changelog_text: str, version: str) -> str | None:
    """Return the body of `## [<version>]`, or None when there is no such section.

    The version is matched literally, so `## [Unreleased]` can never answer a
    request for an X.Y.Z version. The body runs up to the next line that opens
    a section (`## [`), with surrounding blank lines trimmed.
    """
    header = re.compile(r"^## \[" + re.escape(version) + r"\]")
    body: list[str] = []
    collecting = False
    for line in changelog_text.splitlines():
        if collecting:
            if _SECTION_START.match(line):
                break
            body.append(line)
            continue
        if header.match(line):
            collecting = True
    if not collecting:
        return None
    return "\n".join(body).strip("\n")


def resolve_notes(version: str, repo_root: Path) -> tuple[str, str]:
    """Return (notes text, source) where source is "release-notes" or "changelog".

    Raises SystemExit(1) with a message on stderr when neither source has
    anything for this version.
    """
    version = version.strip()
    if version.startswith("v"):
        version = version[1:]
    repo_root = Path(repo_root)

    notes_file = repo_root / "docs" / "release-notes" / f"v{version}.md"
    if notes_file.is_file():
        text = notes_file.read_text(encoding="utf-8").strip("\n")
        if text.strip():
            return text + "\n", "release-notes"

    changelog = repo_root / "CHANGELOG.md"
    if changelog.is_file():
        section = extract_section(changelog.read_text(encoding="utf-8"), version)
        if section and section.strip():
            return section + "\n", "changelog"

    print(
        f"no release notes for {version}: neither docs/release-notes/v{version}.md "
        f"nor a `## [{version}]` section in CHANGELOG.md",
        file=sys.stderr,
    )
    raise SystemExit(1)


def main(argv: list[str] | None = None) -> int:
    # The notes carry em dashes and emoji; a Windows console defaulting to
    # CP1251 would crash on write.
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, OSError):
        pass

    parser = argparse.ArgumentParser(
        prog="changelog_section.py",
        description="Print the release notes for a version to stdout.",
    )
    parser.add_argument("version", help="Version without the leading v, e.g. 0.5.0")
    parser.add_argument("--repo-root", default=".", help="Repository root (default: .)")
    args = parser.parse_args(argv)

    text, source = resolve_notes(args.version, Path(args.repo_root))
    print(f"release notes source: {source}", file=sys.stderr)
    sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
