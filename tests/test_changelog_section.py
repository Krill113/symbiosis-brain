"""Tests for tools/changelog_section.py — the release-notes extractor used by
the `release` job in .github/workflows/publish.yml.

The module lives in tools/ (repo tooling, not shipped in the wheel), so it is
loaded by path instead of being imported as a package.
"""
import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
_MODULE_PATH = REPO_ROOT / "tools" / "changelog_section.py"


def _load():
    spec = importlib.util.spec_from_file_location("changelog_section", _MODULE_PATH)
    assert spec and spec.loader, f"cannot load {_MODULE_PATH}"
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


changelog_section = _load()


CHANGELOG = """# Changelog

Blurb.

## [Unreleased]

### Added
- Something not released yet.

## [0.5.0] — 2026-08-11

### Fixed
- Middle section body.

## [0.4.3] — 2026-08-05

### Added
- Last section body.
"""


def test_extracts_middle_section():
    out = changelog_section.extract_section(CHANGELOG, "0.5.0")
    assert out is not None
    assert "Middle section body." in out
    assert "Last section body." not in out
    assert "Something not released yet." not in out
    assert not out.startswith("## [")


def test_extracts_last_section():
    out = changelog_section.extract_section(CHANGELOG, "0.4.3")
    assert out is not None
    assert "Last section body." in out
    assert out.strip().endswith("Last section body.")


def test_unreleased_never_matches_version():
    assert changelog_section.extract_section(CHANGELOG, "9.9.9") is None
    for version in ("0.5.0", "0.4.3"):
        body = changelog_section.extract_section(CHANGELOG, version)
        assert body is not None
        assert "Something not released yet." not in body


def test_unknown_version_exits_1(tmp_path):
    (tmp_path / "CHANGELOG.md").write_text(CHANGELOG, encoding="utf-8")
    with pytest.raises(SystemExit) as excinfo:
        changelog_section.resolve_notes("9.9.9", tmp_path)
    assert excinfo.value.code == 1


def test_release_notes_file_wins_over_changelog(tmp_path):
    (tmp_path / "CHANGELOG.md").write_text(CHANGELOG, encoding="utf-8")
    notes_dir = tmp_path / "docs" / "release-notes"
    notes_dir.mkdir(parents=True)
    (notes_dir / "v0.5.0.md").write_text(
        "## Highlights\n\nHand-written notes.\n", encoding="utf-8")

    text, source = changelog_section.resolve_notes("0.5.0", tmp_path)
    assert source == "release-notes"
    assert "Hand-written notes." in text
    assert "Middle section body." not in text


def test_changelog_is_the_fallback(tmp_path):
    """Additive (beyond 00-plan): pins the fallback branch and the tolerated
    leading `v`, which the workflow strips but a human invocation may not."""
    (tmp_path / "CHANGELOG.md").write_text(CHANGELOG, encoding="utf-8")
    text, source = changelog_section.resolve_notes("v0.5.0", tmp_path)
    assert source == "changelog"
    assert "Middle section body." in text
