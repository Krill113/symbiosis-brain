"""brain_patch reaches frontmatter, and reaches it only through named fields.

Until now the tool edited the body and nothing else, so changing a `gist`, a
`tags` list or a `valid_to` meant editing the file behind the server's back —
the workaround that got written down three separate times before this.

The body is reachable ONLY through `anchor`, a field ONLY through its own
argument. Nothing has to decide which one the caller meant, because the two
never appear in the same place. An anchor allowed to match inside the YAML block
would be the ambiguous version of this: `gist:` occurs in bodies too, quoting and
folded values break substring edits, and the file would still parse afterwards —
a failure with nothing to notice.

Test pattern per tests/test_server_write_gates.py.
"""
from pathlib import Path

import pytest

import symbiosis_brain.server as server_mod


@pytest.fixture
async def server(tmp_vault_with_taxonomy: Path):
    server_mod._init(tmp_vault_with_taxonomy)
    yield server_mod
    if server_mod._storage is not None:
        server_mod._storage.close()
    for attr in ("_storage", "_search", "_sync", "_graph", "_temporal",
                 "_linter", "_vault_path"):
        setattr(server_mod, attr, None)


async def _call(name: str, args: dict) -> str:
    result = await server_mod.call_tool(name, args)
    return result[0].text


def _note_file(vault: Path, rel: str) -> str:
    return (vault / rel).read_text(encoding="utf-8")


def _meta(vault: Path, rel: str) -> dict:
    import frontmatter as _fm
    return dict(_fm.loads(_note_file(vault, rel)).metadata)


def _body(vault: Path, rel: str) -> str:
    import frontmatter as _fm
    return _fm.loads(_note_file(vault, rel)).content


def _raw_body(vault: Path, rel: str) -> str:
    """Everything after the closing `---`, verbatim.

    Deliberately not `frontmatter.loads(...).content`: the parser strips
    surrounding blank lines, so a body that gained or lost trailing whitespace
    compares equal through it. A mutation that appended a newline to the body
    survived this test until it was written this way."""
    text = _note_file(vault, rel)
    assert text.startswith("---")
    return text.split("---", 2)[2]


async def _seed(vault: Path, rel="wiki/subject.md", *, title="Subject",
                extra="") -> None:
    (vault / rel).parent.mkdir(parents=True, exist_ok=True)
    (vault / rel).write_text(
        f"---\ntitle: {title}\ntype: wiki\nscope: global\n"
        f"gist: the original gist\ntags: [alpha, beta]\n{extra}---\n\n"
        "Body first line.\nANCHOR-HERE\nBody last line.\n",
        encoding="utf-8",
    )
    await _call("brain_sync", {})


class TestFieldsWithoutTouchingTheBody:
    async def test_gist_changes_and_body_is_untouched(self, server, tmp_vault_with_taxonomy):
        """The point of the whole change: a field edit that does not pretend to
        be a body edit. Byte equality on the body is the assertion that fails if
        fields were ever applied to `post.content` instead of `post.metadata`."""
        vault = tmp_vault_with_taxonomy
        await _seed(vault)
        before = _raw_body(vault, "wiki/subject.md")

        msg = await _call("brain_patch", {"path": "wiki/subject.md",
                                          "gist": "a replacement gist"})

        assert msg.startswith("Patched")
        assert _meta(vault, "wiki/subject.md")["gist"] == "a replacement gist"
        assert _raw_body(vault, "wiki/subject.md") == before, \
            "a field-only patch must not move a single byte of the body"

    async def test_field_reaches_the_database_not_only_the_file(
        self, server, tmp_vault_with_taxonomy,
    ):
        """End-to-end through the closed-note filter shipped alongside this.

        Setting `valid_to` in the past closes the note, and a closed note stops
        being injected into prompts while staying findable on an explicit
        search. Both halves are read from the DB, so this passes only if the
        field travelled file → sync → column, not merely file."""
        vault = tmp_vault_with_taxonomy
        await _seed(vault)
        query = "Body first line ANCHOR"

        before = server_mod._search.search(query=query, limit=10,
                                           include_closed=False)
        assert any(h["path"] == "wiki/subject.md" for h in before), \
            "the note must be findable before it is closed, or this proves nothing"

        await _call("brain_patch", {"path": "wiki/subject.md",
                                    "valid_to": "2025-01-01"})

        injected = server_mod._search.search(query=query, limit=10,
                                             include_closed=False)
        explicit = server_mod._search.search(query=query, limit=10,
                                             include_closed=True)
        assert not any(h["path"] == "wiki/subject.md" for h in injected)
        assert any(h["path"] == "wiki/subject.md" for h in explicit)

    async def test_present_and_empty_clears_omitted_keeps(
        self, server, tmp_vault_with_taxonomy,
    ):
        """Same rule as brain_write, deliberately the same objects: `[]` clears,
        absent keeps. A truthiness test instead of the EMPTY_VALUES equality
        would make both cases look identical."""
        vault = tmp_vault_with_taxonomy
        await _seed(vault)

        await _call("brain_patch", {"path": "wiki/subject.md", "tags": []})
        meta = _meta(vault, "wiki/subject.md")
        # The key must be GONE, not present-and-empty. `assert not
        # meta.get("tags")` passed either way — an empty list is falsy — so it
        # guarded nothing, which a mutation that skipped the clearing branch
        # demonstrated by surviving it.
        assert "tags" not in meta, f"tags should be removed, found {meta.get('tags')!r}"
        assert meta["gist"] == "the original gist", "an omitted field must survive"

    async def test_body_and_fields_in_one_call(self, server, tmp_vault_with_taxonomy):
        vault = tmp_vault_with_taxonomy
        await _seed(vault)

        await _call("brain_patch", {"path": "wiki/subject.md",
                                    "anchor": "ANCHOR-HERE",
                                    "replacement": "REPLACED",
                                    "gist": "both at once"})

        assert "REPLACED" in _body(vault, "wiki/subject.md")
        assert "ANCHOR-HERE" not in _body(vault, "wiki/subject.md")
        assert _meta(vault, "wiki/subject.md")["gist"] == "both at once"


class TestRefusalsAreLoud:
    async def test_a_patch_that_asks_for_nothing_is_an_error(
        self, server, tmp_vault_with_taxonomy,
    ):
        """Not a silent success. "Patched" on a call that changed nothing is the
        shape of failure this codebase keeps finding: an answer that cannot be
        told apart from the real thing."""
        await _seed(tmp_vault_with_taxonomy)
        msg = await _call("brain_patch", {"path": "wiki/subject.md"})
        assert msg.startswith("Error:")
        assert "nothing to patch" in msg

    async def test_anchor_without_replacement_is_an_error(
        self, server, tmp_vault_with_taxonomy,
    ):
        await _seed(tmp_vault_with_taxonomy)
        msg = await _call("brain_patch", {"path": "wiki/subject.md",
                                          "anchor": "ANCHOR-HERE"})
        assert msg.startswith("Error:")


class TestTitleChangeNamesWhatItBreaks:
    async def test_inbound_links_written_as_the_old_title_are_listed(
        self, server, tmp_vault_with_taxonomy,
    ):
        """A count would say a problem exists and withhold where it is. The
        author would either not rename, or rename and go hunting. Names cost the
        same query."""
        vault = tmp_vault_with_taxonomy
        await _seed(vault, title="Subject")
        (vault / "wiki" / "referrer.md").write_text(
            "---\ntitle: Referrer\ntype: wiki\nscope: global\n"
            "gist: points at the subject by title\ntags: []\n---\n\n"
            "See [[Subject]] for details.\n",
            encoding="utf-8",
        )
        await _call("brain_sync", {})

        msg = await _call("brain_patch", {"path": "wiki/subject.md",
                                          "title": "Renamed Subject"})

        assert _meta(vault, "wiki/subject.md")["title"] == "Renamed Subject"
        assert "no longer resolve" in msg
        assert "wiki/referrer" in msg, "the warning must name the note, not count it"
