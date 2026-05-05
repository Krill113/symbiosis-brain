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

    def staleness_days(self, note: dict) -> float:
        updated = note.get("updated_at") or note.get("created_at")
        if not updated:
            return 0
        try:
            dt = datetime.fromisoformat(updated)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            delta = datetime.now(timezone.utc) - dt
            return delta.total_seconds() / 86400
        except (ValueError, TypeError):
            return 0

    def staleness_warning(self, note: dict) -> str | None:
        days = self.staleness_days(note)
        threshold = STALENESS_THRESHOLDS.get(note.get("note_type", "wiki"), 180)
        if days < threshold:
            return None
        if days < 60:
            return f"This {note['note_type']} is {int(days)} days old — consider verifying"
        months = int(days / 30)
        return f"This {note['note_type']} is ~{months} months old (threshold: {threshold} days) — may be outdated"

    def is_superseded(self, note: dict) -> bool:
        return note.get("valid_to") is not None
