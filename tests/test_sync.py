from pathlib import Path
from symbiosis_brain.storage import Storage
from symbiosis_brain.sync import SyncResult, VaultSync


class TestVaultSync:
    def _make_sync(self, tmp_vault: Path, db_path: Path) -> VaultSync:
        storage = Storage(db_path)
        return VaultSync(vault_path=tmp_vault, storage=storage)

    def test_sync_ingests_new_file(self, tmp_vault: Path, db_path: Path, sample_note_content: str):
        (tmp_vault / "decisions" / "dapper.md").write_text(sample_note_content, encoding="utf-8")
        sync = self._make_sync(tmp_vault, db_path)
        stats = sync.sync_all()
        assert stats["added"] == 1
        note = sync.storage.get_note("decisions/dapper.md")
        assert note is not None
        assert note["title"] == "Dapper vs EF Core"

    def test_sync_extracts_entities_from_wikilinks(self, tmp_vault: Path, db_path: Path, sample_note_content: str):
        (tmp_vault / "decisions" / "dapper.md").write_text(sample_note_content, encoding="utf-8")
        sync = self._make_sync(tmp_vault, db_path)
        sync.sync_all()
        entities = sync.storage.list_entities()
        names = [e["name"] for e in entities]
        # Post-normalization, the resolver does case-insensitive basename match —
        # [[Dapper]] matches decisions/dapper.md; other wiki-links have no matching
        # file and are stored as broken:<raw> placeholders.
        assert "decisions/dapper" in names
        assert "broken:EF Core" in names
        assert "broken:Database Architecture" in names

    def test_sync_creates_relations(self, tmp_vault: Path, db_path: Path, sample_note_content: str):
        (tmp_vault / "decisions" / "dapper.md").write_text(sample_note_content, encoding="utf-8")
        sync = self._make_sync(tmp_vault, db_path)
        sync.sync_all()
        # from_name is now the canonical path (note path without .md), not the title.
        relations = sync.storage.get_relations("decisions/dapper", direction="outgoing")
        to_names = [r["to_name"] for r in relations]
        # [[Dapper]] resolves to decisions/dapper via case-insensitive basename match.
        assert "decisions/dapper" in to_names
        # Unresolved targets are stored as broken:<raw> placeholders.
        assert "broken:EF Core" in to_names

    def test_sync_removes_deleted_files(self, tmp_vault: Path, db_path: Path, sample_note_content: str):
        note_path = tmp_vault / "wiki" / "temp.md"
        note_path.write_text("---\ntitle: Temp\n---\nBody", encoding="utf-8")
        sync = self._make_sync(tmp_vault, db_path)
        sync.sync_all()
        assert sync.storage.get_note("wiki/temp.md") is not None
        note_path.unlink()
        stats = sync.sync_all()
        assert stats["removed"] >= 1
        assert sync.storage.get_note("wiki/temp.md") is None

    def test_sync_skips_unchanged_files(self, tmp_vault: Path, db_path: Path, sample_note_content: str):
        (tmp_vault / "wiki" / "stable.md").write_text("---\ntitle: Stable\n---\nBody", encoding="utf-8")
        sync = self._make_sync(tmp_vault, db_path)
        sync.sync_all()
        stats = sync.sync_all()
        assert stats["added"] == 0
        assert stats["updated"] == 0


class TestSyncWikilinkNormalization:
    def test_three_variants_collapse_to_one_relation(self, tmp_path):
        """[[foo]], [[foo|Foo]], [[foo\\|Foo]] must all resolve to same edge."""
        from symbiosis_brain.storage import Storage
        from symbiosis_brain.sync import VaultSync

        vault = tmp_path / "vault"
        (vault / "projects").mkdir(parents=True)
        (vault / "projects" / "foo.md").write_text(
            "---\ntitle: Foo\n---\n# Foo\ncontent", encoding="utf-8"
        )
        (vault / "projects" / "src.md").write_text(
            "---\ntitle: Src\n---\n"
            "# Src\n"
            "See [[projects/foo]], also [[projects/foo|Foo Project]], "
            r"and [[projects/foo\|Foo]]",
            encoding="utf-8",
        )

        s = Storage(tmp_path / "brain.db")
        VaultSync(vault, s).sync_all()

        rows = s._conn.execute(
            "SELECT from_name, to_name, label, broken FROM relations "
            "WHERE source_note=?",
            ("projects/src.md",),
        ).fetchall()
        assert len(rows) == 1
        row = rows[0]
        assert row["from_name"] == "projects/src"
        assert row["to_name"] == "projects/foo"
        assert row["broken"] == 0

    def test_broken_link_stored_with_flag(self, tmp_path):
        from symbiosis_brain.storage import Storage
        from symbiosis_brain.sync import VaultSync

        vault = tmp_path / "vault"
        (vault / "projects").mkdir(parents=True)
        (vault / "projects" / "src.md").write_text(
            "# Src\nLink to [[nonexistent-note]]", encoding="utf-8"
        )

        s = Storage(tmp_path / "brain.db")
        VaultSync(vault, s).sync_all()

        row = s._conn.execute(
            "SELECT to_name, broken, raw_target FROM relations WHERE source_note=?",
            ("projects/src.md",),
        ).fetchone()
        assert row is not None
        assert row["broken"] == 1
        assert row["to_name"].startswith("broken:")
        assert row["raw_target"] == "nonexistent-note"

    def test_from_name_is_canonical_path_not_title(self, tmp_path):
        from symbiosis_brain.storage import Storage
        from symbiosis_brain.sync import VaultSync

        vault = tmp_path / "vault"
        (vault / "projects").mkdir(parents=True)
        (vault / "projects" / "foo.md").write_text(
            "---\ntitle: Beautiful Foo Title\n---\n[[projects/bar]]",
            encoding="utf-8",
        )
        (vault / "projects" / "bar.md").write_text("# Bar", encoding="utf-8")

        s = Storage(tmp_path / "brain.db")
        VaultSync(vault, s).sync_all()

        row = s._conn.execute(
            "SELECT from_name FROM relations WHERE source_note=?",
            ("projects/foo.md",),
        ).fetchone()
        assert row["from_name"] == "projects/foo"


class TestForceReindexOnSchemaBump:
    def test_stale_relations_wiped_and_rebuilt(self, tmp_path):
        """Pre-existing relations with raw pipe text should disappear."""
        from symbiosis_brain.storage import Storage
        from symbiosis_brain.sync import VaultSync

        vault = tmp_path / "vault"
        (vault / "projects").mkdir(parents=True)
        (vault / "projects" / "foo.md").write_text("# Foo", encoding="utf-8")
        (vault / "projects" / "src.md").write_text(
            "[[projects/foo|Alias]]", encoding="utf-8"
        )

        s = Storage(tmp_path / "brain.db")
        s._conn.execute(
            "INSERT INTO relations (from_name, to_name, relation_type, "
            "source_note, created_at) VALUES (?, ?, ?, ?, ?)",
            ("Src Title", "projects/foo|Old Raw", "references",
             "projects/src.md", "2026-01-01T00:00:00+00:00"),
        )
        s._conn.execute(
            "UPDATE schema_version SET version=0 WHERE key=?",
            ("wikilink_normalization",),
        )
        s._conn.commit()

        VaultSync(vault, s).sync_all()

        rows = s._conn.execute(
            "SELECT from_name, to_name FROM relations"
        ).fetchall()
        assert all(r["to_name"] != "projects/foo|Old Raw" for r in rows)
        assert any(
            r["from_name"] == "projects/src" and r["to_name"] == "projects/foo"
            for r in rows
        )


def test_sync_all_returns_paths_added(tmp_vault: Path, db_path: Path):
    (tmp_vault / "wiki" / "alpha.md").write_text(
        "---\ntitle: Alpha\ntype: wiki\nscope: global\ntags: []\n---\n\nbody.\n",
        encoding="utf-8",
    )
    s = Storage(db_path)
    sync = VaultSync(tmp_vault, s)
    result = sync.sync_all()
    assert isinstance(result, SyncResult)
    assert result.added == ["wiki/alpha.md"]
    assert result.updated == []
    assert result.removed == []
    assert result.skipped == 0
    s.close()


def test_sync_all_returns_paths_updated_and_removed(tmp_vault: Path, db_path: Path):
    note = tmp_vault / "wiki" / "alpha.md"
    note.write_text("---\ntitle: A1\ntype: wiki\nscope: global\ntags: []\n---\n\nv1.\n",
                    encoding="utf-8")
    s = Storage(db_path)
    sync = VaultSync(tmp_vault, s)
    sync.sync_all()  # initial add

    # Update + add another + remove implicit (none yet)
    note.write_text("---\ntitle: A1\ntype: wiki\nscope: global\ntags: []\n---\n\nv2.\n",
                    encoding="utf-8")
    (tmp_vault / "wiki" / "beta.md").write_text(
        "---\ntitle: Beta\ntype: wiki\nscope: global\ntags: []\n---\n\nb.\n",
        encoding="utf-8",
    )
    result = sync.sync_all()
    assert sorted(result.added) == ["wiki/beta.md"]
    assert sorted(result.updated) == ["wiki/alpha.md"]
    assert result.removed == []

    # Now delete alpha + run again — alpha should appear in removed
    note.unlink()
    result = sync.sync_all()
    assert result.added == []
    assert result.updated == []
    assert result.removed == ["wiki/alpha.md"]
    s.close()
