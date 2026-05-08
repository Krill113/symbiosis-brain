"""Atomic refactor operations on the vault.

brain_rename(old_path, new_path) — rewrite all inbound [[old]] references to
[[new]] in source notes, then move the file. Idempotent on the file move
(refuses to overwrite if new_path already exists).

brain_delete(path, mode) — `safe` mode refuses if inbound refs exist (lists
them via DeleteBlockedError); `cascade` mode replaces inbound refs with a
strikethrough stub `~~old-target~~` (or `~~alias~~` for aliased refs), then
deletes the file.

Both operations propagate via `sync.sync_one()` on each touched note so the
relations table stays consistent in the same transaction-ish window. Phase 6
concurrency-safety guarantees: sync_one holds the per-note write lock; we
process source notes one at a time.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING

from symbiosis_brain.atomic_write import atomic_write_text
from symbiosis_brain.markdown_parser import extract_wikilinks
from symbiosis_brain.write_lock import note_write_lock

if TYPE_CHECKING:
    from symbiosis_brain.storage import Storage
    from symbiosis_brain.sync import VaultSync


class DeleteBlockedError(Exception):
    """Raised by brain_delete in `safe` mode when inbound refs exist."""


def _strip_md_ext(p: str) -> str:
    return p[:-3] if p.lower().endswith(".md") else p


def _canonical(path: str) -> str:
    """vault-relative path without .md, the form used in the relations table.
    Normalizes path separators to forward-slash (Windows callers may pass `\\`)."""
    return _strip_md_ext(path).replace("\\", "/")


def _rewrite_links_in_body(body: str, old_canonical: str, new_canonical: str) -> str:
    """Rewrite [[old_canonical]] and [[old_canonical|alias]] to use new_canonical.
    Preserves alias text. Case-insensitive match on the target portion (matches
    how the resolver canonicalizes — lowercase-equality).
    """
    def repl(m: re.Match[str]) -> str:
        inner = m.group(1)
        unescaped = inner.replace(r"\|", "|")
        parts = unescaped.split("|", 1)
        target = parts[0].strip()
        alias = parts[1].strip() if len(parts) == 2 else None
        if _strip_md_ext(target).lower() != old_canonical.lower():
            return m.group(0)
        if alias:
            return f"[[{new_canonical}|{alias}]]"
        return f"[[{new_canonical}]]"

    return re.sub(r"\[\[([^\]]+)\]\]", repl, body)


def _replace_with_stub(body: str, old_canonical: str) -> str:
    """For brain_delete cascade: replace [[old]] with ~~old~~ (alias preserved)."""
    def repl(m: re.Match[str]) -> str:
        inner = m.group(1)
        unescaped = inner.replace(r"\|", "|")
        parts = unescaped.split("|", 1)
        target = parts[0].strip()
        alias = parts[1].strip() if len(parts) == 2 else None
        if _strip_md_ext(target).lower() != old_canonical.lower():
            return m.group(0)
        text = alias or target
        return f"~~{text}~~"

    return re.sub(r"\[\[([^\]]+)\]\]", repl, body)


def brain_rename(
    old_path: str,
    new_path: str,
    *,
    storage: Storage,
    sync: VaultSync,
    vault_path: Path,
) -> dict:
    """Rename a note and rewrite all inbound references.

    Returns a dict with: {refs_rewritten: int, sources: list[str]}.
    """
    old_canonical = _canonical(old_path)
    new_canonical = _canonical(new_path)

    old_file = vault_path / old_path
    new_file = vault_path / new_path
    if not old_file.exists():
        raise FileNotFoundError(f"Source not found: {old_path}")
    if new_file.exists():
        raise FileExistsError(f"Destination already exists: {new_path}")

    inbound = storage.find_inbound_refs(old_canonical)
    sources = sorted({rel["source_note"] for rel in inbound})

    for src in sources:
        src_file = vault_path / src
        if not src_file.exists():
            continue
        with note_write_lock(vault_path, src):
            text = src_file.read_text(encoding="utf-8")
            new_text = _rewrite_links_in_body(text, old_canonical, new_canonical)
            if new_text != text:
                atomic_write_text(src_file, new_text)
                sync.sync_one(src)

    new_file.parent.mkdir(parents=True, exist_ok=True)
    with note_write_lock(vault_path, new_path):
        old_file.rename(new_file)
        storage.delete_note(old_path)
        storage.delete_relations_by_source(old_path)
        sync.sync_one(new_path)

    return {"refs_rewritten": len(sources), "sources": sources}


def brain_delete(
    path: str,
    *,
    mode: str = "safe",
    storage: Storage,
    sync: VaultSync,
    vault_path: Path,
) -> dict:
    """Delete a note. `safe` mode refuses if inbound refs exist; `cascade` mode
    replaces inbound refs with strikethrough stubs.

    Returns: {sources_modified: list[str], file_deleted: bool}.
    """
    if mode not in ("safe", "cascade"):
        raise ValueError(f"mode must be 'safe' or 'cascade', got {mode!r}")

    canonical = _canonical(path)
    file_path = vault_path / path
    if not file_path.exists():
        raise FileNotFoundError(f"Not found: {path}")

    inbound = storage.find_inbound_refs(canonical)
    sources = sorted({rel["source_note"] for rel in inbound})

    if mode == "safe" and sources:
        raise DeleteBlockedError(
            f"refusing to delete {path} — {len(sources)} inbound ref(s): "
            + ", ".join(sources[:5])
            + (f" (+{len(sources) - 5} more)" if len(sources) > 5 else "")
        )

    modified: list[str] = []
    if mode == "cascade":
        for src in sources:
            src_file = vault_path / src
            if not src_file.exists():
                continue
            with note_write_lock(vault_path, src):
                text = src_file.read_text(encoding="utf-8")
                new_text = _replace_with_stub(text, canonical)
                if new_text != text:
                    atomic_write_text(src_file, new_text)
                    sync.sync_one(src)
                    modified.append(src)

    with note_write_lock(vault_path, path):
        file_path.unlink()
        storage.delete_note(path)
        storage.delete_relations_by_source(path)

    return {"sources_modified": modified, "file_deleted": True}
