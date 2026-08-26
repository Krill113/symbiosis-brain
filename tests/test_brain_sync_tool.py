"""Coverage for the brain_sync MCP tool: default targeted-diff behavior vs
full=true triggering a full re-embed. Uses the canonical server-test pattern
(see test_server_refactor_tools.py) — `server_mod._init(...)` + `await
server_mod.call_tool(name, args)` + teardown reset.
"""
import json
from pathlib import Path

import pytest

import symbiosis_brain.server as server_mod
from symbiosis_brain.search import SearchEngine

FAKE_VEC = [0.1] * 384


def _fake_embed(texts):
    return [FAKE_VEC for _ in texts]


@pytest.fixture
async def initialized_server(tmp_vault_with_taxonomy: Path, monkeypatch):
    monkeypatch.setattr("symbiosis_brain.search._embed", _fake_embed)
    for i in range(3):
        (tmp_vault_with_taxonomy / "wiki" / f"n{i}.md").write_text(
            f"---\ntitle: Note {i}\ntype: wiki\nscope: global\ntags: []\n---\n\nBody {i}.\n",
            encoding="utf-8",
        )
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


async def test_brain_sync_default_indexes_diff_not_full(initialized_server):
    """Plain brain_sync (no `full`) must go through the targeted diff +
    repair_index path, never index_all — that's the fix this test guards."""
    orig_index_all = SearchEngine.index_all
    call_count = {"n": 0}

    def counting_index_all(self, *a, **kw):
        call_count["n"] += 1
        return orig_index_all(self, *a, **kw)

    SearchEngine.index_all = counting_index_all
    try:
        text = await _call("brain_sync", {})
    finally:
        SearchEngine.index_all = orig_index_all

    assert call_count["n"] == 0, "default brain_sync must not do a full re-embed"
    assert "repaired" in text


async def test_brain_sync_full_true_calls_index_all_once(initialized_server):
    """brain_sync(full=true) must trigger exactly one full re-embed."""
    orig_index_all = SearchEngine.index_all
    call_count = {"n": 0}

    def counting_index_all(self, *a, **kw):
        call_count["n"] += 1
        return orig_index_all(self, *a, **kw)

    SearchEngine.index_all = counting_index_all
    try:
        await _call("brain_sync", {"full": True})
    finally:
        SearchEngine.index_all = orig_index_all

    assert call_count["n"] == 1, "full=true must trigger exactly one index_all"


async def test_targeted_index_goes_through_single_helper(initialized_server, monkeypatch):
    """brain_sync must reuse server._apply_targeted_index instead of carrying its
    own copy of the delete_vec/index_note loop. The two copies (server.py:134-141
    in _init and server.py:709-715 in brain_sync) were identical by convention
    only — nothing kept them that way. RED before the helper exists:
    monkeypatch.setattr raises AttributeError."""
    calls = []
    real = server_mod._apply_targeted_index

    def counting(sync_result):
        calls.append((list(sync_result.added), list(sync_result.updated),
                      list(sync_result.removed)))
        return real(sync_result)

    monkeypatch.setattr(server_mod, "_apply_targeted_index", counting)

    (server_mod._vault_path / "wiki" / "fresh.md").write_text(
        "---\ntitle: Fresh\ntype: wiki\nscope: global\ntags: []\n---\n\nbody.\n",
        encoding="utf-8",
    )
    await _call("brain_sync", {})

    assert len(calls) == 1, "brain_sync must call the shared helper exactly once"
    assert calls[0][0] == ["wiki/fresh.md"], f"helper got the wrong diff: {calls[0]}"
