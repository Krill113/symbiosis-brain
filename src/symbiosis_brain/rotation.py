"""Handoff rotation — see docs/superpowers/specs/2026-05-15-b2-handoff-rotation-design.md."""
from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date as Date
from pathlib import Path
from typing import Optional

from symbiosis_brain.atomic_write import atomic_write_text
from symbiosis_brain.write_lock import note_write_lock

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


ARCHIVE_HEADING = "## Archive"
ARCHIVE_INTRO = "Старые handoff'ы (по убыванию даты):"
INDEX_ENTRY_RE = re.compile(r"^- (\d{4}-\d{2}-\d{2}):")
INDEX_ONELINER_MAX = 100


def render_archive_file(
    section: HandoffSection,
    scope: str,
    slug: Optional[str],
    gist: str,
) -> str:
    """Render full archive file content (frontmatter + body + footer)."""
    date_str = section.date.isoformat()
    # Strip leading punctuation from suffix if it starts with em-dash
    title_suffix = ""
    if section.suffix:
        s = section.suffix.lstrip("—-").strip()
        title_suffix = f" — {s}" if s else ""
    title = f"Handoff {date_str}{title_suffix}"

    body_lines = section.body.splitlines()
    # Drop heading line(s) — first '## Handoff ...' line, plus any immediately following blank line
    if body_lines and body_lines[0].startswith("## Handoff"):
        body_lines = body_lines[1:]
    while body_lines and not body_lines[0].strip():
        body_lines = body_lines[1:]
    body_content = "\n".join(body_lines).rstrip()

    frontmatter = (
        f"---\n"
        f"title: {title}\n"
        f"type: project\n"
        f"scope: {scope}\n"
        f"gist: {gist}\n"
        f"valid_from: {date_str}\n"
        f"tags: [handoff, {scope}]\n"
        f"---\n"
    )
    footer = f"\n---\n*Архивный handoff. Свежие — в [[projects/{scope}]] §«Handoff …».*\n"
    return f"{frontmatter}\n# {title}\n\n{body_content}\n{footer}"


def render_archive_index_entry(
    section: HandoffSection,
    scope: str,
    slug: Optional[str],
    gist: str,
) -> str:
    """One '## Archive' index line."""
    date_str = section.date.isoformat()
    slug_part = f"-{slug}" if slug else ""
    link = f"archive/handoffs/{scope}-{date_str}{slug_part}"
    oneliner = gist[:INDEX_ONELINER_MAX].rstrip()
    return f"- {date_str}: [[{link}]] — {oneliner}"


def apply_archive_to_card(
    card_text: str,
    archived: list[tuple[HandoffSection, str]],
) -> str:
    """Remove archived sections from card; insert/merge into '## Archive' index."""
    if not archived:
        return card_text
    # Remove spans (in reverse order to keep indices stable)
    spans = sorted([(s.start, s.end) for s, _ in archived])
    out_parts: list[str] = []
    cursor = 0
    for start, end in spans:
        out_parts.append(card_text[cursor:start])
        cursor = end
    out_parts.append(card_text[cursor:])
    cleaned = "".join(out_parts)
    # Collapse leftover triple+ blank lines
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)

    new_entries = [entry for _, entry in archived]
    archive_re = re.compile(r"(^## Archive\b.*?)(?=^## |\Z)", re.MULTILINE | re.DOTALL)
    m = archive_re.search(cleaned)

    def _date_key(line: str) -> str:
        mm = INDEX_ENTRY_RE.match(line)
        return mm.group(1) if mm else ""

    if m:
        existing_block = m.group(1)
        existing_entries = [
            line for line in existing_block.splitlines()
            if INDEX_ENTRY_RE.match(line)
        ]
        all_entries = sorted(existing_entries + new_entries, key=_date_key, reverse=True)
        new_archive_block = (
            f"{ARCHIVE_HEADING}\n\n{ARCHIVE_INTRO}\n\n"
            + "\n".join(all_entries) + "\n\n"
        )
        return cleaned[: m.start()] + new_archive_block + cleaned[m.end():]
    else:
        sorted_new = sorted(new_entries, key=_date_key, reverse=True)
        new_archive_block = (
            f"\n{ARCHIVE_HEADING}\n\n{ARCHIVE_INTRO}\n\n"
            + "\n".join(sorted_new) + "\n"
        )
        return cleaned.rstrip() + new_archive_block


def select_candidates_to_archive(
    sections: list[HandoffSection],
    inline_days: int = 2,
) -> tuple[list[HandoffSection], list[HandoffSection]]:
    """Return (inline, candidates_for_archive).

    Inline = sections whose date is one of the N most recent distinct dates.
    Candidates = everything else.
    """
    if not sections:
        return [], []
    if inline_days < 1 or inline_days > 7:
        raise ValueError(f"inline_days must be in [1..7], got {inline_days}")
    distinct_dates_desc = sorted({s.date for s in sections}, reverse=True)
    keep_dates = set(distinct_dates_desc[:inline_days])
    inline = [s for s in sections if s.date in keep_dates]
    candidates = [s for s in sections if s.date not in keep_dates]
    return inline, candidates


@dataclass
class SkipReason:
    card: str
    reason: str


@dataclass
class RotationReport:
    cards_processed: int = 0
    cards_modified: int = 0
    sections_archived: int = 0
    archive_files_created: list[str] = field(default_factory=list)
    skipped: list[SkipReason] = field(default_factory=list)
    bytes_freed_per_card: dict[str, int] = field(default_factory=dict)


class ConflictError(RuntimeError):
    """Archive file exists with different content than what we'd write."""


def _rotate_one_card(
    vault: Path,
    card_path: Path,
    scope: str,
    inline_days: int,
    dry_run: bool,
    report: RotationReport,
) -> None:
    report.cards_processed += 1
    rel_card = card_path.relative_to(vault).as_posix()
    text = card_path.read_text(encoding="utf-8")
    sections = parse_handoff_sections(text)
    if not sections:
        report.skipped.append(SkipReason(card=rel_card, reason="no handoff sections"))
        return
    inline, candidates = select_candidates_to_archive(sections, inline_days=inline_days)
    if not candidates:
        report.skipped.append(SkipReason(card=rel_card, reason="all sections within inline window"))
        return

    slugs = assign_slugs(sections)
    section_to_slug = {id(s): slugs[i] for i, s in enumerate(sections)}

    archived: list[tuple[HandoffSection, str]] = []
    archive_dir = vault / "archive" / "handoffs"
    archive_dir.mkdir(parents=True, exist_ok=True)

    for s in candidates:
        slug = section_to_slug[id(s)]
        gist = extract_gist(s.body)
        archive_content = render_archive_file(s, scope=scope, slug=slug, gist=gist)
        slug_part = f"-{slug}" if slug else ""
        archive_path = archive_dir / f"{scope}-{s.date.isoformat()}{slug_part}.md"
        rel_archive = archive_path.relative_to(vault).as_posix()

        if archive_path.exists():
            existing = archive_path.read_text(encoding="utf-8")
            if existing.strip() != archive_content.strip():
                raise ConflictError(f"Archive file exists with different content: {archive_path}")
            # Same content → idempotent skip (no new write, but still remove from card if present)
        else:
            if not dry_run:
                with note_write_lock(vault, rel_archive):
                    atomic_write_text(archive_path, archive_content)
            report.archive_files_created.append(rel_archive)

        entry = render_archive_index_entry(s, scope=scope, slug=slug, gist=gist)
        archived.append((s, entry))
        report.sections_archived += 1

    new_card = apply_archive_to_card(text, archived)
    if not dry_run and new_card != text:
        with note_write_lock(vault, rel_card):
            atomic_write_text(card_path, new_card)
        report.cards_modified += 1
        report.bytes_freed_per_card[rel_card] = len(text) - len(new_card)


def rotate_handoffs(
    vault: Path,
    scope: Optional[str] = None,
    dry_run: bool = False,
    inline_days: int = 2,
) -> RotationReport:
    """Rotate stale handoffs from project cards into archive/handoffs/.

    If scope is None — walks all projects/*.md (auto-discovery).
    If scope is given — operates only on projects/<scope>.md.

    Note: this function is the pure-Python core. The MCP wrapper (server.py)
    is responsible for calling _sync.sync_one(rel_path) on modified files
    AFTER this returns, to re-index into the brain DB.
    """
    if inline_days < 1 or inline_days > 7:
        raise ValueError(f"inline_days must be in [1..7], got {inline_days}")

    vault = Path(vault)
    report = RotationReport()
    projects_dir = vault / "projects"
    if not projects_dir.exists():
        return report

    if scope:
        targets = [projects_dir / f"{scope}.md"]
    else:
        targets = sorted(projects_dir.glob("*.md"))

    for card_path in targets:
        if not card_path.exists():
            report.skipped.append(SkipReason(card=card_path.name, reason="card not found"))
            continue
        # Derive scope from filename if scope=None mode
        card_scope = scope or card_path.stem
        _rotate_one_card(vault, card_path, card_scope, inline_days, dry_run, report)
    return report
