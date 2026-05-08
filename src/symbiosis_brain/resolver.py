from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from symbiosis_brain.storage import Storage


def _strip_md(p: str) -> str:
    return p[:-3] if p.lower().endswith(".md") else p


def _strip_anchor(p: str) -> str:
    """Remove '#anchor' suffix from a wiki-link target. Anchors are for human
    navigation only — the lookup key is the path/basename before the '#'."""
    return p.split("#", 1)[0] if "#" in p else p


def resolve_target(target: str, storage: Storage) -> tuple[str | None, bool]:
    """Resolve a wiki-link target to canonical path (without .md extension).

    Returns (canonical_path, is_broken).
    is_broken=True if target cannot be resolved to a unique note.

    Rules:
      - empty → broken
      - strip trailing '#anchor' before matching
      - contains '/' → path-match (case-insensitive), .md-stripped
      - no '/'        → basename-match across all notes (case-insensitive);
                        unique match returns path; ambiguous/none → broken
    """
    norm = _strip_anchor(target.strip())
    if not norm:
        return None, True

    norm_nomd = _strip_md(norm)
    norm_lower = norm_nomd.lower()

    all_paths = storage.get_all_paths()

    if "/" in norm:
        for p in all_paths:
            if _strip_md(p).lower() == norm_lower:
                return _strip_md(p), False
        return None, True

    matches = [p for p in all_paths if _strip_md(p).rsplit("/", 1)[-1].lower() == norm_lower]
    if len(matches) == 1:
        return _strip_md(matches[0]), False
    return None, True
