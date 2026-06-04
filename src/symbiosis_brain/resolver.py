from __future__ import annotations

from typing import TYPE_CHECKING

import re as _re

if TYPE_CHECKING:
    from symbiosis_brain.storage import Storage


def _strip_md(p: str) -> str:
    return p[:-3] if p.lower().endswith(".md") else p


def _strip_anchor(p: str) -> str:
    """Remove '#anchor' suffix from a wiki-link target. Anchors are for human
    navigation only — the lookup key is the path/basename before the '#'."""
    return p.split("#", 1)[0] if "#" in p else p


# Cross-scope wiki-link syntax: [[scope: path]] or [[scope:path]].
# The scope prefix is human-facing (signals cross-scope intent). For lookup
# we strip it and use only the path portion. A target is treated as having a
# scope prefix iff it matches: <word>:<optional space><path-with-slash-or-extension>.
_SCOPE_PREFIX_RE = _re.compile(r"^[a-z][a-z0-9_-]*:\s?(?=\S)", _re.IGNORECASE)


def _strip_scope_prefix(p: str) -> str:
    """Remove '<scope>:' or '<scope>: ' prefix from a wiki-link target.
    Conservative: only strips when the remainder is a non-empty path-like string."""
    m = _SCOPE_PREFIX_RE.match(p)
    if not m:
        return p
    rest = p[m.end():]
    # Reject cases where stripping would leave nothing meaningful (e.g. "notes:")
    return rest if rest else p


def build_path_index(storage: Storage) -> dict:
    """Precompute path lookups so resolve_target avoids a full notes scan per call.

    Returns {"by_canonical": {lower_canonical: canonical},
             "by_basename":  {lower_basename: [canonical, ...]}}.
    Basename stays a list so the ambiguous-basename → broken rule is preserved.
    Used by the linter's hot loop (one build per lint run instead of one scan
    per link).
    """
    by_canonical: dict[str, str] = {}
    by_basename: dict[str, list[str]] = {}
    for p in storage.get_all_paths():
        c = _strip_md(p)
        # setdefault preserves first-match (matches the old linear-scan loop) on
        # the vanishingly rare case-only canonical collision.
        by_canonical.setdefault(c.lower(), c)
        base = c.rsplit("/", 1)[-1].lower()
        by_basename.setdefault(base, []).append(c)
    return {"by_canonical": by_canonical, "by_basename": by_basename}


def resolve_target(
    target: str, storage: Storage, index: dict | None = None
) -> tuple[str | None, bool]:
    """Resolve a wiki-link target to canonical path (without .md extension).

    Returns (canonical_path, is_broken).
    is_broken=True if target cannot be resolved to a unique note.

    Rules:
      - empty → broken
      - strip trailing '#anchor' before matching
      - contains '/' → path-match (case-insensitive), .md-stripped
      - no '/'        → basename-match across all notes (case-insensitive);
                        unique match returns path; ambiguous/none → broken

    Pass a prebuilt `index` (from build_path_index) to skip re-scanning all note
    paths; when None, an index is built from storage on each call (unchanged for
    existing 2-arg callers).
    """
    norm = _strip_scope_prefix(_strip_anchor(target.strip()))
    if not norm:
        return None, True

    norm_nomd = _strip_md(norm)
    norm_lower = norm_nomd.lower()

    if index is None:
        index = build_path_index(storage)

    if "/" in norm:
        hit = index["by_canonical"].get(norm_lower)
        return (hit, False) if hit is not None else (None, True)

    matches = index["by_basename"].get(norm_lower, [])
    if len(matches) == 1:
        return matches[0], False
    return None, True
