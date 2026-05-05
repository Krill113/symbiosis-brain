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
