from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from symbiosis_brain.storage import Storage

STALENESS_THRESHOLDS = {
    "research": 90,
    "context": 30,
    "progress": 7,
    "decision": 365,
    "wiki": 180,
    "project": 180,
}


class TemporalManager:
    def __init__(self, storage: Storage):
        self.storage = storage

    def staleness_days(self, note: dict) -> float | None:
        """How old the CLAIM is — `None` when the note does not say.

        Deliberately NOT `updated_at`/`created_at`. Those record when the row was
        last written, and every note is rewritten whenever the vault is
        re-synced; recreating the index makes each row a fresh INSERT and loses
        `created_at` as well. Measured on the live vault: 4 distinct `updated_at`
        values across 1632 notes, 1554 of them stamped with the day the embedder
        was migrated — and `staleness_warning` consequently silent on all 1635.

        `valid_from` survives because it lives in the note's own frontmatter, not
        in a column: 109 distinct days over the same vault. Some of them predate
        the vault's own git history, because authors date the claim rather than
        the file — which is exactly the thing worth measuring.

        `None` rather than 0 for an undated note: 0 reads as "written moments
        ago" and would silence the warning on precisely the notes nobody has
        dated, in a way indistinguishable from a healthy vault. `brain_lint`
        reports how many notes are in that state.
        """
        dated = note.get("valid_from")
        if not dated:
            return None
        try:
            dt = datetime.fromisoformat(str(dated))
        except (ValueError, TypeError):
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - dt).total_seconds() / 86400

    def staleness_warning(self, note: dict) -> str | None:
        days = self.staleness_days(note)
        if days is None:
            return None
        threshold = STALENESS_THRESHOLDS.get(note.get("note_type", "wiki"), 180)
        if days < threshold:
            return None
        if days < 60:
            return f"This {note['note_type']} is {int(days)} days old — consider verifying"
        months = int(days / 30)
        return f"This {note['note_type']} is ~{months} months old (threshold: {threshold} days) — may be outdated"

    def is_superseded(self, note: dict, *, now: datetime | None = None) -> bool:
        """Whether the note's own claim has stopped being true.

        `valid_to` is the date a claim STOPS holding, so a value still in the
        future means it holds right now. Testing `is not None` got that exactly
        backwards and would close a note deliberately marked as valid until a
        known date. The method had no production caller until now, so the
        mistake had never had a chance to show.

        An unparsable date leaves the note open. Between the two errors — a live
        note hidden by a typo, and a closed note still shown — only the first is
        silent, and it is the one that costs the reader something they cannot
        see is missing.
        """
        closed_at = note.get("valid_to")
        if not closed_at:
            return False
        try:
            dt = datetime.fromisoformat(str(closed_at))
        except (ValueError, TypeError):
            return False
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt <= (now or datetime.now(timezone.utc))
