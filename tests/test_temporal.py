from datetime import datetime, timedelta, timezone
from pathlib import Path

from symbiosis_brain.storage import Storage
from symbiosis_brain.temporal import TemporalManager


class TestStalenessDetection:
    # The age comes from `valid_from` — the note's own claim about when it
    # became true — and no longer from the row's write stamps, so these no
    # longer poke `updated_at` through SQL to age a note.
    def test_fresh_note_is_not_stale(self, db_path: Path):
        storage = Storage(db_path)
        today = datetime.now(timezone.utc).date().isoformat()
        storage.upsert_note(path="w/test.md", title="Fresh", content="new info",
                            note_type="wiki", scope="global", valid_from=today)
        tm = TemporalManager(storage)
        note = storage.get_note("w/test.md")
        assert tm.staleness_days(note) < 1

    def test_old_note_detected_as_stale(self, db_path: Path):
        storage = Storage(db_path)
        storage.upsert_note(path="r/old.md", title="Old Research", content="findings",
                            note_type="research", scope="global",
                            valid_from="2025-01-01")
        tm = TemporalManager(storage)
        note = storage.get_note("r/old.md")
        assert tm.staleness_days(note) > 300

    def test_staleness_warning_for_research(self, db_path: Path):
        storage = Storage(db_path)
        storage.upsert_note(path="r/old.md", title="Old Research", content="findings",
                            note_type="research", scope="global",
                            valid_from="2025-06-01")
        tm = TemporalManager(storage)
        note = storage.get_note("r/old.md")
        warning = tm.staleness_warning(note)
        assert warning is not None
        assert "months" in warning.lower() or "days" in warning.lower()


class TestAgeSurvivesReindex:
    """`updated_at`/`created_at` say when the row was last written, not how old
    the claim is. A rebuild of the index rewrites both for every note at once,
    and the age they carried is gone — measured on the live vault: 4 distinct
    `updated_at` values across 1632 notes, 1554 of them stamped with the day the
    embedder was migrated, and `staleness_warning` silent on all 1635.

    The tests below do to the database exactly what a rebuild does, and require
    the age to survive it."""

    def _aged_note(self, storage: Storage, path: str, valid_from: str) -> None:
        storage.upsert_note(path=path, title="Old Research", content="findings",
                            note_type="research", scope="global",
                            valid_from=valid_from)

    def test_staleness_survives_a_full_reindex(self, db_path: Path):
        storage = Storage(db_path)
        self._aged_note(storage, "r/old.md", "2025-01-01")
        # A rebuild re-upserts every note, so `updated_at` becomes today for all
        # of them; when the database itself is recreated, `created_at` is a fresh
        # INSERT and is lost too. Both stamps are therefore worthless as an age.
        today = datetime.now(timezone.utc).isoformat()
        storage._conn.execute("UPDATE notes SET created_at=?, updated_at=?",
                              (today, today))
        storage._conn.commit()

        tm = TemporalManager(storage)
        note = storage.get_note("r/old.md")
        assert tm.staleness_days(note) > 300
        assert tm.staleness_warning(note) is not None

    def test_age_is_unknown_without_valid_from(self, db_path: Path):
        """Absent is not fresh.

        `valid_from` is empty on 282 of 1632 notes today. Returning 0 for them
        reads as "written moments ago" and silences the warning on exactly the
        notes nobody has dated — the failure would look identical to a clean
        vault. `None` says "unknown", and `brain_lint` counts how many."""
        storage = Storage(db_path)
        storage.upsert_note(path="w/undated.md", title="Undated", content="x",
                            note_type="wiki", scope="global")
        tm = TemporalManager(storage)
        note = storage.get_note("w/undated.md")
        assert tm.staleness_days(note) is None
        assert tm.staleness_warning(note) is None


class TestValidFromTo:
    def test_note_with_valid_to_is_superseded(self, db_path: Path):
        storage = Storage(db_path)
        storage.upsert_note(path="d/old.md", title="Old Decision", content="use EF Core",
                            note_type="decision", scope="beta",
                            valid_from="2024-01-01", valid_to="2025-03-15")
        tm = TemporalManager(storage)
        note = storage.get_note("d/old.md")
        assert tm.is_superseded(note)

    def test_valid_to_in_the_future_is_not_superseded_yet(self, db_path: Path):
        """`valid_to` says when the claim STOPS being true. A date still ahead
        means it holds right now, so treating any non-empty value as "closed"
        gets it exactly backwards — and would hide a note that is deliberately
        marked as valid until a known future date."""
        storage = Storage(db_path)
        future = (datetime.now(timezone.utc) + timedelta(days=90)).date().isoformat()
        storage.upsert_note(path="d/expires.md", title="Expires later",
                            content="valid until the licence renews",
                            note_type="decision", scope="beta",
                            valid_from="2026-01-01", valid_to=future)
        tm = TemporalManager(storage)
        assert not tm.is_superseded(storage.get_note("d/expires.md"))

    def test_unparsable_valid_to_leaves_the_note_open(self, db_path: Path):
        """A hand-edited date that does not parse must not close a note: hiding
        a live note because of a typo is the worse of the two errors, and it is
        the silent one."""
        storage = Storage(db_path)
        storage.upsert_note(path="d/typo.md", title="Typo", content="x",
                            note_type="decision", scope="beta")
        storage._conn.execute(
            "UPDATE notes SET valid_to='soon-ish' WHERE path='d/typo.md'")
        storage._conn.commit()
        tm = TemporalManager(storage)
        assert not tm.is_superseded(storage.get_note("d/typo.md"))
