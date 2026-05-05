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


class TestBrainContextHubFilter:
    async def test_hub_marker_in_output(self, initialized_server):
        storage = server_mod._storage
        # Use canonical path-key form for "Claude Code" (wiki/claude-code is in DEFAULT_HUB_BLOCKLIST)
        for n in ("Start", "wiki/claude-code", "LeafBehindHub"):
            storage.upsert_entity(n, "concept")
        storage.upsert_relation("Start", "wiki/claude-code", "uses")
        storage.upsert_relation("wiki/claude-code", "LeafBehindHub", "contains")

        out = await _call("brain_context", {"entity": "Start", "depth": 2})
        assert "wiki/claude-code" in out
        assert "[HUB]" in out
        # Leaf behind hub must NOT appear — hub blocks expansion by default.
        assert "LeafBehindHub" not in out

    async def test_include_hubs_expands_through(self, initialized_server):
        storage = server_mod._storage
        # Use canonical path-key form for "Claude Code" (wiki/claude-code is in DEFAULT_HUB_BLOCKLIST)
        for n in ("Start", "wiki/claude-code", "LeafBehindHub"):
            storage.upsert_entity(n, "concept")
        storage.upsert_relation("Start", "wiki/claude-code", "uses")
        storage.upsert_relation("wiki/claude-code", "LeafBehindHub", "contains")

        out = await _call("brain_context", {
            "entity": "Start",
            "depth": 2,
            "include_hubs": True,
        })
        assert "LeafBehindHub" in out

    async def test_custom_hub_threshold(self, initialized_server):
        storage = server_mod._storage
        for n in ("Root", "Target", "Hidden"):
            storage.upsert_entity(n, "concept")
        storage.upsert_relation("Root", "Target", "uses")
        storage.upsert_relation("Target", "Hidden", "uses")
        # Give Target 4 incoming edges total → exceeds threshold=3
        for i, src in enumerate(("A", "B", "C")):
            storage.upsert_entity(src, "concept")
            storage.upsert_relation(src, "Target", "uses")

        out = await _call("brain_context", {
            "entity": "Root",
            "depth": 2,
            "hub_threshold": 3,
        })
        assert "Target" in out
        assert "[HUB]" in out
        assert "Hidden" not in out
