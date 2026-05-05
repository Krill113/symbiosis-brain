import pytest

from symbiosis_brain.sections import split_sections


BODY_SIMPLE = """Intro paragraph.

## Next Up

- item A
- item B

## Completed

- item C
"""


class TestSplitSections:
    def test_returns_preamble_and_sections(self):
        result = split_sections(BODY_SIMPLE)
        assert result["preamble"] == "Intro paragraph.\n\n"
        assert list(result["sections"].keys()) == ["Next Up", "Completed"]

    def test_section_content_includes_heading_line(self):
        result = split_sections(BODY_SIMPLE)
        assert result["sections"]["Next Up"].startswith("## Next Up\n")
        assert "- item A" in result["sections"]["Next Up"]
        assert "- item C" not in result["sections"]["Next Up"]

    def test_no_sections(self):
        result = split_sections("Just a paragraph.\n")
        assert result["preamble"] == "Just a paragraph.\n"
        assert result["sections"] == {}

    def test_section_names_preserve_case(self):
        body = "## next up\n\nfoo\n\n## Next Up\n\nbar\n"
        result = split_sections(body)
        assert list(result["sections"].keys()) == ["next up", "Next Up"]

    def test_trailing_whitespace_in_heading_stripped(self):
        body = "## Next Up   \n\nfoo\n"
        result = split_sections(body)
        assert "Next Up" in result["sections"]

    def test_preserves_h1_and_h3(self):
        body = "# Title\n\n### Subsection\n\nfoo\n\n## Real Section\n\nbar\n"
        result = split_sections(body)
        assert list(result["sections"].keys()) == ["Real Section"]
        assert "### Subsection" in result["preamble"]


from symbiosis_brain.sections import append_to_section, SectionNotFoundError


class TestAppendToSection:
    def test_appends_to_existing_section(self):
        body = "## Next Up\n\n- item A\n\n## Completed\n\n- item C\n"
        result = append_to_section(body, "Next Up", "- item B")
        assert result == "## Next Up\n\n- item A\n- item B\n\n## Completed\n\n- item C\n"

    def test_appends_to_last_section(self):
        body = "## Notes\n\nfirst\n"
        result = append_to_section(body, "Notes", "second")
        assert result == "## Notes\n\nfirst\nsecond\n"

    def test_keeps_other_sections_intact(self):
        body = "## A\n\nfoo\n\n## B\n\nbar\n\n## C\n\nbaz\n"
        result = append_to_section(body, "B", "EXTRA")
        assert "## A\n\nfoo\n" in result
        assert "## C\n\nbaz\n" in result
        assert "bar\nEXTRA\n" in result

    def test_missing_section_raises(self):
        body = "## Next Up\n\nfoo\n"
        with pytest.raises(SectionNotFoundError) as exc:
            append_to_section(body, "Ideas", "x")
        assert "Ideas" in str(exc.value)
        assert "Next Up" in str(exc.value)  # lists available

    def test_create_if_missing_appends_new_section(self):
        body = "## Next Up\n\nfoo\n"
        result = append_to_section(body, "Ideas", "new idea", create_if_missing=True)
        assert result.endswith("## Ideas\n\nnew idea\n")
        assert "## Next Up\n\nfoo\n" in result

    def test_create_if_missing_on_empty_body(self):
        result = append_to_section("", "Notes", "first", create_if_missing=True)
        assert result == "## Notes\n\nfirst\n"

    def test_case_sensitive(self):
        body = "## Next Up\n\nfoo\n"
        with pytest.raises(SectionNotFoundError):
            append_to_section(body, "next up", "x")

    def test_normalizes_trailing_whitespace(self):
        body = "## Notes\n\nfirst\n\n\n\n"  # excess blank lines at end
        result = append_to_section(body, "Notes", "second")
        # exactly one blank line between existing content and new, one trailing newline
        assert result == "## Notes\n\nfirst\nsecond\n"

    def test_preserves_crlf_body(self):
        body = "## A\r\n\r\n- x\r\n\r\n## B\r\n\r\n- y\r\n"
        result = append_to_section(body, "A", "- new")
        # Output must use CRLF throughout, no bare LF
        assert "\r\n" in result
        stripped = result.replace("\r\n", "")
        assert "\n" not in stripped
        # Content added to correct section, other sections intact
        assert "- x\r\n- new\r\n" in result
        assert "## B\r\n\r\n- y\r\n" in result

    def test_handles_mixed_eol_content(self):
        # Body in CRLF, inserted content in LF — result should be uniform CRLF
        body = "## Notes\r\n\r\nfirst\r\n"
        result = append_to_section(body, "Notes", "second\nthird")
        assert "\r\n" in result
        stripped = result.replace("\r\n", "")
        assert "\n" not in stripped
        assert "second\r\nthird" in result

    def test_lf_only_body_unchanged_eol(self):
        # LF-only body must not gain CRLF after append
        body = "## A\n\n- x\n"
        result = append_to_section(body, "A", "- y")
        assert "\r" not in result
        assert result == "## A\n\n- x\n- y\n"


from symbiosis_brain.sections import (
    replace_anchor,
    AnchorNotFoundError,
    AnchorAmbiguousError,
)


class TestReplaceAnchor:
    def test_replaces_unique_anchor(self):
        body = "## Next Up\n\n- old item\n- other\n"
        result = replace_anchor(body, "- old item", "- new item")
        assert result == "## Next Up\n\n- new item\n- other\n"

    def test_empty_replacement_deletes(self):
        body = "foo\nbar\nbaz\n"
        result = replace_anchor(body, "bar\n", "")
        assert result == "foo\nbaz\n"

    def test_multiline_anchor(self):
        body = "keep\n\nA\nB\nC\n\nkeep\n"
        result = replace_anchor(body, "A\nB\nC", "X\nY")
        assert result == "keep\n\nX\nY\n\nkeep\n"

    def test_anchor_not_found(self):
        with pytest.raises(AnchorNotFoundError):
            replace_anchor("foo\n", "bar", "baz")

    def test_anchor_ambiguous(self):
        body = "foo\nfoo\n"
        with pytest.raises(AnchorAmbiguousError) as exc:
            replace_anchor(body, "foo", "bar")
        assert "2" in str(exc.value)  # mentions count

    def test_normalizes_crlf_in_body_for_matching(self):
        body = "line1\r\nline2\r\nline3\r\n"
        result = replace_anchor(body, "line2", "LINE2")
        # Output preserves original CRLF style
        assert result == "line1\r\nLINE2\r\nline3\r\n"

    def test_normalizes_crlf_in_anchor(self):
        body = "line1\r\nline2\r\nline3\r\n"
        result = replace_anchor(body, "line1\nline2", "X")
        assert result == "X\r\nline3\r\n"
