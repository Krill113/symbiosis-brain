"""sync_all's vault walk selects exactly what `glob("**/*.md")` and the old
filters selected — on this platform and this Python — only cheaper.

The walk decides which notes exist. A walk that picks different files drops
notes out of the index, or lets foreign files in, silently and in every
session at once. So the reference here is the OLD code, verbatim, run by the
same interpreter on the same tree; the new walk must match it name for name.
The decisive cases are also pinned outright, so that a change made to both
sides at once cannot pass unnoticed.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from symbiosis_brain.storage import Storage
from symbiosis_brain.sync import (MD_GLOB, SKIP_FILES, VaultSync, _walk_vault,
                                  is_material_path)

NOTE = "---\ntitle: T\ntype: wiki\nscope: global\ngist: x\n---\n# T\n\nbody\n"

TREE = (
    "a.md", "Upper.MD", "mixed.Md", "notes.txt",
    "README.md", "CLAUDE.md", "MEMORY.md", "log.md",
    ".hidden-top.md", ".git/objects/ab/x.md", ".obsidian/y.md", ".index/z.md",
    "sub/b.md", "sub/.hidden/c.md", "sub/.dot.md", "sub/README.md", "sub/deeper/d.md",
    "sub/Files/cap.md",
    "files.md", "x/files.md", "a/files/raw.md", "a/b/files/c/deep.md", "files/top.md",
    "dir.md/inner.md",
)


def _old_selection(vault: Path) -> dict[str, Path]:
    """sync._sync_all's walk as it stood until 2026-09-11, verbatim."""
    disk_files: dict[str, Path] = {}
    for md_file in vault.glob(MD_GLOB):
        rel = md_file.relative_to(vault).as_posix()
        if md_file.name in SKIP_FILES:
            continue
        parts = rel.split("/")
        if parts[0].startswith("."):
            continue
        if is_material_path(rel):
            continue
        disk_files[rel] = md_file
    return disk_files


def _tree(root: Path) -> Path:
    for rel in TREE:
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(NOTE, encoding="utf-8")
    return root


def test_walk_selects_exactly_what_glob_selected(tmp_path):
    vault = _tree(tmp_path / "v")
    assert _walk_vault(vault) == _old_selection(vault)


def test_decisive_cases_are_pinned_outright(tmp_path):
    sel = set(_walk_vault(_tree(tmp_path / "v")))
    # a dot dir or dot file BELOW the top level is walked, as it always was
    assert {"sub/.hidden/c.md", "sub/.dot.md"} <= sel
    # anything under a top-level dot name is not
    assert not {".hidden-top.md", ".git/objects/ab/x.md", ".obsidian/y.md", ".index/z.md"} & sel
    # a `files` directory is material at any depth; a note merely NAMED files
    # stays, and the rule is case-sensitive, as is_material_path is
    assert not {"a/files/raw.md", "a/b/files/c/deep.md", "files/top.md"} & sel
    assert {"files.md", "x/files.md", "sub/Files/cap.md"} <= sel
    # SKIP_FILES at any depth
    assert not {"README.md", "CLAUDE.md", "MEMORY.md", "log.md", "sub/README.md"} & sel
    # a directory named like a note is an entry, because glob yields it
    assert {"dir.md", "dir.md/inner.md"} <= sel
    assert "notes.txt" not in sel
    # glob's platform default for case
    assert ("Upper.MD" in sel) is (os.name == "nt")
    assert ("mixed.Md" in sel) is (os.name == "nt")


def test_symlinked_directory_is_not_descended_and_symlinked_file_counts(tmp_path):
    vault = _tree(tmp_path / "v")
    try:
        os.symlink(vault / "sub", vault / "linkdir", target_is_directory=True)
        os.symlink(vault / "a.md", vault / "ln.md")
    except (OSError, NotImplementedError):
        pytest.skip("creating symlinks needs a privilege this machine does not grant")
    sel = _walk_vault(vault)
    assert sel == _old_selection(vault)
    assert "linkdir/b.md" not in sel and "ln.md" in sel


def test_sync_all_reads_path_and_hash_not_whole_rows(tmp_path, monkeypatch):
    """The walk's partner. list_notes() — every body, two JSON columns per note
    and an ORDER BY — was 71 of 298 ms of a no-change run and fed only Pass 0's
    empty-body guard, which now reads the single body it needs."""
    vault = tmp_path / "v"
    (vault / "wiki").mkdir(parents=True)
    (vault / "wiki" / "n.md").write_text(NOTE, encoding="utf-8")
    storage = Storage(tmp_path / "t.db")

    def whole_rows(*a, **k):
        raise AssertionError("sync_all must not load whole rows")
    monkeypatch.setattr(storage, "list_notes", whole_rows)
    sync = VaultSync(vault, storage)

    assert sync.sync_all().added == ["wiki/n.md"]
    (vault / "wiki" / "n.md").write_text(NOTE + "\nmore\n", encoding="utf-8")
    assert sync.sync_all().updated == ["wiki/n.md"]
