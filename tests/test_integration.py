"""Smoke test: full workflow from markdown files to search results."""
from pathlib import Path

from symbiosis_brain.graph import GraphTraverser
from symbiosis_brain.scopes import ScopeResolver
from symbiosis_brain.search import SearchEngine
from symbiosis_brain.storage import Storage
from symbiosis_brain.sync import VaultSync
from symbiosis_brain.temporal import TemporalManager


def test_full_workflow(tmp_vault: Path, db_path: Path, sample_note_content: str):
    # Write files to vault
    (tmp_vault / "decisions" / "dapper.md").write_text(sample_note_content, encoding="utf-8")
    (tmp_vault / "projects" / "beta.md").write_text("""---
title: Beta Project
type: project
scope: beta
tags: [dotnet]
---

Desktop app using [[Dapper]] and [[WPF]].
""", encoding="utf-8")
    (tmp_vault / "wiki" / "wpf.md").write_text("""---
title: WPF
type: wiki
scope: global
tags: [ui, desktop]
---

Windows Presentation Foundation. Used in [[Beta Project]].
Implements [[MVVM]] pattern.
""", encoding="utf-8")

    # Sync
    storage = Storage(db_path)
    sync = VaultSync(tmp_vault, storage)
    stats = sync.sync_all()
    assert stats["added"] == 3

    # Search
    search = SearchEngine(storage)
    fts_results = search.search_fts("Dapper")
    assert len(fts_results) >= 1

    # Scoped search
    scoped = search.search_fts("dotnet", scope="beta")
    for note in scoped:
        assert note["scope"] in ("beta", "global")

    # Graph — post-wikilink-normalization, entities are canonical paths (not titles).
    # beta.md references [[Dapper]] and [[WPF]]; the resolver matches them by
    # case-insensitive basename to decisions/dapper.md and wiki/wpf.md respectively.
    graph = GraphTraverser(storage)
    context = graph.traverse("projects/beta", max_depth=2)
    neighbor_names = [n["name"] for n in context["neighbors"]]
    assert "decisions/dapper" in neighbor_names
    assert "wiki/wpf" in neighbor_names

    # Temporal
    temporal = TemporalManager(storage)
    for note in storage.list_notes():
        warning = temporal.staleness_warning(note)
        # All notes are fresh, no warnings expected
        assert warning is None
