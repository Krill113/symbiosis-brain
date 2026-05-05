import pytest
from pathlib import Path
from symbiosis_brain.storage import Storage
from symbiosis_brain.search import SearchEngine


@pytest.fixture
def search_engine(db_path: Path) -> SearchEngine:
    storage = Storage(db_path)
    storage.upsert_note(path="d/dapper.md", title="Dapper Choice", scope="beta", note_type="decision",
                        content="Chose Dapper over EF Core for performance on large datasets", tags=["orm"])
    storage.upsert_note(path="w/logging.md", title="Logging Setup", scope="global", note_type="wiki",
                        content="Use Serilog with structured logging to Elasticsearch", tags=["logging"])
    storage.upsert_note(path="w/efcore.md", title="EF Core Patterns", scope="global", note_type="wiki",
                        content="Entity Framework Core patterns: repository, unit of work, migrations", tags=["orm"])
    engine = SearchEngine(storage)
    # Index all notes into vector store so hybrid search can use embeddings
    engine.index_all()
    return engine


class TestFTSSearch:
    def test_finds_by_keyword(self, search_engine: SearchEngine):
        results = search_engine.search_fts("Dapper")
        assert len(results) >= 1
        assert results[0]["title"] == "Dapper Choice"

    def test_finds_by_content_keyword(self, search_engine: SearchEngine):
        results = search_engine.search_fts("Serilog")
        assert len(results) >= 1
        assert results[0]["title"] == "Logging Setup"

    def test_respects_scope_filter(self, search_engine: SearchEngine):
        # scope="beta" matches notes where scope IN ('beta', 'global')
        # so both "Dapper Choice" (scope=beta) and "EF Core Patterns" (scope=global) are returned
        results = search_engine.search_fts("orm", scope="beta")
        titles = [r["title"] for r in results]
        assert "Dapper Choice" in titles
        # EF Core Patterns is global, so it IS visible from scope="beta"
        assert "EF Core Patterns" in titles
        # Logging Setup is global but tagged "logging", not "orm" — may or may not appear
        # (FTS5 with porter stemmer: "logging" stem matches "logging" tag, not "orm")


class TestHybridSearch:
    def test_hybrid_returns_results(self, search_engine: SearchEngine):
        results = search_engine.search("database ORM choice")
        assert len(results) >= 1

    def test_hybrid_ranks_relevant_higher(self, search_engine: SearchEngine):
        results = search_engine.search("why did we choose Dapper")
        assert len(results) >= 1
        assert results[0]["title"] == "Dapper Choice"


class TestScopeBoost:
    @pytest.fixture
    def scoped_engine(self, db_path: Path) -> SearchEngine:
        storage = Storage(db_path)
        storage.upsert_note(
            path="global/foo.md", title="Foo Global", scope="global",
            note_type="wiki",
            content="Valve sizing rules for water networks", tags=[],
        )
        storage.upsert_note(
            path="alpha-seti/foo.md", title="Foo Seti", scope="alpha-seti",
            note_type="wiki",
            content="Valve sizing rules for water networks", tags=[],
        )
        engine = SearchEngine(storage)
        # Guard: scope-boost is a hybrid concern. If the vector backend is
        # missing in this environment, the test degenerates into an FTS-only
        # check and may pass for the wrong reason. Fail loudly instead.
        assert engine._vec_enabled, (
            "Vector backend unavailable — scope-boost test requires hybrid search"
        )
        engine.index_all()
        return engine

    def test_scope_specific_outranks_global_on_equal_match(
        self, scoped_engine: SearchEngine
    ):
        """Two notes with identical text, different scopes. With scope filter set
        to the specific scope, the scope-matched note must rank above the global
        one."""
        results = scoped_engine.search("valve sizing water", scope="alpha-seti", limit=5)
        paths = [r["path"] for r in results]
        assert "alpha-seti/foo.md" in paths and "global/foo.md" in paths, (
            f"Both notes should appear in results; got {paths}"
        )
        assert paths.index("alpha-seti/foo.md") < paths.index("global/foo.md"), (
            f"Scope-specific note must rank above global match. Order: {paths}"
        )

    @pytest.mark.parametrize("scope", [None, "global"])
    def test_no_boost_for_none_or_global_scope(
        self, scoped_engine: SearchEngine, scope
    ):
        """Regression guard: `if scope and scope != "global"` must not invoke
        the boost branch for either falsy scope. Smoke-level — verifies the
        call doesn't crash and the global note is returned in both cases.
        (With scope='global' the storage filter excludes alpha-seti; with
        scope=None both scopes are visible.)"""
        results = scoped_engine.search("valve sizing water", scope=scope, limit=5)
        paths = {r["path"] for r in results}
        assert "global/foo.md" in paths


class TestGistMode:
    @pytest.fixture
    def gist_engine(self, db_path: Path) -> SearchEngine:
        storage = Storage(db_path)
        # Note WITH gist field
        storage.upsert_note(
            path="patterns/cad-spawn.md",
            title="CAD Spawn Pattern",
            scope="alpha-seti",
            note_type="pattern",
            content="## Body\n\nLong body text about spawning CAD commands from background threads via DataModel.",
            frontmatter={"gist": "Spawn CAD commands from bg via DataModel — async without UI thread leaks"},
            tags=["cad"],
        )
        # Note WITHOUT gist (fallback case)
        storage.upsert_note(
            path="decisions/no-gist.md",
            title="Decision Without Gist",
            scope="alpha-seti",
            note_type="decision",
            content="# Heading\n\nFirst paragraph here that is fairly short.\n\nSecond paragraph longer.",
            frontmatter={},
            tags=[],
        )
        engine = SearchEngine(storage)
        engine.index_all()
        return engine

    def test_gist_mode_returns_gist_field_when_present(self, gist_engine: SearchEngine):
        results = gist_engine.search("CAD spawn", scope="alpha-seti", limit=2, mode="gist")
        cad = next((r for r in results if r["path"] == "patterns/cad-spawn.md"), None)
        assert cad is not None
        assert cad["gist"] == "Spawn CAD commands from bg via DataModel — async without UI thread leaks"

    def test_gist_mode_falls_back_to_first_paragraph(self, gist_engine: SearchEngine):
        results = gist_engine.search("decision", scope="alpha-seti", limit=2, mode="gist")
        no_gist = next((r for r in results if r["path"] == "decisions/no-gist.md"), None)
        assert no_gist is not None
        # Fallback: first non-empty paragraph after heading, ≤80 chars
        assert no_gist["gist"].startswith("First paragraph")
        assert len(no_gist["gist"]) <= 80

    def test_gist_mode_default_is_preview(self, gist_engine: SearchEngine):
        results = gist_engine.search("CAD spawn", scope="alpha-seti", limit=2)  # no mode
        cad = next((r for r in results if r["path"] == "patterns/cad-spawn.md"), None)
        assert cad is not None
        assert "content" in cad  # preview mode keeps full content
        assert "gist" not in cad  # default mode does NOT add gist key

    def test_gist_mode_fallback_skips_residual_frontmatter(self, gist_engine: SearchEngine):
        """Regression test: if the stored content still contains a frontmatter block
        (e.g. parser failed to strip it), the fallback must not return YAML keys."""
        from symbiosis_brain.search import _extract_fallback_gist
        raw = "---\ntitle: X\ntags: [a, b]\n---\n\n# Heading\n\nThe real first paragraph here."
        result = _extract_fallback_gist(raw)
        assert "title:" not in result
        assert "tags:" not in result
        assert result.startswith("The real first paragraph")
