"""Scope-first vault layout: lint rules, rotation discovery, scaffolding.

The 2026-09 reorg moves notes from type-first (``mistakes/x.md``) to
scope-first (``<scope>/<type>/x.md``, flat ``<scope>/x.md`` below the
pair threshold, card at ``<scope>/<scope>.md``, handoffs in
``<scope>/archive/``). Both layouts stay valid during the transition:
legacy top-level type folders keep their old rule.
"""

from pathlib import Path

from symbiosis_brain.lint import VaultLinter
from symbiosis_brain.rotation import rotate_handoffs
from symbiosis_brain.storage import Storage
from symbiosis_brain.sync import VAULT_DIRS


def _note(storage, path, note_type, scope, gist="short gist"):
    storage.upsert_note(
        path=path, title=Path(path).stem, scope=scope, note_type=note_type,
        content="Body", frontmatter={"gist": gist}, tags=[],
    )


def _touch(vault: Path, rel: str) -> None:
    p = vault / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("---\ntitle: x\n---\nBody", encoding="utf-8")


class TestLintScopeFirstFolders:
    def test_scope_type_folder_drift(self, tmp_vault_with_taxonomy, db_path):
        storage = Storage(db_path)
        _note(storage, "alpha-seti/mistakes/wrong.md", "wiki", "alpha-seti")
        _note(storage, "alpha-seti/mistakes/right.md", "mistake", "alpha-seti")
        report = VaultLinter(storage, vault_path=tmp_vault_with_taxonomy).lint()
        drifted = {i["path"] for i in report["type_drift"]}
        assert "alpha-seti/mistakes/wrong.md" in drifted
        assert "alpha-seti/mistakes/right.md" not in drifted

    def test_flat_scope_note_has_no_folder_constraint(self, tmp_vault_with_taxonomy, db_path):
        storage = Storage(db_path)
        _note(storage, "alpha-seti/loose-note.md", "wiki", "alpha-seti")
        report = VaultLinter(storage, vault_path=tmp_vault_with_taxonomy).lint()
        assert not any(i["path"].startswith("alpha-seti/") for i in report["type_drift"])

    def test_card_must_be_project(self, tmp_vault_with_taxonomy, db_path):
        storage = Storage(db_path)
        _note(storage, "alpha-seti/alpha-seti.md", "wiki", "alpha-seti")
        report = VaultLinter(storage, vault_path=tmp_vault_with_taxonomy).lint()
        assert any(
            i["path"] == "alpha-seti/alpha-seti.md" and i["expected_type"] == "project"
            for i in report["type_drift"]
        )

    def test_scope_archive_expects_project(self, tmp_vault_with_taxonomy, db_path):
        storage = Storage(db_path)
        _note(storage, "alpha-seti/archive/alpha-seti-2026-01-01.md", "wiki", "alpha-seti")
        report = VaultLinter(storage, vault_path=tmp_vault_with_taxonomy).lint()
        assert any(
            i["path"].startswith("alpha-seti/archive/") and i["expected_type"] == "project"
            for i in report["type_drift"]
        )

    def test_legacy_top_level_rule_still_applies(self, tmp_vault_with_taxonomy, db_path):
        storage = Storage(db_path)
        _note(storage, "wiki/legacy-wrong.md", "mistake", "global")
        report = VaultLinter(storage, vault_path=tmp_vault_with_taxonomy).lint()
        assert any(i["path"] == "wiki/legacy-wrong.md" for i in report["type_drift"])

    def test_scope_folder_frontmatter_mismatch_reported(self, tmp_vault_with_taxonomy, db_path):
        storage = Storage(db_path)
        _note(storage, "alpha-seti/mistakes/stray.md", "mistake", "global")
        _note(storage, "alpha-seti/mistakes/fine.md", "mistake", "alpha-seti")
        report = VaultLinter(storage, vault_path=tmp_vault_with_taxonomy).lint()
        mismatched = {i["path"] for i in report["scope_folder_mismatch"]}
        assert "alpha-seti/mistakes/stray.md" in mismatched
        assert "alpha-seti/mistakes/fine.md" not in mismatched
        assert report["summary"]["scope_folder_mismatch_count"] == 1


class TestLintDbOrphans:
    def test_db_row_without_file_is_reported(self, tmp_vault_with_taxonomy, db_path):
        storage = Storage(db_path)
        _note(storage, "wiki/ghost.md", "wiki", "global")          # no file on disk
        _note(storage, "wiki/real.md", "wiki", "global")
        _touch(tmp_vault_with_taxonomy, "wiki/real.md")
        report = VaultLinter(storage, vault_path=tmp_vault_with_taxonomy).lint()
        assert [i["path"] for i in report["db_orphans"]] == ["wiki/ghost.md"]
        assert report["summary"]["db_orphans_count"] == 1


CARD = (
    "# Alpha Seti\n\n"
    "## Handoff 2020-01-05\n\n"
    "- **Shipped:** old stuff, see [[demo-docs/some-note]] for details\n"
    "- **Next-step:** nothing\n\n"
    "## Handoff 2020-01-06\n\n"
    "- **Shipped:** newer\n\n"
    "## Handoff 2020-01-07\n\n"
    "- **Shipped:** newest\n"
)


class TestRotationScopeFirst:
    def test_rotates_new_layout_card_into_scope_archive(self, tmp_path):
        vault = tmp_path
        card = vault / "alpha-seti" / "alpha-seti.md"
        card.parent.mkdir(parents=True)
        card.write_text(CARD, encoding="utf-8")

        report = rotate_handoffs(vault, scope="alpha-seti", inline_days=2)

        assert report.sections_archived == 1
        archived = list((vault / "alpha-seti" / "archive").glob("alpha-seti-2020-01-05*.md"))
        assert len(archived) == 1
        new_card = card.read_text(encoding="utf-8")
        assert "[[alpha-seti/archive/alpha-seti-2020-01-05" in new_card
        # legacy shared archive must NOT be created for a new-layout card
        assert not (vault / "archive" / "handoffs").exists()

    def test_autodiscovery_finds_new_layout_cards(self, tmp_path):
        vault = tmp_path
        card = vault / "alpha-seti" / "alpha-seti.md"
        card.parent.mkdir(parents=True)
        card.write_text(CARD, encoding="utf-8")

        report = rotate_handoffs(vault, inline_days=2)
        assert report.cards_processed == 1
        assert report.sections_archived == 1

    def test_legacy_projects_card_keeps_legacy_archive(self, tmp_path):
        vault = tmp_path
        card = vault / "projects" / "alpha-seti.md"
        card.parent.mkdir(parents=True)
        card.write_text(CARD, encoding="utf-8")

        report = rotate_handoffs(vault, scope="alpha-seti", inline_days=2)
        assert report.sections_archived == 1
        assert list((vault / "archive" / "handoffs").glob("alpha-seti-2020-01-05*.md"))

    def test_new_layout_archive_footer_links_to_scope_first_card(self, tmp_path):
        """The archive footer must point back at the card that actually exists.

        A scope-first vault has no root projects/ folder, so a footer hardcoded
        to [[projects/<scope>]] writes a broken wiki-link into every handoff
        archived after the 2026-09 reorg.
        """
        vault = tmp_path
        card = vault / "alpha-seti" / "alpha-seti.md"
        card.parent.mkdir(parents=True)
        card.write_text(CARD, encoding="utf-8")

        rotate_handoffs(vault, scope="alpha-seti", inline_days=2)

        archived = list((vault / "alpha-seti" / "archive").glob("alpha-seti-2020-01-05*.md"))
        footer = archived[0].read_text(encoding="utf-8")
        assert "[[alpha-seti/alpha-seti]]" in footer
        assert "[[projects/alpha-seti]]" not in footer

    def test_legacy_layout_archive_footer_still_links_to_legacy_card(self, tmp_path):
        """A pre-migration vault must keep linking to projects/<scope>.md."""
        vault = tmp_path
        card = vault / "projects" / "alpha-seti.md"
        card.parent.mkdir(parents=True)
        card.write_text(CARD, encoding="utf-8")

        rotate_handoffs(vault, scope="alpha-seti", inline_days=2)

        archived = list((vault / "archive" / "handoffs").glob("alpha-seti-2020-01-05*.md"))
        footer = archived[0].read_text(encoding="utf-8")
        assert "[[projects/alpha-seti]]" in footer


class TestScaffold:
    def test_vault_dirs_include_global_scope_tree(self):
        assert "global/mistakes" in VAULT_DIRS
        assert "global/patterns" in VAULT_DIRS
        # legacy dirs stay during the transition
        assert "wiki" in VAULT_DIRS
