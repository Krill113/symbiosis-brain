"""Regression coverage for brain_rotate_handoffs: newly created archive
notes must land in notes_vec, not just the notes table — a permanent
count drift here was the trigger for the reindex storm (every stale-lock
window it then went unguarded through, see F5 in the final fix report).
Uses the canonical server-test pattern (see test_server_refactor_tools.py)
— `server_mod._init(...)` + `await server_mod.call_tool(name, args)`.
"""
import json
import shutil
from pathlib import Path

import pytest

import symbiosis_brain.server as server_mod

FIXTURE = Path(__file__).parent / "fixtures" / "card-snapshot-synthetic.md"
FAKE_VEC = [0.1] * 384


def _fake_embed(texts):
    return [FAKE_VEC for _ in texts]


@pytest.fixture
async def initialized_server(tmp_vault_with_taxonomy: Path, monkeypatch):
    monkeypatch.setattr("symbiosis_brain.search._embed", _fake_embed)
    projects_dir = tmp_vault_with_taxonomy / "projects"
    # Card filename == scope, so rotate_handoffs resolves it via the direct
    # projects/<scope>.md path (see test_rotation_integration.py).
    shutil.copy(FIXTURE, projects_dir / "demo.md")
    server_mod._init(tmp_vault_with_taxonomy)
    yield server_mod
    if server_mod._storage is not None:
        server_mod._storage.close()
    for attr in ("_storage", "_search", "_sync", "_graph", "_temporal",
                 "_linter", "_vault_path"):
        setattr(server_mod, attr, None)


async def test_rotate_handoffs_archives_stay_embedded(initialized_server):
    result = await server_mod.call_tool(
        "brain_rotate_handoffs", {"scope": "demo", "dry_run": False}
    )
    payload = json.loads(result[0].text)
    created = payload["archive_files_created"]
    assert created, "fixture card (8 handoff sections) must produce archive files"

    conn = server_mod._storage._conn
    for rel in created:
        row = conn.execute(
            "SELECT path FROM notes_vec WHERE path=?", (rel,)
        ).fetchone()
        assert row is not None, f"{rel} missing a notes_vec row after rotation"

    assert server_mod._search.is_index_dirty() is False


async def test_rotate_handoffs_stamps_archive_notes(initialized_server):
    """Сервер прокидывает written_by в ротацию (I-16 п. 4). Значение проверяем
    по форме, а не буквально: клиента в тесте нет (LookupError -> unknown/unknown),
    а модель после CP-5 может прийти из моста."""
    import re
    from datetime import date

    result = await server_mod.call_tool(
        "brain_rotate_handoffs", {"scope": "demo", "dry_run": False}
    )
    payload = json.loads(result[0].text)
    created = payload["archive_files_created"]
    assert created

    pattern = re.compile(rf'^written_by: "\S+/\S+ \S+ {date.today().isoformat()}"$', re.M)
    for rel in created:
        text = (server_mod._vault_path / rel).read_text(encoding="utf-8")
        assert pattern.search(text), text[:400]
