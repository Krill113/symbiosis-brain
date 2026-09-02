"""<scope>/files/** holds raw materials (reports, scripts, assets), not notes.

The 2026-09 reorg reserves a ``files/`` path segment under any scope directory
for non-note artifacts. A .md file living below ``files/`` must be invisible to
both the indexer scan and the linter's not_indexed check — otherwise every
saved raw report would surface as a frontmatter-less pseudo-note.
"""

from pathlib import Path

from symbiosis_brain.lint import VaultLinter
from symbiosis_brain.storage import Storage
from symbiosis_brain.sync import VaultSync, is_material_path


def _write(p: Path, text: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


class TestIsMaterialPath:
    def test_files_segment_anywhere_is_material(self):
        assert is_material_path("acme-net/files/raw/2026-09-01-audit/report.md")
        assert is_material_path("global/files/tools/yt/README.md")
        assert is_material_path("files/loose.md")

    def test_regular_note_paths_are_not_material(self):
        assert not is_material_path("wiki/some-note.md")
        # a NOTE merely named files.md must stay indexed
        assert not is_material_path("acme-net/mistakes/files.md")
        assert not is_material_path("projects/files-convention.md")


class TestSyncSkipsMaterials:
    def test_md_under_files_not_ingested_and_not_failed(self, tmp_vault: Path, db_path: Path):
        _write(
            tmp_vault / "wiki" / "real-note.md",
            "---\ntitle: Real\ntype: wiki\nscope: global\ntags: []\n---\n\nBody.\n",
        )
        # raw material: no frontmatter at all — parsing it would either fail or
        # fabricate a default-scope note; both are wrong, it must be skipped
        _write(
            tmp_vault / "acme-net" / "files" / "raw" / "2026-09-01-x" / "report.md",
            "# Raw report\nplain markdown, no frontmatter\n",
        )
        storage = Storage(db_path)
        result = VaultSync(tmp_vault, storage).sync_all()

        paths = {n["path"] for n in storage.list_notes()}
        assert "wiki/real-note.md" in paths
        assert not any("/files/" in p or p.startswith("files/") for p in paths)
        assert result.failed == []

    def test_note_named_files_md_still_ingested(self, tmp_vault: Path, db_path: Path):
        _write(
            tmp_vault / "wiki" / "files.md",
            "---\ntitle: Files note\ntype: wiki\nscope: global\ntags: []\n---\n\nBody.\n",
        )
        storage = Storage(db_path)
        VaultSync(tmp_vault, storage).sync_all()
        assert "wiki/files.md" in {n["path"] for n in storage.list_notes()}


class TestLintIgnoresMaterials:
    def test_files_md_not_reported_as_not_indexed(
        self, tmp_vault_with_taxonomy: Path, db_path: Path
    ):
        _write(
            tmp_vault_with_taxonomy / "global" / "files" / "tools" / "note-like.md",
            "# Not a note\n",
        )
        storage = Storage(db_path)
        VaultSync(tmp_vault_with_taxonomy, storage).sync_all()
        report = VaultLinter(storage, vault_path=tmp_vault_with_taxonomy).lint()
        assert report["not_indexed"] == []
        assert report["summary"]["not_indexed_count"] == 0
