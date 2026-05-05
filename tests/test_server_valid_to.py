"""Test that brain_write supports valid_to parameter."""
from pathlib import Path

from symbiosis_brain.markdown_parser import parse_note, render_note
from symbiosis_brain.storage import Storage
from symbiosis_brain.sync import VaultSync


def test_render_note_includes_valid_to(tmp_vault: Path, db_path: Path):
    """brain_write with valid_to should persist it in frontmatter and DB."""
    md = render_note(
        title="Test Decision",
        body="Chose X over Y.",
        note_type="decision",
        scope="global",
        extra_frontmatter={"valid_from": "2026-04-15", "valid_to": "2027-04-15"},
    )
    assert "valid_to" in md
    assert "2027-04-15" in md

    # Write to vault and sync
    (tmp_vault / "decisions").mkdir(exist_ok=True)
    (tmp_vault / "decisions" / "test.md").write_text(md, encoding="utf-8")
    storage = Storage(db_path)
    sync = VaultSync(tmp_vault, storage)
    sync.sync_all()

    note = storage.get_note("decisions/test.md")
    assert note is not None
    assert note["valid_to"] == "2027-04-15"
