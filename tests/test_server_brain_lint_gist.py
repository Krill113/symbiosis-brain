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

    # Stylistic sections moved behind verbose=true (decision 4, 2026-08-25).
    result = await server.call_tool("brain_lint", {"verbose": True})
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

    text = (await server.call_tool("brain_lint", {"verbose": True}))[0].text

    assert "Forward refs: 1" in text
    assert "## Forward Refs" in text
    assert "forward:wiki/not-yet" in text
    # Настоящая битая ссылка осталась битой и ровно одна.
    assert "Broken links: 1" in text
    assert "wiki/ghost" in text


@pytest.fixture
async def lint_server(tmp_vault_with_taxonomy: Path, db_path: Path):
    """Server wired to a tiny vault that populates BOTH buckets: the mouth
    (gist_missing) and the stylistic ones (gist_too_long, gist_equals_title,
    orphans, since nothing links to these notes)."""
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
    yield server
    server._storage.close()
    for attr in ("_storage", "_search", "_sync", "_graph", "_temporal",
                 "_linter", "_vault_path"):
        setattr(server, attr, None)


@pytest.mark.asyncio
async def test_default_report_hides_stylistic_sections(lint_server):
    """The default report is the "mouth" only. Orphans + weak links alone were
    ~187 of the ~311 lines a live vault produced (measured 2026-08-25)."""
    text = (await lint_server.call_tool("brain_lint", {}))[0].text
    assert "## Orphans" not in text
    assert "## Weak Links" not in text
    assert "## Gist Too Long" not in text
    assert "## Gist Equals Title" not in text
    assert "Stylistic findings hidden — call brain_lint(verbose=true) to list them." in text


@pytest.mark.asyncio
async def test_gist_missing_stays_in_default_report(lint_server):
    """skills/brain-backfill-gists reads gist_missing out of the DEFAULT report
    (SKILL.md:15,21,60) — moving it behind verbose would break that skill
    silently."""
    text = (await lint_server.call_tool("brain_lint", {}))[0].text
    assert "## Gist Missing" in text
    assert "patterns/missing.md" in text


@pytest.mark.asyncio
async def test_verbose_report_lists_gist_too_long(lint_server):
    text = (await lint_server.call_tool("brain_lint", {"verbose": True}))[0].text
    assert "## Gist Too Long (>100 chars)" in text
    assert "patterns/long.md" in text
    assert "## Gist Equals Title" in text
    assert "Stylistic findings hidden" not in text


@pytest.mark.asyncio
async def test_summary_line_keeps_all_counters_in_both_modes(lint_server):
    """The owner's condition for splitting the report at all (decision 4):
    stylistic findings may leave the body, never the header — otherwise they rot
    unseen. One logical line, fixed order, both modes."""
    default_text = (await lint_server.call_tool("brain_lint", {}))[0].text
    verbose_text = (await lint_server.call_tool("brain_lint", {"verbose": True}))[0].text
    counters = (
        "Total notes:", "Audited:", "Orphans:", "Weak links:", "Broken links:",
        "Forward refs:", "Not indexed:", "Scope warnings:", "Type drift:",
        "Gist missing:", "Gist too long:", "Gist equals title:",
    )
    for mode, text in (("default", default_text), ("verbose", verbose_text)):
        header = [ln for ln in text.splitlines() if ln.startswith("Total notes:")]
        assert len(header) == 1, f"{mode}: expected exactly one counter line"
        for counter in counters:
            assert counter in header[0], f"{mode}: {counter} missing from the counter line"
    assert "Gist too long: 1" in default_text, \
        "a hidden section must still be counted in the default header"
