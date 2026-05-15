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
