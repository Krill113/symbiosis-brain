"""Unit tests for symbiosis_brain.rotation."""
from datetime import date as Date

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
