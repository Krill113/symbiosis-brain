"""Smoke tests for brain_search Tool spec + handler accepting `mode` parameter."""
import pytest
from pathlib import Path
import asyncio


def test_tool_spec_includes_mode_parameter():
    from symbiosis_brain import server
    # list_tools is a coroutine; run it to get spec
    tools = asyncio.run(server.list_tools())
    brain_search = next(t for t in tools if t.name == "brain_search")
    props = brain_search.inputSchema["properties"]
    assert "mode" in props
    assert props["mode"]["enum"] == ["preview", "gist"]
    assert props["mode"]["default"] == "preview"


@pytest.mark.asyncio
async def test_handler_passes_mode_to_search(monkeypatch, tmp_vault: Path, db_path: Path):
    from symbiosis_brain import server
    from symbiosis_brain.storage import Storage
    from symbiosis_brain.search import SearchEngine
    from symbiosis_brain.temporal import TemporalManager

    storage = Storage(db_path)
    storage.upsert_note(
        path="patterns/x.md", title="X", scope="global", note_type="pattern",
        content="Body", frontmatter={"gist": "A gist for X note"}, tags=[],
    )
    server._storage = storage
    server._search = SearchEngine(storage)
    server._search.index_all()
    server._temporal = TemporalManager(storage)
    server._vault_path = tmp_vault
    server._ready = asyncio.Event()
    server._ready.set()

    result = await server.call_tool("brain_search", {"query": "X", "mode": "gist"})
    text = result[0].text
    # In gist mode, output should be compact and include the gist string
    assert "A gist for X note" in text
