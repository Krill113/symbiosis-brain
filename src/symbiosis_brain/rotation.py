"""Handoff rotation — see docs/superpowers/specs/2026-05-15-b2-handoff-rotation-design.md."""
from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date as Date
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

HANDOFF_HEADING_RE = re.compile(
    r"^## Handoff (\d{4}-\d{2}-\d{2})(?:[ \t]+(.+?))?[ \t]*$"
)  # used on single line — no MULTILINE flag

GIST_MAX = 140

SHIPPED_RE = re.compile(
    r"\*\*Shipped[^*\n]*?\*\*\s*(.+?)(?=\n\n|\n-\s|\n\*\*|\Z)",
    re.DOTALL,
)


@dataclass(frozen=True)
class HandoffSection:
    start: int           # char offset of '## Handoff' heading
    end: int             # exclusive — start of next ## section or len(text)
    date: Date
    suffix: Optional[str]
    body: str            # text[start:end], including heading line


def _walk_h2_outside_fences(text: str):
    """Yield (offset, line_text_no_newline) for '## '-starting lines outside fenced code blocks."""
    inside_fence = False
    offset = 0
    for line in text.splitlines(keepends=True):
        bare = line.rstrip("\n").rstrip("\r")
        if bare.lstrip().startswith("```"):
            inside_fence = not inside_fence
        elif not inside_fence and bare.startswith("## "):
            yield offset, bare
        offset += len(line)


def parse_handoff_sections(text: str) -> list[HandoffSection]:
    """Parse '## Handoff YYYY-MM-DD [suffix]' sections from markdown.

    Code-fence aware: headings inside ``` blocks are ignored.
    Malformed dates emit a warning to the logger; section is skipped.
    """
    h2_items = list(_walk_h2_outside_fences(text))
    sections: list[HandoffSection] = []
    for i, (start, line) in enumerate(h2_items):
        m = HANDOFF_HEADING_RE.match(line)
        if not m:
            continue
        end = h2_items[i + 1][0] if i + 1 < len(h2_items) else len(text)
        suffix_raw = m.group(2)
        suffix = suffix_raw.strip() if suffix_raw else None
        try:
            d = Date.fromisoformat(m.group(1))
        except ValueError:
            logger.warning("Skipping malformed handoff heading: %r", line)
            continue
        sections.append(HandoffSection(
            start=start, end=end, date=d, suffix=suffix, body=text[start:end],
        ))
    return sections


def extract_gist(section_body: str) -> str:
    """Extract gist: first phrase of '**Shipped:**', then first non-heading line, then literal."""
    m = SHIPPED_RE.search(section_body)
    raw = m.group(1).strip() if m else None
    if not raw:
        # Fallback: first non-empty, non-heading line of body
        for line in section_body.splitlines():
            stripped = line.strip()
            if stripped and not stripped.startswith("#"):
                raw = stripped
                break
    if not raw:
        return "Handoff"
    # First sentence — stop at . ; or : (followed by space or end)
    first = re.split(r"[.;:](?:\s|$)", raw, maxsplit=1)[0].strip()
    if len(first) <= GIST_MAX:
        return first
    return first[: GIST_MAX - 1] + "…"


STOP_WORDS = {
    "shipped", "done", "fix", "wip", "the", "a", "an", "is", "of", "to",
    "—", "-", "&", "|",
}
SLUG_MAX_LEN = 30


def _slugify(text: str, max_words: int = 4) -> str:
    """Kebab-case, ASCII-only, drop stop-words, ≤SLUG_MAX_LEN chars."""
    normalized = unicodedata.normalize("NFKD", text)
    ascii_text = normalized.encode("ascii", "ignore").decode("ascii")
    raw_words = re.findall(r"[a-zA-Z0-9]+", ascii_text)
    words = [w.lower() for w in raw_words if w.lower() not in STOP_WORDS]
    if not words:
        return ""
    slug = "-".join(words[:max_words])
    return slug[:SLUG_MAX_LEN].rstrip("-")


def _candidate_slug_for(section: HandoffSection) -> Optional[str]:
    """Candidate slug from suffix or first phrase of '**Shipped**'. None if neither yields."""
    if section.suffix:
        slug = _slugify(section.suffix)
        if slug:
            return slug
    m = SHIPPED_RE.search(section.body)
    if m:
        raw = m.group(1).strip()
        first = re.split(r"[.;:](?:\s|$)", raw, maxsplit=1)[0]
        slug = _slugify(first)
        if slug:
            return slug
    return None


def assign_slugs(sections: list[HandoffSection]) -> list[Optional[str]]:
    """Assign final slug per section after collision resolution within same date.

    None means filename uses '<scope>-<date>.md' (no -<slug>).
    """
    result: list[Optional[str]] = [None] * len(sections)
    by_date: dict[Date, list[int]] = {}
    for i, s in enumerate(sections):
        by_date.setdefault(s.date, []).append(i)

    for d, indices in by_date.items():
        used_slugs: set[str] = set()
        used_no_slug = False
        for idx in indices:
            cand = _candidate_slug_for(sections[idx])
            if cand is None:
                if not used_no_slug:
                    result[idx] = None
                    used_no_slug = True
                else:
                    n = 2
                    while str(n) in used_slugs:
                        n += 1
                    result[idx] = str(n)
                    used_slugs.add(str(n))
            else:
                if cand not in used_slugs:
                    result[idx] = cand
                    used_slugs.add(cand)
                else:
                    n = 2
                    while f"{cand}-{n}" in used_slugs:
                        n += 1
                    final = f"{cand}-{n}"
                    result[idx] = final
                    used_slugs.add(final)
    return result
