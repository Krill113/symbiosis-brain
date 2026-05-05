from pathlib import Path

from symbiosis_brain.lint import VaultLinter
from symbiosis_brain.storage import Storage
from symbiosis_brain.sync import VaultSync


class TestVaultLinter:
    def test_orphan_note_detected(self, tmp_vault_with_taxonomy: Path, db_path: Path):
        """A note no one links to is reported as orphan (no incoming edges)."""
        (tmp_vault_with_taxonomy / "wiki" / "lonely.md").write_text(
            "---\ntitle: Lonely Note\ntype: wiki\nscope: global\n---\n\nNo links here at all.\n",
            encoding="utf-8",
        )
        storage = Storage(db_path)
        sync = VaultSync(tmp_vault_with_taxonomy, storage)
        sync.sync_all()

        linter = VaultLinter(storage, vault_path=tmp_vault_with_taxonomy)
        report = linter.lint()

        assert any(
            i["path"] == "wiki/lonely.md" for i in report["orphans"]
        ), f"Expected wiki/lonely.md in orphans, got {report['orphans']}"

    def test_weak_link_detected(self, tmp_vault_with_taxonomy: Path, db_path: Path):
        """A note with only 1 [[wiki-link]] is reported as weak."""
        (tmp_vault_with_taxonomy / "wiki" / "almost.md").write_text(
            "---\ntitle: Almost Connected\ntype: wiki\nscope: global\n---\n\nLinks to [[SomeEntity]] only.\n",
            encoding="utf-8",
        )
        storage = Storage(db_path)
        sync = VaultSync(tmp_vault_with_taxonomy, storage)
        sync.sync_all()

        linter = VaultLinter(storage, vault_path=tmp_vault_with_taxonomy)
        report = linter.lint()

        assert any(
            i["path"] == "wiki/almost.md" and i["link_count"] == 1
            for i in report["weak_links"]
        )

    def test_broken_link_detected(self, tmp_vault_with_taxonomy: Path, db_path: Path):
        """A [[wiki-link]] that doesn't match any note title is broken."""
        (tmp_vault_with_taxonomy / "wiki" / "referrer.md").write_text(
            "---\ntitle: Referrer\ntype: wiki\nscope: global\n---\n\nSee [[Existing]] and [[Ghost Note]].\n",
            encoding="utf-8",
        )
        (tmp_vault_with_taxonomy / "wiki" / "existing.md").write_text(
            "---\ntitle: Existing\ntype: wiki\nscope: global\n---\n\nI exist. See [[Referrer]].\n",
            encoding="utf-8",
        )
        storage = Storage(db_path)
        sync = VaultSync(tmp_vault_with_taxonomy, storage)
        sync.sync_all()

        linter = VaultLinter(storage, vault_path=tmp_vault_with_taxonomy)
        report = linter.lint()

        assert any(
            i["target"] == "Ghost Note" for i in report["broken_links"]
        ), f"Expected 'Ghost Note' in broken_links, got {report['broken_links']}"

    def test_healthy_note_not_flagged(self, tmp_vault_with_taxonomy: Path, db_path: Path):
        """A note with >= 2 wiki-links to existing notes has no issues."""
        # Post-normalization: resolver matches by filename/path, not title.
        # Link targets therefore use filename stems (good/alpha/beta), not titles.
        (tmp_vault_with_taxonomy / "wiki" / "good.md").write_text(
            "---\ntitle: Good Note\ntype: wiki\nscope: global\n---\n\nSee [[alpha]] and [[beta]].\n",
            encoding="utf-8",
        )
        (tmp_vault_with_taxonomy / "wiki" / "alpha.md").write_text(
            "---\ntitle: Alpha\ntype: wiki\nscope: global\n---\n\nI am [[good]] friend. Also [[beta]].\n",
            encoding="utf-8",
        )
        (tmp_vault_with_taxonomy / "wiki" / "beta.md").write_text(
            "---\ntitle: Beta\ntype: wiki\nscope: global\n---\n\nI am [[good]] friend. Also [[alpha]].\n",
            encoding="utf-8",
        )
        storage = Storage(db_path)
        sync = VaultSync(tmp_vault_with_taxonomy, storage)
        sync.sync_all()

        linter = VaultLinter(storage, vault_path=tmp_vault_with_taxonomy)
        report = linter.lint()

        assert not report["orphans"]
        assert not report["weak_links"]
        assert not report["broken_links"]

    def test_summary_counts(self, tmp_vault_with_taxonomy: Path, db_path: Path):
        """Summary includes total notes and issue counts."""
        (tmp_vault_with_taxonomy / "wiki" / "orphan.md").write_text(
            "---\ntitle: Orphan\ntype: wiki\nscope: global\n---\n\nNo links.\n",
            encoding="utf-8",
        )
        (tmp_vault_with_taxonomy / "wiki" / "linked.md").write_text(
            "---\ntitle: Linked\ntype: wiki\nscope: global\n---\n\nSee [[Orphan]] and [[Fake]].\n",
            encoding="utf-8",
        )
        storage = Storage(db_path)
        sync = VaultSync(tmp_vault_with_taxonomy, storage)
        sync.sync_all()

        linter = VaultLinter(storage, vault_path=tmp_vault_with_taxonomy)
        report = linter.lint()

        # taxonomy file is excluded from audit; only orphan.md + linked.md are counted.
        assert report["summary"]["total_notes"] == 2
        assert report["summary"]["orphan_count"] >= 1

    def test_case_insensitive_matching(self, tmp_vault_with_taxonomy: Path, db_path: Path):
        """[[Alpha-Seti]] matches note with stem 'alpha-seti' (case-insensitive)."""
        (tmp_vault_with_taxonomy / "projects" / "alpha-seti.md").write_text(
            "---\ntitle: Alpha-Seti\ntype: project\nscope: alpha\n---\n\nMain project. See [[Other]].\n",
            encoding="utf-8",
        )
        (tmp_vault_with_taxonomy / "wiki" / "other.md").write_text(
            "---\ntitle: Other\ntype: wiki\nscope: global\n---\n\nReferences [[Alpha-Seti]] and [[alpha-seti]].\n",
            encoding="utf-8",
        )
        storage = Storage(db_path)
        sync = VaultSync(tmp_vault_with_taxonomy, storage)
        sync.sync_all()

        linter = VaultLinter(storage, vault_path=tmp_vault_with_taxonomy)
        report = linter.lint()

        # Neither "Alpha-Seti" nor "alpha-seti" should be broken
        broken_targets = [i["target"] for i in report["broken_links"]]
        assert "Alpha-Seti" not in broken_targets
        assert "alpha-seti" not in broken_targets

    def test_alias_link_resolved(self, tmp_vault_with_taxonomy: Path, db_path: Path):
        """[[target|display]] resolves to target for broken link check."""
        (tmp_vault_with_taxonomy / "wiki" / "eco.md").write_text(
            "---\ntitle: Ecosystem Map\ntype: wiki\nscope: global\n---\n\nSee [[eco|Ecosystem Map]] and [[Missing]].\n",
            encoding="utf-8",
        )
        storage = Storage(db_path)
        sync = VaultSync(tmp_vault_with_taxonomy, storage)
        sync.sync_all()

        linter = VaultLinter(storage, vault_path=tmp_vault_with_taxonomy)
        report = linter.lint()

        broken_targets = [i["target"] for i in report["broken_links"]]
        # "eco|Ecosystem Map" should resolve to "eco" which matches the filename stem
        assert "eco|Ecosystem Map" not in broken_targets
        # "Missing" should still be broken
        assert "Missing" in broken_targets

    def test_scope_warning_emitted(self, tmp_vault_with_taxonomy: Path, db_path: Path):
        """A note with scope outside VALID_SCOPES is flagged in scope_warnings."""
        (tmp_vault_with_taxonomy / "wiki" / "bogus.md").write_text(
            "---\ntitle: Bogus Scope\ntype: wiki\nscope: bogus-thing\n---\n\nSee [[A]] and [[B]].\n",
            encoding="utf-8",
        )
        storage = Storage(db_path)
        sync = VaultSync(tmp_vault_with_taxonomy, storage)
        sync.sync_all()

        linter = VaultLinter(storage, vault_path=tmp_vault_with_taxonomy)
        report = linter.lint()

        assert any(
            i["path"] == "wiki/bogus.md" and i["scope"] == "bogus-thing"
            for i in report["scope_warnings"]
        ), f"Expected wiki/bogus.md in scope_warnings, got {report['scope_warnings']}"
        assert report["summary"]["scope_warning_count"] >= 1

    def test_valid_scope_no_warning(self, tmp_vault_with_taxonomy: Path, db_path: Path):
        """A note with scope in VALID_SCOPES produces no scope_warning."""
        (tmp_vault_with_taxonomy / "projects" / "ok.md").write_text(
            "---\ntitle: OK Scope\ntype: project\nscope: alpha-seti\n---\n\nSee [[A]] and [[B]].\n",
            encoding="utf-8",
        )
        storage = Storage(db_path)
        sync = VaultSync(tmp_vault_with_taxonomy, storage)
        sync.sync_all()

        linter = VaultLinter(storage, vault_path=tmp_vault_with_taxonomy)
        report = linter.lint()

        assert not any(
            i["path"] == "projects/ok.md" for i in report["scope_warnings"]
        ), f"Unexpected scope_warning for valid scope: {report['scope_warnings']}"

    def test_path_link_resolved(self, tmp_vault_with_taxonomy: Path, db_path: Path):
        """[[projects/symbiosis-brain]] matches note at that path."""
        (tmp_vault_with_taxonomy / "projects" / "symbiosis-brain.md").write_text(
            "---\ntitle: Symbiosis Brain\ntype: project\nscope: global\n---\n\nThe project. See [[Other]].\n",
            encoding="utf-8",
        )
        # Post-normalization: title-based match is no longer supported; use filename stem
        # ([[symbiosis-brain]]) or the full path ([[projects/symbiosis-brain]]).
        (tmp_vault_with_taxonomy / "wiki" / "ref.md").write_text(
            "---\ntitle: Reference\ntype: wiki\nscope: global\n---\n\nSee [[projects/symbiosis-brain]] and [[symbiosis-brain]].\n",
            encoding="utf-8",
        )
        storage = Storage(db_path)
        sync = VaultSync(tmp_vault_with_taxonomy, storage)
        sync.sync_all()

        linter = VaultLinter(storage, vault_path=tmp_vault_with_taxonomy)
        report = linter.lint()

        broken_targets = [i["target"] for i in report["broken_links"]]
        assert "projects/symbiosis-brain" not in broken_targets
        assert "symbiosis-brain" not in broken_targets

    def test_orphan_defined_by_absence_of_incoming_edges(
        self, tmp_vault_with_taxonomy: Path, db_path: Path
    ):
        """After W3 link-semantics: orphan = no one references this note.

        Note A has outgoing link to B, B has no outgoing links. Before Task 4,
        A was 'linked' (has outgoing), B was 'orphan' (no outgoing). After
        Task 4: A is orphan (no incoming), B is linked (A references it).
        """
        (tmp_vault_with_taxonomy / "wiki" / "a.md").write_text(
            "---\ntitle: A\ntype: wiki\nscope: global\n---\n\nSee [[wiki/b]].\n",
            encoding="utf-8",
        )
        (tmp_vault_with_taxonomy / "wiki" / "b.md").write_text(
            "---\ntitle: B\ntype: wiki\nscope: global\n---\n\nNo outgoing.\n",
            encoding="utf-8",
        )
        storage = Storage(db_path)
        VaultSync(tmp_vault_with_taxonomy, storage).sync_all()

        linter = VaultLinter(storage, vault_path=tmp_vault_with_taxonomy)
        result = linter.lint()
        orphan_paths = {o["path"] for o in result["orphans"]}
        assert "wiki/a.md" in orphan_paths, (
            f"A has no incoming edges, should be orphan. Got: {orphan_paths}"
        )
        assert "wiki/b.md" not in orphan_paths, (
            f"B has incoming edge from A, should NOT be orphan. Got: {orphan_paths}"
        )


class TestLintBrokenViaFlag:
    def test_broken_links_sourced_from_flag(self, tmp_path, tmp_vault_with_taxonomy: Path):
        """Broken-link detection uses ONLY the relations.broken flag, not name heuristics."""
        storage = Storage(tmp_path / "t.db")
        # Minimal note so linter iterates at least one source.
        storage._conn.execute(
            "INSERT INTO notes (path, title, content, note_type, scope, tags, "
            "frontmatter, created_at, updated_at) "
            "VALUES ('projects/src.md', 'Src', '', 'wiki', 'global', '[]', '{}', "
            "'2026-04-20T12:00:00+00:00', '2026-04-20T12:00:00+00:00')"
        )
        storage._conn.commit()

        storage.upsert_entity(name="projects/src")
        storage.upsert_entity(name="projects/foo")
        storage.upsert_entity(name="broken:nowhere")

        # Good link: broken=False
        storage.upsert_relation(
            from_name="projects/src",
            to_name="projects/foo",
            relation_type="references",
            source_note="projects/src.md",
            raw_target="projects/foo",
            broken=False,
        )
        # Broken link: broken=True
        storage.upsert_relation(
            from_name="projects/src",
            to_name="broken:nowhere",
            relation_type="references",
            source_note="projects/src.md",
            raw_target="nowhere",
            broken=True,
        )

        report = VaultLinter(storage, vault_path=tmp_vault_with_taxonomy).lint()
        broken = report["broken_links"]

        # Exactly ONE broken link, identified by flag=True
        assert len(broken) == 1, f"Expected 1 broken link, got {len(broken)}: {broken}"
        assert broken[0]["target"] == "nowhere", f"Expected target='nowhere', got {broken[0]['target']}"
        assert broken[0]["source"] == "projects/src.md", f"Expected source='projects/src.md', got {broken[0]['source']}"


def test_lint_loads_valid_scopes_from_taxonomy(tmp_path: Path, db_path: Path):
    (tmp_path / "reference").mkdir()
    (tmp_path / "reference" / "scope-taxonomy.md").write_text(
        "## Whitelist\n\n| scope | purpose |\n|---|---|\n"
        "| `global` | x |\n| `my-scope` | y |\n\n"
        "## Folder ↔ type convention\n\n"
        "| folder | type |\n|---|---|\n| `wiki/` | `wiki` |\n",
        encoding="utf-8",
    )
    storage = Storage(db_path)
    storage.upsert_note(
        path="wiki/x.md", title="X", content="", note_type="wiki",
        scope="unknown-scope", tags=[],
    )
    linter = VaultLinter(storage, vault_path=tmp_path)
    result = linter.lint()
    assert any(w["path"] == "wiki/x.md" for w in result["scope_warnings"])

    storage.upsert_note(
        path="wiki/y.md", title="Y", content="", note_type="wiki",
        scope="my-scope", tags=[],
    )
    result = linter.lint()
    assert not any(w["path"] == "wiki/y.md" for w in result["scope_warnings"])


class TestTypeFolderRule:
    def test_type_folder_drift_flagged(self, tmp_vault_with_taxonomy: Path, db_path: Path):
        storage = Storage(db_path)
        storage.upsert_note(
            path="feedback/x.md", title="X", content="", note_type="pattern",
            scope="global", tags=[],
        )
        linter = VaultLinter(storage, vault_path=tmp_vault_with_taxonomy)
        result = linter.lint()
        drift = result["type_drift"]
        assert len(drift) == 1
        assert drift[0]["path"] == "feedback/x.md"
        assert drift[0]["actual_type"] == "pattern"
        assert drift[0]["expected_type"] == "feedback"

    def test_type_folder_match_not_flagged(self, tmp_vault_with_taxonomy: Path, db_path: Path):
        storage = Storage(db_path)
        storage.upsert_note(
            path="decisions/x.md", title="X", content="", note_type="decision",
            scope="global", tags=[],
        )
        linter = VaultLinter(storage, vault_path=tmp_vault_with_taxonomy)
        assert linter.lint()["type_drift"] == []

    def test_type_folder_escape_hatch_skips_check(
        self, tmp_vault_with_taxonomy: Path, db_path: Path
    ):
        """Frontmatter `allow_type_mismatch: true` suppresses the drift report."""
        storage = Storage(db_path)
        storage.upsert_note(
            path="feedback/x.md", title="X", content="", note_type="pattern",
            scope="global", tags=[],
            frontmatter={"allow_type_mismatch": True},
        )
        linter = VaultLinter(storage, vault_path=tmp_vault_with_taxonomy)
        assert linter.lint()["type_drift"] == []

    def test_type_folder_root_note_skipped(
        self, tmp_vault_with_taxonomy: Path, db_path: Path
    ):
        """Notes at vault root (no folder segment) are not drift-checked."""
        storage = Storage(db_path)
        storage.upsert_note(
            path="CRITICAL_FACTS.md", title="CF", content="", note_type="wiki",
            scope="global", tags=[],
        )
        linter = VaultLinter(storage, vault_path=tmp_vault_with_taxonomy)
        assert linter.lint()["type_drift"] == []

    def test_type_folder_unknown_folder_skipped(
        self, tmp_vault_with_taxonomy: Path, db_path: Path
    ):
        """Notes in folders absent from the taxonomy map are not drift-checked."""
        storage = Storage(db_path)
        storage.upsert_note(
            path="unknown-folder/a.md", title="A", content="", note_type="wiki",
            scope="global", tags=[],
        )
        linter = VaultLinter(storage, vault_path=tmp_vault_with_taxonomy)
        assert linter.lint()["type_drift"] == []

    def test_type_folder_escape_hatch_survives_disk_roundtrip(
        self, tmp_vault_with_taxonomy: Path, db_path: Path
    ):
        """End-to-end: `allow_type_mismatch: true` in a .md frontmatter survives
        disk → VaultSync → Storage → VaultLinter and suppresses drift.

        Guards the Option-C choice (flag lives in JSON frontmatter blob, not a
        dedicated column): if sync.py stops persisting arbitrary frontmatter
        keys, this test fails loudly.
        """
        (tmp_vault_with_taxonomy / "feedback" / "mixed.md").write_text(
            "---\ntitle: Mixed\ntype: pattern\nscope: global\n"
            "allow_type_mismatch: true\n---\n\nIntentional drift.\n",
            encoding="utf-8",
        )
        storage = Storage(db_path)
        VaultSync(tmp_vault_with_taxonomy, storage).sync_all()

        linter = VaultLinter(storage, vault_path=tmp_vault_with_taxonomy)
        drift_paths = {d["path"] for d in linter.lint()["type_drift"]}
        assert "feedback/mixed.md" not in drift_paths, (
            f"Escape hatch must suppress drift on disk round-trip. "
            f"Got drift: {drift_paths}"
        )

    def test_type_folder_disk_drift_without_escape_hatch_flagged(
        self, tmp_vault_with_taxonomy: Path, db_path: Path
    ):
        """End-to-end: a drifted .md WITHOUT the escape hatch IS flagged after sync.

        Complement to the round-trip test above — proves the baseline works so
        the escape-hatch test isn't a vacuous pass.
        """
        (tmp_vault_with_taxonomy / "feedback" / "drifted.md").write_text(
            "---\ntitle: Drifted\ntype: pattern\nscope: global\n---\n\nNo hatch.\n",
            encoding="utf-8",
        )
        storage = Storage(db_path)
        VaultSync(tmp_vault_with_taxonomy, storage).sync_all()

        linter = VaultLinter(storage, vault_path=tmp_vault_with_taxonomy)
        drift_paths = {d["path"] for d in linter.lint()["type_drift"]}
        assert "feedback/drifted.md" in drift_paths, (
            f"Drifted note without escape hatch must be flagged. "
            f"Got drift: {drift_paths}"
        )


class TestGistRules:
    def test_gist_missing_warns(self, tmp_vault_with_taxonomy: Path, db_path: Path):
        storage = Storage(db_path)
        storage.upsert_note(
            path="patterns/no-gist.md", title="No Gist", scope="global",
            note_type="pattern",
            content="Body without gist",
            frontmatter={},  # no gist field
            tags=[],
        )
        linter = VaultLinter(storage, vault_path=tmp_vault_with_taxonomy)
        report = linter.lint()
        assert any(item["path"] == "patterns/no-gist.md" for item in report["gist_missing"])
        assert report["summary"]["gist_missing_count"] >= 1

    def test_gist_too_long_warns(self, tmp_vault_with_taxonomy: Path, db_path: Path):
        storage = Storage(db_path)
        long_gist = "x" * 105  # > 100 chars
        storage.upsert_note(
            path="patterns/long-gist.md", title="Long Gist", scope="global",
            note_type="pattern",
            content="Body",
            frontmatter={"gist": long_gist},
            tags=[],
        )
        linter = VaultLinter(storage, vault_path=tmp_vault_with_taxonomy)
        report = linter.lint()
        assert any(item["path"] == "patterns/long-gist.md" for item in report["gist_too_long"])

    def test_gist_equals_title_warns(self, tmp_vault_with_taxonomy: Path, db_path: Path):
        storage = Storage(db_path)
        storage.upsert_note(
            path="patterns/dup.md", title="Some Pattern", scope="global",
            note_type="pattern",
            content="Body",
            frontmatter={"gist": "Some Pattern"},
            tags=[],
        )
        linter = VaultLinter(storage, vault_path=tmp_vault_with_taxonomy)
        report = linter.lint()
        assert any(item["path"] == "patterns/dup.md" for item in report["gist_equals_title"])

    def test_gist_ok_no_warning(self, tmp_vault_with_taxonomy: Path, db_path: Path):
        storage = Storage(db_path)
        storage.upsert_note(
            path="patterns/ok.md", title="Something", scope="global",
            note_type="pattern",
            content="Body",
            frontmatter={"gist": "A useful one-line description that differs from title"},
            tags=[],
        )
        linter = VaultLinter(storage, vault_path=tmp_vault_with_taxonomy)
        report = linter.lint()
        assert not any(i["path"] == "patterns/ok.md" for i in report["gist_missing"])
        assert not any(i["path"] == "patterns/ok.md" for i in report["gist_too_long"])
        assert not any(i["path"] == "patterns/ok.md" for i in report["gist_equals_title"])
