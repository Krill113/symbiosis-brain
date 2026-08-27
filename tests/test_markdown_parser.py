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

    def test_unquoted_date_frontmatter_normalized_to_str(self):
        """YAML turns an unquoted `valid_from: 2026-08-25` into datetime.date;
        sqlite3 then binds it through the DEFAULT DATE ADAPTER, deprecated since
        3.12 and scheduled for removal — at which point every note with an
        unquoted date fails its write with InterfaceError. brain_rotate_handoffs
        emits exactly this shape (rotation.py:238), so it is not a hypothetical.
        parse_note must hand storage a plain ISO string."""
        content = (
            "---\ntitle: Dated\ntype: wiki\nscope: global\n"
            "valid_from: 2026-08-25\n"
            "valid_to: 2026-09-01\n"
            "created_at: 2026-08-25 10:00:00\n"
            "---\n\nBody\n"
        )
        note = parse_note(content)
        assert isinstance(note["valid_from"], str)
        assert note["valid_from"] == "2026-08-25"
        assert isinstance(note["valid_to"], str)
        assert note["valid_to"] == "2026-09-01"
        assert isinstance(note["created_at"], str)
        assert note["created_at"].startswith("2026-08-25T10:00:00")


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

    def test_extract_wikilinks_does_not_span_newline(self):
        """Незакрытая '[[' не должна съедать следующую строку (B1, второе звено).

        Радиус проверен линзой B по всему vault: кросс-строчных [[…\n…]] — 3, и все
        три внутри ноты бэклога, где этот баг цитируется. Легитимных нет.
        """
        text = "- item [[wiki/broken\n- other [[wiki/real]]\n"
        links = extract_wikilinks(text)
        assert [l["target"] for l in links] == ["wiki/real"]
        assert all("\n" not in l["raw"] for l in links)

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


import frontmatter as _fm  # noqa: E402 — секция CP-4, импорт рядом с её тестами

from symbiosis_brain.markdown_parser import merge_frontmatter  # noqa: E402


class TestMergeFrontmatter:
    """I-13: чистая функция «ключи existing, которых НЕТ в incoming»."""

    def test_returns_only_keys_absent_from_incoming(self):
        existing = {
            "title": "Old Title", "type": "wiki", "scope": "global",
            "umbrella": "alpha", "aliases": ["ALPHA"], "created_at": "2026-01-01",
        }
        incoming = {"type": "decision", "scope": "beta"}
        assert merge_frontmatter(existing, incoming) == {
            "title": "Old Title",
            "umbrella": "alpha",
            "aliases": ["ALPHA"],
            "created_at": "2026-01-01",
        }

    def test_never_returns_written_by(self):
        """Штамп всегда перезаписывается сервером (§3.1): вернуть старый — значит
        воскресить чужую подпись под новой записью."""
        existing = {"written_by": "oldclient/0.0.1 old-model-1 2026-01-01",
                    "umbrella": "alpha"}
        assert merge_frontmatter(existing, {}) == {"umbrella": "alpha"}
        assert merge_frontmatter(existing, {"written_by": "x"}) == {"umbrella": "alpha"}

    def test_substitutes_no_defaults(self):
        assert merge_frontmatter({}, {}) == {}
        assert merge_frontmatter({}, {"type": "wiki"}) == {}

    def test_renames_nothing(self):
        """`note_type` — имя АРГУМЕНТА тула; ключей с таким именем функция не знает."""
        assert merge_frontmatter({"type": "wiki"}, {"note_type": "decision"}) == {"type": "wiki"}

    def test_does_not_mutate_its_arguments(self):
        existing = {"umbrella": "alpha", "written_by": "oldclient/0.0.1 old-model-1 2026-01-01"}
        incoming = {"scope": "beta"}
        merge_frontmatter(existing, incoming)
        assert existing == {"umbrella": "alpha",
                            "written_by": "oldclient/0.0.1 old-model-1 2026-01-01"}
        assert incoming == {"scope": "beta"}


class TestRenderNotePreserved:
    """I-13: `preserved` вливается ПЕРЕД `extra_frontmatter`; tags=None ≠ tags=[]."""

    def test_preserved_keys_survive_the_render(self):
        out = render_note(
            title="T", body="B", note_type="wiki", scope="global",
            preserved={"umbrella": "alpha", "aliases": ["ALPHA"],
                       "created_at": "2026-01-01"},
        )
        meta = _fm.loads(out).metadata
        assert meta["umbrella"] == "alpha"
        assert meta["aliases"] == ["ALPHA"]
        assert meta["created_at"] == "2026-01-01"

    def test_extra_frontmatter_overrides_preserved(self):
        """Это и есть механизм перезаписи старой подписи (I-13): порядок
        «preserved, затем extra_frontmatter» обязателен."""
        out = render_note(
            title="T", body="B",
            preserved={"written_by": "oldclient/0.0.1 old-model-1 2026-01-01",
                       "gist": "old gist"},
            extra_frontmatter={"written_by": "testclient/9.9.9 test-model-9 2026-01-02",
                               "gist": "new gist"},
        )
        meta = _fm.loads(out).metadata
        assert meta["written_by"] == "testclient/9.9.9 test-model-9 2026-01-02"
        assert meta["gist"] == "new gist"

    def test_preserved_supplies_type_and_scope_over_the_defaults(self):
        """Ядро дефекта B4: перезапись без note_type/scope больше не сбрасывает
        ноту в wiki/global — значение берётся из файла."""
        out = render_note(
            title="T", body="B", note_type="wiki", scope="global",
            preserved={"type": "decision", "scope": "beta"},
        )
        meta = _fm.loads(out).metadata
        assert meta["type"] == "decision"
        assert meta["scope"] == "beta"

    def test_preserved_never_overrides_title(self):
        """`title` — обязательный параметр функции и `required` в схеме тула
        (server.py:370), поэтому в `preserved` его быть не может; если он туда
        всё же попал, побеждает аргумент вызова, а не старый файл."""
        out = render_note(title="New Title", body="B",
                          preserved={"title": "Old Title"})
        assert _fm.loads(out).metadata["title"] == "New Title"

    def test_tags_none_takes_the_preserved_tags(self):
        out = render_note(title="T", body="B", tags=None,
                          preserved={"tags": ["alpha", "beta"]})
        assert _fm.loads(out).metadata["tags"] == ["alpha", "beta"]

    def test_tags_empty_list_clears_the_key(self):
        out = render_note(title="T", body="B", tags=[],
                          preserved={"tags": ["alpha", "beta"]})
        assert "tags" not in _fm.loads(out).metadata

    def test_tags_non_empty_wins_over_preserved(self):
        out = render_note(title="T", body="B", tags=["gamma"],
                          preserved={"tags": ["alpha"]})
        assert _fm.loads(out).metadata["tags"] == ["gamma"]

    def test_preserved_none_is_todays_behaviour(self):
        out = render_note(title="T", body="B", note_type="wiki", scope="global",
                          tags=["x"], extra_frontmatter={"gist": "g"})
        meta = _fm.loads(out).metadata
        assert meta == {"title": "T", "type": "wiki", "scope": "global",
                        "tags": ["x"], "gist": "g"}
