"""brain_lint output displays gist warnings."""
import pytest
import asyncio
from pathlib import Path


@pytest.mark.asyncio
async def test_brain_lint_shows_gist_sections(
    tmp_vault_with_taxonomy: Path, db_path: Path
):
    from symbiosis_brain import server
    from symbiosis_brain.storage import Storage
    from symbiosis_brain.search import SearchEngine
    from symbiosis_brain.lint import VaultLinter

    server._storage = Storage(db_path)
    server._storage.upsert_note(
        path="patterns/missing.md", title="Missing", scope="global",
        note_type="pattern", content="Body", frontmatter={}, tags=[],
    )
    server._storage.upsert_note(
        path="patterns/long.md", title="Long", scope="global",
        note_type="pattern", content="Body",
        frontmatter={"gist": "x" * 105}, tags=[],
    )
    server._storage.upsert_note(
        path="patterns/dup.md", title="Same Title", scope="global",
        note_type="pattern", content="Body",
        frontmatter={"gist": "Same Title"}, tags=[],
    )
    server._search = SearchEngine(server._storage)
    server._linter = VaultLinter(server._storage, vault_path=tmp_vault_with_taxonomy)
    server._vault_path = tmp_vault_with_taxonomy
    server._ready = asyncio.Event()
    server._ready.set()

    result = await server.call_tool("brain_lint", {})
    text = result[0].text
    assert "Gist Missing" in text
    assert "Gist Too Long" in text
    assert "Gist Equals Title" in text
    assert "patterns/missing.md" in text
    assert "patterns/long.md" in text
    assert "patterns/dup.md" in text


@pytest.mark.asyncio
async def test_report_lists_forward_refs_separately(
    tmp_vault_with_taxonomy: Path, db_path: Path
):
    """Отчёт печатает Forward refs отдельным счётчиком и отдельной секцией."""
    from symbiosis_brain import server
    from symbiosis_brain.storage import Storage
    from symbiosis_brain.search import SearchEngine
    from symbiosis_brain.lint import VaultLinter
    from symbiosis_brain.sync import VaultSync

    (tmp_vault_with_taxonomy / "wiki" / "planner.md").write_text(
        "---\ntitle: Planner\ntype: wiki\nscope: global\ngist: plans ahead\n---\n\n"
        "Later: [[forward:wiki/not-yet]] and a real miss [[wiki/ghost]].\n",
        encoding="utf-8",
    )
    storage = Storage(db_path)
    VaultSync(tmp_vault_with_taxonomy, storage).sync_all()

    server._storage = storage
    server._search = SearchEngine(storage)
    server._linter = VaultLinter(storage, vault_path=tmp_vault_with_taxonomy)
    server._vault_path = tmp_vault_with_taxonomy
    server._ready = asyncio.Event()
    server._ready.set()

    text = (await server.call_tool("brain_lint", {}))[0].text

    assert "Forward refs: 1" in text
    assert "## Forward Refs" in text
    assert "forward:wiki/not-yet" in text
    # Настоящая битая ссылка осталась битой и ровно одна.
    assert "Broken links: 1" in text
    assert "wiki/ghost" in text
