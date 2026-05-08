from pathlib import Path

import pytest

import symbiosis_brain.server as server_mod


@pytest.fixture
async def initialized_server(tmp_vault_with_taxonomy: Path):
    """Initialize the module-level server state against a tmp vault."""
    server_mod._init(tmp_vault_with_taxonomy)
    yield server_mod
    if server_mod._storage is not None:
        server_mod._storage.close()
    server_mod._storage = None
    server_mod._search = None
    server_mod._sync = None
    server_mod._graph = None
    server_mod._temporal = None
    server_mod._vault_path = None
    server_mod._linter = None


async def _call(name: str, args: dict) -> str:
    result = await server_mod.call_tool(name, args)
    return result[0].text


@pytest.fixture
async def seeded_backlog(initialized_server, tmp_vault_with_taxonomy: Path):
    """Write a realistic backlog note via brain_write, return its path."""
    await _call("brain_write", {
        "path": "projects/sample-backlog.md",
        "title": "Sample Backlog",
        "body": "## Next Up\n\n- [[forward:Alpha]] task [[forward:Beta]]\n\n## Ideas\n\n- idea 1\n\n## Completed\n\n- done 1\n",
        "note_type": "project",
        "scope": "global",
        "gist": "sample backlog for append/patch tests",
    })
    return tmp_vault_with_taxonomy / "projects" / "sample-backlog.md"


class TestBrainAppend:
    async def test_appends_to_existing_section(self, seeded_backlog: Path):
        msg = await _call("brain_append", {
            "path": "projects/sample-backlog.md",
            "section": "Next Up",
            "content": "- extra item",
        })
        assert "Appended" in msg
        text = seeded_backlog.read_text(encoding="utf-8")
        assert "[[forward:Alpha]]" in text
        assert "- extra item" in text
        assert "- idea 1" in text
        assert "- done 1" in text

    async def test_missing_section_returns_error(self, seeded_backlog: Path):
        msg = await _call("brain_append", {
            "path": "projects/sample-backlog.md",
            "section": "Nonexistent",
            "content": "- x",
        })
        assert "Error" in msg
        assert "Nonexistent" in msg
        assert "Next Up" in msg  # lists available

    async def test_create_if_missing(self, seeded_backlog: Path):
        msg = await _call("brain_append", {
            "path": "projects/sample-backlog.md",
            "section": "Parked",
            "content": "- parked item",
            "create_if_missing": True,
        })
        assert "Appended" in msg
        text = seeded_backlog.read_text(encoding="utf-8")
        assert "## Parked" in text
        assert "- parked item" in text

    async def test_path_outside_vault_rejected(self, initialized_server):
        msg = await _call("brain_append", {
            "path": "../outside.md",
            "section": "X",
            "content": "y",
        })
        assert "Error" in msg
        assert "vault" in msg.lower()

    async def test_note_not_found(self, initialized_server):
        msg = await _call("brain_append", {
            "path": "projects/does-not-exist.md",
            "section": "Next Up",
            "content": "x",
        })
        assert "Error" in msg
        assert "not found" in msg.lower()

    async def test_search_index_updated(self, seeded_backlog: Path):
        await _call("brain_append", {
            "path": "projects/sample-backlog.md",
            "section": "Next Up",
            "content": "- unique_marker_zxcv",
        })
        search_result = await _call("brain_search", {"query": "unique_marker_zxcv"})
        assert "unique_marker_zxcv" in search_result


class TestBrainPatch:
    async def test_replaces_unique_anchor(self, seeded_backlog: Path):
        msg = await _call("brain_patch", {
            "path": "projects/sample-backlog.md",
            "anchor": "[[forward:Alpha]] task",
            "replacement": "[[forward:Alpha]] task DONE",
        })
        assert "Patched" in msg
        text = seeded_backlog.read_text(encoding="utf-8")
        assert "[[forward:Alpha]] task DONE" in text
        assert "- idea 1" in text  # untouched

    async def test_anchor_not_found(self, seeded_backlog: Path):
        msg = await _call("brain_patch", {
            "path": "projects/sample-backlog.md",
            "anchor": "nonexistent_text_zzzz",
            "replacement": "x",
        })
        assert "Error" in msg
        assert "not found" in msg.lower()

    async def test_anchor_ambiguous(self, initialized_server, tmp_vault_with_taxonomy: Path):
        await _call("brain_write", {
            "path": "projects/dup.md",
            "title": "Dup",
            "body": "## Notes\n\nfoo\nfoo\n[[forward:A]] [[forward:B]]",
            "note_type": "project",
            "gist": "duplicate anchor test note",
        })
        msg = await _call("brain_patch", {
            "path": "projects/dup.md",
            "anchor": "foo",
            "replacement": "bar",
        })
        assert "Error" in msg
        assert "2" in msg or "multiple" in msg.lower() or "unique" in msg.lower()

    async def test_empty_replacement_deletes(self, seeded_backlog: Path):
        msg = await _call("brain_patch", {
            "path": "projects/sample-backlog.md",
            "anchor": "- idea 1\n",
            "replacement": "",
        })
        assert "Patched" in msg
        text = seeded_backlog.read_text(encoding="utf-8")
        assert "- idea 1" not in text
        assert "- done 1" in text  # untouched

    async def test_frontmatter_not_searched(self, initialized_server, tmp_vault_with_taxonomy: Path):
        await _call("brain_write", {
            "path": "projects/fm-guard.md",
            "title": "FM Guard",
            "body": "## Notes\n\nThe word test appears here.\n[[forward:A]] [[forward:B]]",
            "note_type": "project",
            "tags": ["test"],  # 'test' appears in frontmatter as a tag
            "gist": "frontmatter guard test note",
        })
        msg = await _call("brain_patch", {
            "path": "projects/fm-guard.md",
            "anchor": "test",
            "replacement": "TEST",
        })
        # Should find 'test' only in body, not in frontmatter tags → unique match OK
        assert "Patched" in msg
        raw = (tmp_vault_with_taxonomy / "projects" / "fm-guard.md").read_text(encoding="utf-8")
        # Tag should still be "test" in frontmatter; body word replaced to "TEST"
        assert "tags:\n- test" in raw or "tags: [test]" in raw or "- test" in raw.split("---")[1]
        body = raw.split("---", 2)[2]
        assert "TEST appears" in body

    async def test_path_outside_vault_rejected(self, initialized_server):
        msg = await _call("brain_patch", {
            "path": "../outside.md",
            "anchor": "x",
            "replacement": "y",
        })
        assert "Error" in msg
        assert "vault" in msg.lower()
