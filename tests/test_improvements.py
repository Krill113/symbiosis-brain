"""Tests for vault dirs, operation log, and consolidation trigger."""
from pathlib import Path

from symbiosis_brain.storage import Storage
from symbiosis_brain.sync import VAULT_DIRS


class TestVaultDirs:
    def test_vault_dirs_contains_all_directories(self):
        legacy = {"projects", "wiki", "research", "user", "decisions",
                  "patterns", "mistakes", "feedback", "reference", "archive"}
        # 2026-09 reorg: the scaffold also creates the scope-first global tree;
        # legacy top-level dirs stay while both layouts are valid.
        assert legacy.issubset(set(VAULT_DIRS))
        assert "global/mistakes" in VAULT_DIRS
        assert "global/patterns" in VAULT_DIRS

    def test_tmp_vault_has_all_dirs(self, tmp_vault: Path):
        for d in VAULT_DIRS:
            assert (tmp_vault / d).is_dir()


class TestOperationLog:
    def test_append_log_creates_file(self, tmp_path: Path):
        from symbiosis_brain.server import _append_log
        _append_log(tmp_path, "write", "wiki/test.md", "Test Note")
        log = tmp_path / "log.md"
        assert log.exists()
        content = log.read_text(encoding="utf-8")
        assert "wiki/test.md" in content
        assert "Test Note" in content

    def test_append_log_appends(self, tmp_path: Path):
        from symbiosis_brain.server import _append_log
        _append_log(tmp_path, "write", "wiki/a.md", "First")
        _append_log(tmp_path, "write", "wiki/b.md", "Second")
        content = (tmp_path / "log.md").read_text(encoding="utf-8")
        assert "First" in content
        assert "Second" in content

    def test_log_not_synced(self, tmp_vault: Path, db_path: Path):
        """log.md must be in SKIP_FILES and not indexed."""
        from symbiosis_brain.server import _append_log
        _append_log(tmp_vault, "write", "wiki/test.md", "Test")
        (tmp_vault / "wiki" / "real.md").write_text(
            "---\ntitle: Real\ntype: wiki\nscope: global\n---\nBody",
            encoding="utf-8",
        )
        storage = Storage(db_path)
        from symbiosis_brain.sync import VaultSync
        sync = VaultSync(tmp_vault, storage)
        sync.sync_all()
        assert storage.get_note("log.md") is None


class TestCountNotes:
    def test_count_empty(self, db_path: Path):
        storage = Storage(db_path)
        assert storage.count_notes() == 0

    def test_count_after_inserts(self, db_path: Path):
        storage = Storage(db_path)
        for i in range(3):
            storage.upsert_note(f"w/{i}.md", f"N{i}", "x", "wiki", "global")
        assert storage.count_notes() == 3

    def test_count_unchanged_on_update(self, db_path: Path):
        storage = Storage(db_path)
        storage.upsert_note("w/a.md", "V1", "old", "wiki", "global")
        storage.upsert_note("w/a.md", "V2", "new", "wiki", "global")
        assert storage.count_notes() == 1


class TestFrontmatterDateBinding:
    def test_no_deprecation_warnings_on_rotate_path(self, db_path: Path):
        """brain_rotate_handoffs writes `valid_from: <date>` UNQUOTED into every
        archive note (rotation.py:238). Re-reading that note and upserting it is
        the exact chain behind the suite's six DeprecationWarnings:
        YAML -> datetime.date -> sqlite3's default date adapter. Asserted here
        with an explicit filter so the test stands on its own, independent of
        pyproject's filterwarnings."""
        import warnings

        from symbiosis_brain.markdown_parser import parse_note

        archive_note = (
            "---\ntitle: Handoff 2026-08-25\ntype: project\nscope: demo\n"
            "gist: something shipped\n"
            "valid_from: 2026-08-25\n"
            "tags: [handoff, demo]\n"
            "---\n\n# Handoff 2026-08-25\n\nBody.\n"
        )
        storage = Storage(db_path)
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", DeprecationWarning)
                parsed = parse_note(archive_note)
                assert isinstance(parsed["valid_from"], str)
                storage.upsert_note(
                    path="archive/handoffs/demo-2026-08-25.md",
                    title=parsed["title"],
                    content=parsed["body"],
                    note_type=parsed["type"],
                    scope=parsed["scope"],
                    tags=parsed["tags"],
                    frontmatter=parsed["extra"],
                    valid_from=parsed["valid_from"],
                    valid_to=parsed["valid_to"],
                )
            row = storage.get_note("archive/handoffs/demo-2026-08-25.md")
            assert row["valid_from"] == "2026-08-25"
        finally:
            storage.close()
