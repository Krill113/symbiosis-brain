"""Tests that brain_rename and brain_delete are exposed as MCP tools and behave
end-to-end. Uses the canonical server-test pattern (see test_server_append_patch.py)
— `import symbiosis_brain.server as server_mod` + module-level state init via
`server_mod._init(tmp_vault_with_taxonomy)` + teardown reset.

Pre-write of `wiki/b.md` and `wiki/a.md` goes through `brain_write`, which now
runs the Phase 2 gates. Both bodies use 2 wiki-link refs (self-refs for B, B-refs
for A) — but at the moment B's body is validated, B itself isn't in the DB yet,
so the self-refs would be flagged broken. Workaround: pre-seed the storage row
for B *before* the brain_write call, the same trick used in Phase 2 fixtures.
"""
from pathlib import Path

import pytest

import symbiosis_brain.server as server_mod


@pytest.fixture
async def initialized_server(tmp_vault_with_taxonomy: Path):
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


async def _seed_b_then_write(name: str = "wiki/b.md", body_extra: str = ""):
    """Pre-seed B in DB so the self-ref body validates, then write through MCP."""
    server_mod._storage.upsert_note(
        path=name,
        title="B",
        content="# B",
        note_type="wiki",
        scope="global",
        tags=[],
        frontmatter={"gist": "x"},
        valid_from=None,
        valid_to=None,
    )
    await _call("brain_write", {
        "path": name,
        "title": "B",
        "body": f"# B\n[[{name[:-3]}]] [[{name[:-3]}]]{body_extra}",
        "gist": "x",
    })


async def test_brain_rename_tool_registered(initialized_server):
    tools = await server_mod.list_tools()
    names = {t.name for t in tools}
    assert "brain_rename" in names
    assert "brain_delete" in names


async def test_brain_rename_end_to_end(
    initialized_server, tmp_vault_with_taxonomy: Path,
):
    await _seed_b_then_write()
    await _call("brain_write", {
        "path": "wiki/a.md",
        "title": "A",
        "body": "# A\nsee [[wiki/b]] and [[wiki/b|alias]]",
        "gist": "x",
    })

    text = await _call("brain_rename", {
        "old_path": "wiki/b.md",
        "new_path": "wiki/b-new.md",
    })
    assert "renamed" in text.lower()

    a_body = (tmp_vault_with_taxonomy / "wiki" / "a.md").read_text(encoding="utf-8")
    assert "[[wiki/b-new]]" in a_body
    assert "[[wiki/b]]" not in a_body
    assert "[[wiki/b-new|alias]]" in a_body


async def test_brain_delete_safe_blocks(
    initialized_server, tmp_vault_with_taxonomy: Path,
):
    await _seed_b_then_write()
    await _call("brain_write", {
        "path": "wiki/a.md", "title": "A",
        "body": "# A\n[[wiki/b]] [[wiki/b]]",
        "gist": "x",
    })

    text = await _call("brain_delete", {
        "path": "wiki/b.md",
        "mode": "safe",
    })
    assert ("refus" in text.lower()
            or "blocked" in text.lower()
            or "inbound" in text.lower()
            or "error" in text.lower())
    assert (tmp_vault_with_taxonomy / "wiki" / "b.md").exists()
