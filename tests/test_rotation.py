"""Unit tests for symbiosis_brain.rotation."""
from datetime import date as Date
from typing import Optional

import pytest

from symbiosis_brain.rotation import HandoffSection, extract_gist, parse_handoff_sections


def test_module_imports():
    assert parse_handoff_sections is not None


def test_parses_simple_handoff():
    text = "## Handoff 2026-05-14\nbody line\n"
    sections = parse_handoff_sections(text)
    assert len(sections) == 1
    assert sections[0].date == Date(2026, 5, 14)
    assert sections[0].suffix is None
    assert "body line" in sections[0].body


def test_parses_suffix_with_em_dash():
    text = "## Handoff 2026-05-14 — MCP Zombie Shutdown shipped\nbody\n"
    sections = parse_handoff_sections(text)
    assert sections[0].suffix == "— MCP Zombie Shutdown shipped"


def test_parses_word_suffix():
    text = "## Handoff 2026-05-14 evening\nbody\n"
    sections = parse_handoff_sections(text)
    assert sections[0].suffix == "evening"


def test_section_ends_at_next_h2():
    text = "## Handoff 2026-05-14\nbody1\n## Other\nbody2\n"
    sections = parse_handoff_sections(text)
    assert len(sections) == 1
    assert "body1" in sections[0].body
    assert "Other" not in sections[0].body


def test_multiple_handoffs_in_order():
    text = "## Handoff 2026-05-14\nA\n## Handoff 2026-05-13\nB\n"
    sections = parse_handoff_sections(text)
    assert len(sections) == 2
    assert sections[0].date == Date(2026, 5, 14)
    assert sections[1].date == Date(2026, 5, 13)


def test_malformed_date_skipped():
    text = "## Handoff 9999-13-99\nbad\n## Handoff 2026-05-14\nok\n"
    sections = parse_handoff_sections(text)
    assert len(sections) == 1
    assert sections[0].date == Date(2026, 5, 14)


def test_empty_text_returns_empty():
    assert parse_handoff_sections("") == []


def test_text_without_handoffs_returns_empty():
    assert parse_handoff_sections("## Other heading\nbody\n") == []


def test_handoff_inside_fenced_code_block_ignored():
    text = (
        "## Handoff 2026-05-14\nreal body\n"
        "```markdown\n"
        "## Handoff 2026-05-08\n"
        "fake inside code block\n"
        "```\n"
        "more real body\n"
    )
    sections = parse_handoff_sections(text)
    assert len(sections) == 1
    assert sections[0].date == Date(2026, 5, 14)
    assert "## Handoff 2026-05-08" in sections[0].body  # code-fence content stays in body
    assert "more real body" in sections[0].body


def test_gist_from_shipped_simple():
    body = "## Handoff 2026-05-14\n**Shipped:** Closed bug X. More text.\n"
    assert extract_gist(body) == "Closed bug X"


def test_gist_from_shipped_with_parenthetical():
    body = "## Handoff 2026-05-14\n**Shipped (full day):** Did the thing. Etc.\n"
    assert extract_gist(body) == "Did the thing"


def test_gist_truncates_at_140():
    long = "x" * 200
    body = f"## Handoff 2026-05-14\n**Shipped:** {long}\n"
    g = extract_gist(body)
    assert len(g) == 140
    assert g.endswith("…")


def test_gist_fallback_first_line():
    body = "## Handoff 2026-05-14\nFirst informative line. Second.\n"
    assert extract_gist(body) == "First informative line"


def test_gist_literal_fallback():
    body = "## Handoff 2026-05-14\n"
    assert extract_gist(body) == "Handoff"


from symbiosis_brain.rotation import _candidate_slug_for, assign_slugs


def _section(date: Date, suffix: Optional[str] = None, body: str = "") -> "HandoffSection":
    from symbiosis_brain.rotation import HandoffSection
    return HandoffSection(start=0, end=10, date=date, suffix=suffix, body=body)


def test_slug_from_em_dash_suffix():
    s = _section(Date(2026, 5, 14), suffix="— MCP Zombie Shutdown shipped")
    assert _candidate_slug_for(s) == "mcp-zombie-shutdown"


def test_slug_from_word_suffix():
    s = _section(Date(2026, 5, 14), suffix="evening")
    assert _candidate_slug_for(s) == "evening"


def test_slug_from_compound_suffix():
    s = _section(Date(2026, 5, 14), suffix="late evening")
    assert _candidate_slug_for(s) == "late-evening"


def test_slug_from_shipped_fallback():
    body = "## Handoff 2026-05-14\n**Shipped:** Phase 6 concurrency safety shipped.\n"
    s = _section(Date(2026, 5, 14), suffix=None, body=body)
    assert _candidate_slug_for(s) == "phase-6-concurrency-safety"


def test_slug_none_when_no_signal():
    s = _section(Date(2026, 5, 14), suffix=None, body="## Handoff 2026-05-14\n")
    assert _candidate_slug_for(s) is None


def test_slug_strips_non_ascii():
    s = _section(Date(2026, 5, 14), suffix="Финиш B1 shipped")
    assert _candidate_slug_for(s) in ("b1", "finish-b1")  # transliteration may vary


def test_assign_slugs_single_no_suffix():
    sections = [_section(Date(2026, 5, 14))]
    assert assign_slugs(sections) == [None]


def test_assign_slugs_burst_no_suffix():
    sections = [_section(Date(2026, 5, 14)), _section(Date(2026, 5, 14))]
    assert assign_slugs(sections) == [None, "2"]


def test_assign_slugs_distinct_suffixes_no_collision():
    sections = [
        _section(Date(2026, 5, 14), suffix="evening"),
        _section(Date(2026, 5, 14), suffix="late evening"),
    ]
    assert assign_slugs(sections) == ["evening", "late-evening"]


def test_assign_slugs_same_suffix_collision():
    sections = [
        _section(Date(2026, 5, 14), suffix="evening"),
        _section(Date(2026, 5, 14), suffix="evening"),
    ]
    assert assign_slugs(sections) == ["evening", "evening-2"]


def test_assign_slugs_separate_dates_no_collision():
    sections = [
        _section(Date(2026, 5, 14), suffix="evening"),
        _section(Date(2026, 5, 13), suffix="evening"),
    ]
    assert assign_slugs(sections) == ["evening", "evening"]


from symbiosis_brain.rotation import select_candidates_to_archive


def test_select_keeps_2_distinct_dates():
    sections = [
        _section(Date(2026, 5, 14)),
        _section(Date(2026, 5, 13)),
        _section(Date(2026, 5, 12)),
        _section(Date(2026, 5, 8)),
    ]
    inline, cands = select_candidates_to_archive(sections, inline_days=2)
    assert {s.date for s in inline} == {Date(2026, 5, 14), Date(2026, 5, 13)}
    assert {s.date for s in cands} == {Date(2026, 5, 12), Date(2026, 5, 8)}


def test_select_burst_all_inline():
    sections = [
        _section(Date(2026, 5, 14)),
        _section(Date(2026, 5, 14), suffix="evening"),
        _section(Date(2026, 5, 14), suffix="late"),
        _section(Date(2026, 5, 14), suffix="night"),
        _section(Date(2026, 5, 13)),
    ]
    inline, cands = select_candidates_to_archive(sections, inline_days=2)
    assert len(inline) == 5
    assert cands == []


def test_select_sparse_keeps_recent_distinct():
    sections = [
        _section(Date(2026, 5, 14)),
        _section(Date(2026, 4, 20)),
        _section(Date(2026, 4, 15)),
    ]
    inline, cands = select_candidates_to_archive(sections, inline_days=2)
    assert {s.date for s in inline} == {Date(2026, 5, 14), Date(2026, 4, 20)}
    assert {s.date for s in cands} == {Date(2026, 4, 15)}


def test_select_empty():
    assert select_candidates_to_archive([], inline_days=2) == ([], [])


def test_select_all_within_window():
    sections = [_section(Date(2026, 5, 14)), _section(Date(2026, 5, 13))]
    inline, cands = select_candidates_to_archive(sections, inline_days=2)
    assert len(inline) == 2
    assert cands == []


from symbiosis_brain.rotation import (
    render_archive_file, render_archive_index_entry, apply_archive_to_card,
)


def test_render_archive_file_with_suffix():
    s = _section(
        Date(2026, 5, 14), suffix="— MCP Zombie Shutdown shipped",
        body="## Handoff 2026-05-14 — MCP Zombie Shutdown shipped\n\n**Shipped:** Closed bug.\n",
    )
    out = render_archive_file(s, scope="symbiosis-brain", slug="mcp-zombie", gist="Closed bug")
    assert out.startswith("---\n")
    assert "type: project" in out
    assert "scope: symbiosis-brain" in out
    assert "gist: Closed bug" in out
    assert "valid_from: 2026-05-14" in out
    assert "tags: [handoff, symbiosis-brain]" in out
    assert "# Handoff 2026-05-14 — MCP Zombie Shutdown shipped" in out
    assert "**Shipped:** Closed bug" in out
    assert "Архивный handoff" in out


def test_render_archive_file_no_suffix():
    s = _section(Date(2026, 5, 14), suffix=None, body="## Handoff 2026-05-14\n**Shipped:** X.\n")
    out = render_archive_file(s, scope="ld", slug=None, gist="X")
    assert "title: Handoff 2026-05-14\n" in out
    assert "# Handoff 2026-05-14\n" in out


def test_render_archive_index_entry_with_slug():
    s = _section(Date(2026, 5, 14))
    line = render_archive_index_entry(s, scope="symbiosis-brain", slug="mcp-zombie", gist="Closed bug X")
    assert line == "- 2026-05-14: [[archive/handoffs/symbiosis-brain-2026-05-14-mcp-zombie]] — Closed bug X"


def test_render_archive_index_entry_no_slug():
    s = _section(Date(2026, 5, 14))
    line = render_archive_index_entry(s, scope="ld", slug=None, gist="Some shipped item")
    assert line == "- 2026-05-14: [[archive/handoffs/ld-2026-05-14]] — Some shipped item"


def test_apply_archive_to_card_no_existing_archive_section():
    card = (
        "# Project\n\n"
        "## Roadmap\nrows\n\n"
        "## Handoff 2026-05-14\nrecent body\n\n"
        "## Handoff 2026-05-08\nold body\n"
    )
    sections = parse_handoff_sections(card)
    old = [s for s in sections if s.date == Date(2026, 5, 8)]
    entry = "- 2026-05-08: [[archive/handoffs/x-2026-05-08]] — Old item"
    out = apply_archive_to_card(card, [(old[0], entry)])
    assert "## Handoff 2026-05-08" not in out
    assert "## Handoff 2026-05-14" in out
    assert "## Archive" in out
    assert entry in out


def test_apply_archive_merges_existing_archive_section():
    card = (
        "## Handoff 2026-05-14\nA\n\n"
        "## Handoff 2026-05-13\nB\n\n"
        "## Handoff 2026-05-08\nC\n\n"
        "## Archive\n\nСтарые handoff'ы (по убыванию даты):\n\n"
        "- 2026-04-29: [[archive/handoffs/x-2026-04-29]] — Older item\n"
    )
    sections = parse_handoff_sections(card)
    old = [s for s in sections if s.date == Date(2026, 5, 8)]
    entry = "- 2026-05-08: [[archive/handoffs/x-2026-05-08]] — Item C"
    out = apply_archive_to_card(card, [(old[0], entry)])
    assert out.index(entry) < out.index("Older item")
