from pathlib import Path

import pytest

from symbiosis_brain.resolver import resolve_target
from symbiosis_brain.storage import Storage


@pytest.fixture
def storage_with_notes(tmp_path: Path) -> Storage:
    db = tmp_path / "test.db"
    s = Storage(db)
    now = "2026-04-20T12:00:00+00:00"
    for p in [
        "projects/alpha-seti.md",
        "projects/widgetcompare.md",
        "wiki/graphics-optimization.md",
        "projects/graphics-optimization.md",
        "user/profile.md",
    ]:
        s._conn.execute(
            "INSERT INTO notes (path, title, content, note_type, scope, tags, "
            "frontmatter, created_at, updated_at) "
            "VALUES (?, 'T', 'C', 'wiki', 'global', '[]', '{}', ?, ?)",
            (p, now, now),
        )
    s._conn.commit()
    return s


class TestResolveTarget:
    def test_path_match_exact(self, storage_with_notes):
        path, broken = resolve_target("projects/alpha-seti", storage_with_notes)
        assert path == "projects/alpha-seti"
        assert broken is False

    def test_path_match_case_insensitive(self, storage_with_notes):
        path, broken = resolve_target("Projects/Alpha-Seti", storage_with_notes)
        assert path == "projects/alpha-seti"
        assert broken is False

    def test_path_match_with_md_extension_stripped(self, storage_with_notes):
        path, broken = resolve_target("projects/alpha-seti.md", storage_with_notes)
        assert path == "projects/alpha-seti"
        assert broken is False

    def test_path_no_match_broken(self, storage_with_notes):
        path, broken = resolve_target("projects/nonexistent", storage_with_notes)
        assert path is None
        assert broken is True

    def test_basename_unique_match(self, storage_with_notes):
        path, broken = resolve_target("alpha-seti", storage_with_notes)
        assert path == "projects/alpha-seti"
        assert broken is False

    def test_basename_case_insensitive(self, storage_with_notes):
        path, broken = resolve_target("WIDGETCOMPARE", storage_with_notes)
        assert path == "projects/widgetcompare"
        assert broken is False

    def test_basename_ambiguous_is_broken(self, storage_with_notes):
        path, broken = resolve_target("graphics-optimization", storage_with_notes)
        assert path is None
        assert broken is True

    def test_basename_no_match_broken(self, storage_with_notes):
        path, broken = resolve_target("nowhere-note", storage_with_notes)
        assert path is None
        assert broken is True

    def test_empty_target_is_broken(self, storage_with_notes):
        path, broken = resolve_target("", storage_with_notes)
        assert path is None
        assert broken is True
