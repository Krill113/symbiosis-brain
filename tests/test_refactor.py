"""Tests for the refactor module: brain_rename and brain_delete logic.

brain_rename(old_path, new_path):
  - Read all inbound refs to old_path
  - For each source, rewrite [[old_target]] → [[new_target]] in body
  - Move file from old_path to new_path
  - Re-sync index for all touched notes

brain_delete(path, mode='safe'|'cascade'):
  - safe (default): refuse if inbound refs exist; list them
  - cascade: replace inbound [[X]] with stub `~~old-link~~`; delete file
"""
from pathlib import Path

from symbiosis_brain.storage import Storage
from symbiosis_brain.sync import VaultSync
from symbiosis_brain.refactor import (
    brain_rename,
    brain_delete,
    DeleteBlockedError,
)


def _vault_with_refs(tmp_path: Path) -> tuple[Path, Storage, VaultSync]:
    vault = tmp_path / "vault"
    (vault / "wiki").mkdir(parents=True)
    storage = Storage(tmp_path / "test.db")
    sync = VaultSync(vault_path=vault, storage=storage)

    (vault / "wiki" / "b.md").write_text(
        "---\ntitle: B\ntype: wiki\nscope: global\ngist: x\n---\n# B\n",
        encoding="utf-8",
    )
    sync.sync_one("wiki/b.md")

    (vault / "wiki" / "a.md").write_text(
        "---\ntitle: A\ntype: wiki\nscope: global\ngist: x\n---\n# A\n"
        "see [[wiki/b]] and also [[wiki/b|B-alias]]\n",
        encoding="utf-8",
    )
    sync.sync_one("wiki/a.md")

    return vault, storage, sync


def test_brain_rename_rewrites_inbound_refs(tmp_path):
    vault, storage, sync = _vault_with_refs(tmp_path)

    brain_rename("wiki/b.md", "wiki/b-renamed.md", storage=storage, sync=sync, vault_path=vault)

    assert not (vault / "wiki" / "b.md").exists()
    assert (vault / "wiki" / "b-renamed.md").exists()

    a_body = (vault / "wiki" / "a.md").read_text()
    assert "[[wiki/b-renamed]]" in a_body
    assert "[[wiki/b-renamed|B-alias]]" in a_body
    assert "[[wiki/b]]" not in a_body
    assert "[[wiki/b|B-alias]]" not in a_body


def test_brain_delete_safe_blocks_when_inbound_refs_exist(tmp_path):
    vault, storage, sync = _vault_with_refs(tmp_path)

    try:
        brain_delete("wiki/b.md", mode="safe", storage=storage, sync=sync, vault_path=vault)
        assert False, "expected DeleteBlockedError"
    except DeleteBlockedError as e:
        msg = str(e)
        assert "wiki/a" in msg

    assert (vault / "wiki" / "b.md").exists()


def test_brain_delete_cascade_replaces_refs_with_stub(tmp_path):
    vault, storage, sync = _vault_with_refs(tmp_path)

    brain_delete("wiki/b.md", mode="cascade", storage=storage, sync=sync, vault_path=vault)

    assert not (vault / "wiki" / "b.md").exists()
    a_body = (vault / "wiki" / "a.md").read_text()
    assert "[[wiki/b]]" not in a_body
    assert "[[wiki/b|B-alias]]" not in a_body
    assert "~~wiki/b~~" in a_body
    assert "~~B-alias~~" in a_body


def test_brain_delete_safe_proceeds_when_no_inbound_refs(tmp_path):
    vault = tmp_path / "vault"
    (vault / "wiki").mkdir(parents=True)
    storage = Storage(tmp_path / "test.db")
    sync = VaultSync(vault_path=vault, storage=storage)

    (vault / "wiki" / "lonely.md").write_text(
        "---\ntitle: Lonely\ntype: wiki\nscope: global\ngist: x\n---\n# Lonely\n",
        encoding="utf-8",
    )
    sync.sync_one("wiki/lonely.md")

    brain_delete("wiki/lonely.md", mode="safe", storage=storage, sync=sync, vault_path=vault)

    assert not (vault / "wiki" / "lonely.md").exists()
