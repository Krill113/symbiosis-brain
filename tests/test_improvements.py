"""Tests for vault dirs, operation log, and consolidation trigger."""
from pathlib import Path

from symbiosis_brain.storage import Storage
from symbiosis_brain.sync import VAULT_DIRS


class TestVaultDirs:
    def test_vault_dirs_contains_all_directories(self):
        expected = {"projects", "wiki", "research", "user", "decisions",
                    "patterns", "mistakes", "feedback", "reference"}
        assert set(VAULT_DIRS) == expected

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
