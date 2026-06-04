from symbiosis_brain.markdown_parser import parse_note, extract_wikilinks, render_note


class TestParseNote:
    def test_parses_frontmatter_and_body(self, sample_note_content: str):
        note = parse_note(sample_note_content)
        assert note["title"] == "Dapper vs EF Core"
        assert note["type"] == "decision"
        assert note["scope"] == "beta"
        assert "Chose" in note["body"]
        assert "orm" in note["tags"]

    def test_handles_missing_frontmatter(self):
        note = parse_note("# Just a heading\n\nSome text")
        assert note["title"] == "Just a heading"
        assert note["type"] == "wiki"
        assert note["scope"] == "global"
        assert note["body"] == "# Just a heading\n\nSome text"

    def test_handles_missing_title_in_frontmatter(self):
        content = "---\ntype: note\n---\n# My Title\n\nBody"
        note = parse_note(content)
        assert note["title"] == "My Title"

    def test_extracts_valid_from(self, sample_note_content: str):
        note = parse_note(sample_note_content)
        assert note["valid_from"] == "2025-03-15"


class TestExtractWikilinks:
    def test_extracts_simple_link(self):
        links = extract_wikilinks("See [[Dapper]] please")
        assert links == [{"raw": "Dapper", "target": "Dapper", "alias": None}]

    def test_extracts_pipe_alias(self):
        links = extract_wikilinks("See [[projects/foo|Foo Project]]")
        assert links == [{
            "raw": "projects/foo|Foo Project",
            "target": "projects/foo",
            "alias": "Foo Project",
        }]

    def test_unescapes_pipe(self):
        links = extract_wikilinks(r"See [[projects/foo\|Foo]]")
        assert links == [{
            "raw": r"projects/foo\|Foo",
            "target": "projects/foo",
            "alias": "Foo",
        }]

    def test_multiple_pipes_split_on_first(self):
        links = extract_wikilinks("[[a|b|c]]")
        assert links == [{"raw": "a|b|c", "target": "a", "alias": "b|c"}]

    def test_trims_whitespace(self):
        links = extract_wikilinks("[[  projects/foo  |  Foo  ]]")
        assert links == [{
            "raw": "  projects/foo  |  Foo  ",
            "target": "projects/foo",
            "alias": "Foo",
        }]

    def test_skips_empty(self):
        assert extract_wikilinks("[[]]") == []
        assert extract_wikilinks("[[   ]]") == []

    def test_no_links(self):
        assert extract_wikilinks("No links here") == []

    def test_deduplicates_by_raw(self):
        links = extract_wikilinks("[[A]] and [[B]] and [[A]] again")
        assert links == [
            {"raw": "A", "target": "A", "alias": None},
            {"raw": "B", "target": "B", "alias": None},
        ]

    def test_ignores_markdown_links(self):
        links = extract_wikilinks("Some [regular](link) and [[wiki]]")
        assert links == [{"raw": "wiki", "target": "wiki", "alias": None}]

    # FR4/Q3 — wiki-links inside code regions are documentation, not real links.

    def test_ignores_wikilink_in_inline_code(self):
        links = extract_wikilinks("Use `[[forward:X|y]]` syntax and [[real]]")
        assert links == [{"raw": "real", "target": "real", "alias": None}]

    def test_ignores_wikilink_in_fenced_block(self):
        text = "Before\n\n```\n[[wiki/x]]\n```\n\nSee [[real]]\n"
        links = extract_wikilinks(text)
        assert links == [{"raw": "real", "target": "real", "alias": None}]

    def test_ignores_wikilink_in_tilde_fence(self):
        text = "Before\n\n~~~\n[[wiki/x]]\n~~~\n\nSee [[real]]\n"
        links = extract_wikilinks(text)
        assert links == [{"raw": "real", "target": "real", "alias": None}]

    def test_inline_code_with_multiple_backticks(self):
        # A 2-backtick span closes only on an exactly-2 run; the inner lone
        # backtick is literal, so [[x]] stays inside the code span.
        links = extract_wikilinks("``code with [[x]] and a ` tick``")
        assert links == []

    def test_prose_link_kept_when_inline_code_on_same_line(self):
        links = extract_wikilinks("[[real]] then `[[x]]`")
        assert links == [{"raw": "real", "target": "real", "alias": None}]

    def test_offsets_preserved_for_raw_after_code(self):
        # raw must be sliced from the ORIGINAL text, not the masked filler.
        links = extract_wikilinks(r"`[[a]]` [[b\|c]]")
        assert links == [{"raw": r"b\|c", "target": "b", "alias": "c"}]

    def test_unterminated_inline_backtick_is_prose(self):
        links = extract_wikilinks("a ` lone backtick then [[real]]")
        assert links == [{"raw": "real", "target": "real", "alias": None}]

    def test_unterminated_fence_masks_to_eof(self):
        text = "intro [[keep]]\n\n```\n[[gone]]\nstill in fence [[gone2]]\n"
        links = extract_wikilinks(text)
        assert links == [{"raw": "keep", "target": "keep", "alias": None}]

    def test_fence_with_info_string_is_masked(self):
        text = "```python\n[[wiki/x]]\n```\n[[real]]\n"
        links = extract_wikilinks(text)
        assert links == [{"raw": "real", "target": "real", "alias": None}]


class TestRenderNote:
    def test_renders_note_to_markdown(self):
        result = render_note(
            title="Test Note",
            body="## Content\n\nSome text about [[Concept]]",
            note_type="wiki",
            scope="global",
            tags=["test", "example"],
            extra_frontmatter={"valid_from": "2025-01-01"},
        )
        assert "---" in result
        assert "title: Test Note" in result
        assert "type: wiki" in result
        assert "scope: global" in result
        assert "tags:" in result
        assert "## Content" in result
