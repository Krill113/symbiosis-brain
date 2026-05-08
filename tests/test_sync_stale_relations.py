"""Test that creating a note re-resolves any previously-broken inbound relations
that pointed at it. Otherwise the relations table contains stale broken=True
entries, which inflate brain_lint's broken-link count.

Repro for the live-vault case: wiki/lazy-commands-index.md was written first
(referencing wiki/lazy-cmd-common, which didn't exist yet) → relation inserted
broken=True. Later wiki/lazy-cmd-common.md was created via brain_write, but the
existing broken-relation pointing at it was never re-resolved — sync_one only
re-walks the *outgoing* relations of the writing note.

Note on identifiers in this test:
- The relations table stores `from_name` as the canonical path WITHOUT `.md`
  (sync.py line 146 strips the suffix), so `get_relations("wiki/a", ...)` is
  the correct lookup, not `get_relations("a", ...)`.
- For broken targets, `to_name` is `f"broken:{target[:200]}"` (sync.py line 152);
  filtering on `raw_target` (the original [[...]] body) is the stable key.
"""
from pathlib import Path

from symbiosis_brain.storage import Storage
from symbiosis_brain.sync import VaultSync


def _build_vault(tmp_path: Path) -> tuple[Path, Storage, VaultSync]:
    vault = tmp_path / "vault"
    (vault / "wiki").mkdir(parents=True)
    storage = Storage(tmp_path / "test.db")
    sync = VaultSync(vault_path=vault, storage=storage)
    return vault, storage, sync


def test_inbound_broken_relation_resolves_when_target_created(tmp_path):
    vault, storage, sync = _build_vault(tmp_path)

    # 1. Write A referencing B (B doesn't exist yet)
    a = vault / "wiki" / "a.md"
    a.write_text("---\ntitle: A\ntype: wiki\nscope: global\n---\n# A\n[[wiki/b]]\n")
    sync.sync_one("wiki/a.md")

    # Verify A→B relation is currently broken
    rels = storage.get_relations("wiki/a", direction="outgoing")
    refs_to_b = [r for r in rels if r.get("raw_target") == "wiki/b"]
    assert len(refs_to_b) == 1
    assert refs_to_b[0]["broken"] == 1  # SQLite stores INTEGER for broken column

    # 2. Now create B
    b = vault / "wiki" / "b.md"
    b.write_text("---\ntitle: B\ntype: wiki\nscope: global\n---\n# B\n")
    sync.sync_one("wiki/b.md")

    # 3. Assert: A→B is no longer broken
    rels = storage.get_relations("wiki/a", direction="outgoing")
    refs_to_b = [r for r in rels if r.get("raw_target") == "wiki/b"]
    assert len(refs_to_b) == 1
    assert refs_to_b[0]["broken"] == 0, "Creating B should re-resolve A→B"
    assert refs_to_b[0]["to_name"] == "wiki/b"


def test_inbound_broken_relations_with_anchor_resolve(tmp_path):
    """Same as above but the link in A uses [[wiki/b#section]] form."""
    vault, storage, sync = _build_vault(tmp_path)

    a = vault / "wiki" / "a.md"
    a.write_text("---\ntitle: A\ntype: wiki\nscope: global\n---\n# A\n[[wiki/b#sec]]\n")
    sync.sync_one("wiki/a.md")

    b = vault / "wiki" / "b.md"
    b.write_text("---\ntitle: B\ntype: wiki\nscope: global\n---\n# B\n")
    sync.sync_one("wiki/b.md")

    rels = storage.get_relations("wiki/a", direction="outgoing")
    refs = [r for r in rels if r.get("raw_target") == "wiki/b#sec"]
    assert len(refs) == 1
    assert refs[0]["broken"] == 0
    assert refs[0]["to_name"] == "wiki/b"
