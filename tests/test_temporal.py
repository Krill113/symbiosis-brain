from pathlib import Path
from symbiosis_brain.storage import Storage
from symbiosis_brain.temporal import TemporalManager


class TestStalenessDetection:
    def test_fresh_note_is_not_stale(self, db_path: Path):
        storage = Storage(db_path)
        storage.upsert_note(path="w/test.md", title="Fresh", content="new info",
                            note_type="wiki", scope="global")
        tm = TemporalManager(storage)
        note = storage.get_note("w/test.md")
        assert tm.staleness_days(note) < 1

    def test_old_note_detected_as_stale(self, db_path: Path):
        storage = Storage(db_path)
        storage.upsert_note(path="r/old.md", title="Old Research", content="findings",
                            note_type="research", scope="global")
        storage._conn.execute(
            "UPDATE notes SET created_at='2025-01-01T00:00:00+00:00', updated_at='2025-01-01T00:00:00+00:00' WHERE path='r/old.md'"
        )
        storage._conn.commit()
        tm = TemporalManager(storage)
        note = storage.get_note("r/old.md")
        assert tm.staleness_days(note) > 300

    def test_staleness_warning_for_research(self, db_path: Path):
        storage = Storage(db_path)
        storage.upsert_note(path="r/old.md", title="Old Research", content="findings",
                            note_type="research", scope="global")
        storage._conn.execute(
            "UPDATE notes SET created_at='2025-06-01T00:00:00+00:00', updated_at='2025-06-01T00:00:00+00:00' WHERE path='r/old.md'"
        )
        storage._conn.commit()
        tm = TemporalManager(storage)
        note = storage.get_note("r/old.md")
        warning = tm.staleness_warning(note)
        assert warning is not None
        assert "months" in warning.lower() or "days" in warning.lower()


class TestValidFromTo:
    def test_note_with_valid_to_is_superseded(self, db_path: Path):
        storage = Storage(db_path)
        storage.upsert_note(path="d/old.md", title="Old Decision", content="use EF Core",
                            note_type="decision", scope="beta",
                            valid_from="2024-01-01", valid_to="2025-03-15")
        tm = TemporalManager(storage)
        note = storage.get_note("d/old.md")
        assert tm.is_superseded(note)
