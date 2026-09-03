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

    def is_superseded(self, note: dict) -> bool:
        return note.get("valid_to") is not None
