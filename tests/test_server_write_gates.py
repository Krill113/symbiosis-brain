"""Integration: brain_write MCP handler must reject hard-block violations and
render soft warnings without writing partial state.

Test pattern — see existing tests/test_server_append_patch.py for the canonical
shape: import `symbiosis_brain.server as server_mod`, init the module-level
state via `server_mod._init(tmp_vault_with_taxonomy)`, call tools via
`await server_mod.call_tool(...)`, then teardown by closing storage and resetting
all the module globals (otherwise the next test reuses stale state).

`pytest-asyncio` is in `asyncio_mode = "auto"` (see pyproject.toml), so async
test functions and async fixtures work without explicit `@pytest.mark.asyncio`.
"""
from pathlib import Path

import pytest

import symbiosis_brain.server as server_mod


@pytest.fixture
async def initialized_server(tmp_vault_with_taxonomy: Path):
    """Initialize module-level server state for a tmp vault. Tear down after."""
    server_mod._init(tmp_vault_with_taxonomy)
    yield server_mod
    if server_mod._storage is not None:
        server_mod._storage.close()
    for attr in ("_storage", "_search", "_sync", "_graph", "_temporal",
                 "_linter", "_vault_path"):
        setattr(server_mod, attr, None)


@pytest.fixture
async def initialized_server_with_anchor(initialized_server, tmp_vault_with_taxonomy: Path):
    """initialized_server + a pre-seeded `wiki/anchor.md` note that's safe to
    reference in test bodies (avoids broken-ref hard-block during setup)."""
    server_mod._storage.upsert_note(
        path="wiki/anchor.md",
        title="Anchor",
        content="# H",
        note_type="wiki",
        scope="global",
        tags=[],
        frontmatter={"gist": "anchor for test refs"},
        valid_from=None,
        valid_to=None,
    )
    yield server_mod


async def _call(name: str, args: dict) -> str:
    result = await server_mod.call_tool(name, args)
    return result[0].text


async def test_brain_write_missing_gist_does_not_write_file(
    initialized_server, tmp_vault_with_taxonomy: Path,
):
    text = await _call("brain_write", {
        "path": "wiki/new.md",
        "title": "New",
        "body": "# H",
    })
    assert "gist" in text.lower()
    assert "error" in text.lower() or "required" in text.lower()
    assert not (tmp_vault_with_taxonomy / "wiki" / "new.md").exists()


async def test_brain_write_broken_ref_does_not_write_file(
    initialized_server, tmp_vault_with_taxonomy: Path,
):
    text = await _call("brain_write", {
        "path": "wiki/new.md",
        "title": "New",
        "body": "# H\n[[wiki/does-not-exist]]",
        "gist": "ok",
    })
    assert "broken" in text.lower()
    assert not (tmp_vault_with_taxonomy / "wiki" / "new.md").exists()


async def test_brain_write_long_gist_writes_with_warning(
    initialized_server_with_anchor, tmp_vault_with_taxonomy: Path,
):
    """Soft-zone gist (>100 but ≤140) writes successfully with warning."""
    long_gist = "x" * 130
    text = await _call("brain_write", {
        "path": "wiki/new.md",
        "title": "New",
        "body": "# H\n[[wiki/anchor]] [[wiki/anchor]]",
        "gist": long_gist,
    })
    assert "saved" in text.lower()
    assert "gist" in text.lower() and "130" in text
    assert (tmp_vault_with_taxonomy / "wiki" / "new.md").exists()


async def test_brain_append_introducing_broken_ref_blocks(
    initialized_server_with_anchor, tmp_vault_with_taxonomy: Path,
):
    await _call("brain_write", {
        "path": "wiki/host.md",
        "title": "Host",
        "body": "# H\n## Sec\nfoo\n[[wiki/anchor]] [[wiki/anchor]]",
        "gist": "x",
    })
    text = await _call("brain_append", {
        "path": "wiki/host.md",
        "section": "Sec",
        "content": "[[wiki/never-existed]]",
    })
    assert "broken" in text.lower() or "error" in text.lower()
    body = (tmp_vault_with_taxonomy / "wiki" / "host.md").read_text(encoding="utf-8")
    assert "never-existed" not in body


async def test_brain_append_no_new_links_does_not_validate(
    initialized_server_with_anchor, tmp_vault_with_taxonomy: Path,
):
    """Pure-text appends bypass validation — they cannot introduce breakage."""
    await _call("brain_write", {
        "path": "wiki/host2.md",
        "title": "Host2",
        "body": "# H\n## Sec\nfoo\n[[wiki/anchor]] [[wiki/anchor]]",
        "gist": "x",
    })
    text = await _call("brain_append", {
        "path": "wiki/host2.md",
        "section": "Sec",
        "content": "more text without any wiki-links",
    })
    assert "appended" in text.lower()
