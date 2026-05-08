"""brain_write hard-block when gist is missing (validation gate)."""
import pytest
import asyncio
from pathlib import Path


@pytest.mark.asyncio
async def test_brain_write_blocks_when_gist_missing(tmp_vault: Path, db_path: Path):
    from symbiosis_brain import server
    from symbiosis_brain.storage import Storage
    from symbiosis_brain.search import SearchEngine
    from symbiosis_brain.sync import VaultSync
    from symbiosis_brain.temporal import TemporalManager

    server._storage = Storage(db_path)
    server._search = SearchEngine(server._storage)
    server._sync = VaultSync(tmp_vault, server._storage)
    server._temporal = TemporalManager(server._storage)
    server._vault_path = tmp_vault
    server._ready = asyncio.Event()
    server._ready.set()

    result = await server.call_tool("brain_write", {
        "path": "wiki/test.md", "title": "T", "body": "Body",
        "note_type": "wiki", "scope": "global",
    })
    text = result[0].text
    assert "Error" in text
    assert "gist" in text.lower()  # error message mentions gist
    assert not (tmp_vault / "wiki" / "test.md").exists()  # file not written


@pytest.mark.asyncio
async def test_brain_write_no_warning_when_gist_present(tmp_vault: Path, db_path: Path):
    from symbiosis_brain import server
    from symbiosis_brain.storage import Storage
    from symbiosis_brain.search import SearchEngine
    from symbiosis_brain.sync import VaultSync
    from symbiosis_brain.temporal import TemporalManager

    server._storage = Storage(db_path)
    server._search = SearchEngine(server._storage)
    server._sync = VaultSync(tmp_vault, server._storage)
    server._temporal = TemporalManager(server._storage)
    server._vault_path = tmp_vault
    server._ready = asyncio.Event()
    server._ready.set()

    result = await server.call_tool("brain_write", {
        "path": "wiki/test2.md", "title": "T2", "body": "Body",
        "note_type": "wiki", "scope": "global",
        "gist": "A useful one-line gist",
    })
    text = result[0].text
    assert "Saved" in text
    assert "⚠️" not in text  # no warning
