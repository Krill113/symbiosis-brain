from pathlib import Path

import pytest

from symbiosis_brain.storage import Storage
from symbiosis_brain.sync import VaultSync


def test_sync_one_inserts_new_note(tmp_vault: Path, db_path: Path):
    note = tmp_vault / "wiki" / "n1.md"
    note.write_text(
        "---\ntitle: N1\ntype: wiki\nscope: global\ntags: []\n---\n\nbody [[other]].\n",
        encoding="utf-8",
    )
    s = Storage(db_path)
    sync = VaultSync(tmp_vault, s)

    # Note: sync_one assumes note file is already on disk (caller wrote it).
    sync.sync_one("wiki/n1.md")

    row = s.get_note("wiki/n1.md")
    assert row is not None
    assert row["title"] == "N1"
    # Wikilinks resolved
    rels = s.get_relations("wiki/n1", direction="outgoing")
    assert len(rels) == 1
    assert rels[0]["raw_target"] == "other"
    s.close()


def test_sync_one_updates_existing_note(tmp_vault: Path, db_path: Path):
    note = tmp_vault / "wiki" / "n1.md"
    note.write_text(
        "---\ntitle: N1\ntype: wiki\nscope: global\ntags: []\n---\n\nv1 body.\n",
        encoding="utf-8",
    )
    s = Storage(db_path)
    sync = VaultSync(tmp_vault, s)
    sync.sync_one("wiki/n1.md")
    assert s.get_note("wiki/n1.md")["content"] == "v1 body."

    note.write_text(
        "---\ntitle: N1\ntype: wiki\nscope: global\ntags: []\n---\n\nv2 body.\n",
        encoding="utf-8",
    )
    sync.sync_one("wiki/n1.md")
    assert s.get_note("wiki/n1.md")["content"] == "v2 body."
    s.close()


def test_sync_one_does_not_scan_other_notes(tmp_vault: Path, db_path: Path):
    """sync_one for note A must not touch note B's row even if B was changed externally."""
    a = tmp_vault / "wiki" / "a.md"
    b = tmp_vault / "wiki" / "b.md"
    a.write_text("---\ntitle: A\ntype: wiki\nscope: global\ntags: []\n---\n\nA1.\n",
                 encoding="utf-8")
    b.write_text("---\ntitle: B\ntype: wiki\nscope: global\ntags: []\n---\n\nB1.\n",
                 encoding="utf-8")
    s = Storage(db_path)
    sync = VaultSync(tmp_vault, s)
    sync.sync_all()  # ingest both

    # External edit to B (e.g., Obsidian) — DB still has B1
    b.write_text("---\ntitle: B\ntype: wiki\nscope: global\ntags: []\n---\n\nB2_EXTERNAL.\n",
                 encoding="utf-8")
    a.write_text("---\ntitle: A\ntype: wiki\nscope: global\ntags: []\n---\n\nA2.\n",
                 encoding="utf-8")

    # sync_one A — must NOT pick up B's external change
    sync.sync_one("wiki/a.md")
    assert s.get_note("wiki/a.md")["content"] == "A2."
    assert s.get_note("wiki/b.md")["content"] == "B1.", \
        "sync_one for A should NOT touch B (external edits are caught by sync_all only)"
    s.close()
