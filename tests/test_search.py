import sqlite3

import numpy as np
import pytest
from pathlib import Path
from symbiosis_brain.storage import Storage
from symbiosis_brain.search import SearchEngine
from symbiosis_brain.sync import VaultSync


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

    def test_hybrid_attaches_score_and_in_both(self, search_engine: SearchEngine):
        """Stage 0: search() attaches _score (post-boost RRF) and _in_both
        (returned by both FTS and vector) to every hit. _score-sorted desc."""
        results = search_engine.search("database ORM choice", limit=5)
        assert results
        scores = [r["_score"] for r in results]
        assert all(isinstance(s, float) for s in scores)
        assert scores == sorted(scores, reverse=True)
        assert all(isinstance(r["_in_both"], bool) for r in results)

    def test_hybrid_score_in_both_present_in_gist_mode(self, search_engine: SearchEngine):
        results = search_engine.search("Dapper", limit=5, mode="gist")
        assert results
        for r in results:
            assert "_score" in r and "_in_both" in r
            assert "gist" in r  # gist mode still populated

    def test_hybrid_in_both_true_for_note_matched_both_ways(self, search_engine: SearchEngine):
        # Single keyword present in content (FTS) AND semantically central (vector)
        # → surfaces in both → _in_both True. NB: a multi-token natural-language
        # query (e.g. "why did we choose Dapper") would be VECTOR-ONLY because FTS5
        # ANDs tokens — which is exactly why _in_both is a label, never a drop-gate
        # (see [[decisions/2026-06-03-recall-behavior]]).
        if not search_engine._vec_enabled:
            pytest.skip("vector backend unavailable — _in_both requires hybrid")
        results = search_engine.search("Dapper", limit=5)
        dapper = next((r for r in results if r["path"] == "d/dapper.md"), None)
        assert dapper is not None
        assert dapper["_in_both"] is True

    def test_hybrid_fts_only_emits_capped_when_vector_disabled(self, search_engine, monkeypatch):
        """Locked constraint: cold/disabled vector → recall must still emit
        cap-top-N (FTS-only), NEVER strong-only. All hits _in_both False, no ★."""
        monkeypatch.setattr(search_engine, "_vec_enabled", False)
        results = search_engine.search("Dapper", limit=5)
        assert results  # FTS-only still emits — not gated to empty
        assert all(r["_in_both"] is False for r in results)
        assert all(isinstance(r["_score"], float) for r in results)


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


def test_delete_vec_removes_single_path(tmp_vault: Path, db_path: Path):
    s = Storage(db_path)
    se = SearchEngine(s)
    if not se._vec_enabled:
        pytest.skip("sqlite-vec not available")
    # Manually insert two vec rows
    import numpy as np
    emb = np.zeros(384, dtype=np.float32).tobytes()
    s._conn.execute("INSERT INTO notes_vec (path, embedding) VALUES ('a.md', ?)", (emb,))
    s._conn.execute("INSERT INTO notes_vec (path, embedding) VALUES ('b.md', ?)", (emb,))
    s._conn.commit()
    assert s._conn.execute("SELECT COUNT(*) FROM notes_vec").fetchone()[0] == 2

    se.delete_vec("a.md")
    rows = s._conn.execute("SELECT path FROM notes_vec ORDER BY path").fetchall()
    assert [r[0] for r in rows] == ["b.md"]
    s.close()


def test_is_index_dirty_detects_count_drift(tmp_vault: Path, db_path: Path):
    note = tmp_vault / "wiki" / "n1.md"
    note.write_text("---\ntitle: N1\ntype: wiki\nscope: global\ntags: []\n---\n\nbody.\n",
                    encoding="utf-8")
    s = Storage(db_path)
    sync = VaultSync(tmp_vault, s)
    sync.sync_all()
    se = SearchEngine(s)
    if not se._vec_enabled:
        pytest.skip("sqlite-vec not available")

    # Index is empty (count=0), notes table has 1 → dirty
    assert se.is_index_dirty() is True

    # Index it
    se.index_all()
    assert se.is_index_dirty() is False

    # Now delete a vec row to simulate drift
    s._conn.execute("DELETE FROM notes_vec WHERE path='wiki/n1.md'")
    s._conn.commit()
    assert se.is_index_dirty() is True
    s.close()


def test_search_engine_exposes_model_name(tmp_vault: Path, db_path: Path):
    from symbiosis_brain.search import SearchEngine, _MODEL_NAME
    assert _MODEL_NAME == "BAAI/bge-small-en-v1.5"
    s = Storage(db_path)
    se = SearchEngine(s)
    assert se._model_name == _MODEL_NAME
    s.close()


def test_get_embedder_creates_lockfile_during_init(tmp_path, monkeypatch):
    """During fastembed init, a lockfile exists at LOCK_DIR. After init, gone."""
    from symbiosis_brain import search as _search_mod
    monkeypatch.setattr(_search_mod, "LOCK_DIR", tmp_path)
    # Reset module singleton
    monkeypatch.setattr(_search_mod, "_embedder", None)

    e = _search_mod._get_embedder()
    assert e is not None
    # After init, lockfile is removed
    lockfile = tmp_path / "sb-fastembed-init.lock"
    assert not lockfile.exists()


def test_get_embedder_skips_lock_when_already_loaded(tmp_path, monkeypatch):
    """If _embedder is already non-None, _get_embedder returns immediately
    without touching the lockfile."""
    from symbiosis_brain import search as _search_mod
    monkeypatch.setattr(_search_mod, "LOCK_DIR", tmp_path)
    sentinel = object()
    monkeypatch.setattr(_search_mod, "_embedder", sentinel)
    # Pre-create a fresh lockfile to assert it's NOT touched
    (tmp_path / "sb-fastembed-init.lock").write_text("99999\n0\n")

    result = _search_mod._get_embedder()
    assert result is sentinel  # fast path returned the cached singleton


# ============ embedder-switchable: З1 normalize / З2 dim / З3 model
# source-of-truth / З4 prefixes. Model in tests: mocked, never downloaded. ==

class TestL2Normalize:
    """З1: normalization lives in one place, both paths pass through it."""

    def test_non_zero_vector_becomes_unit(self):
        from symbiosis_brain.search import _l2_normalize
        v = np.array([3.0, 4.0], dtype=np.float32)  # norm 5
        out = _l2_normalize(v)
        assert out == pytest.approx([0.6, 0.8])
        assert float(np.linalg.norm(out)) == pytest.approx(1.0)

    def test_zero_vector_returned_unchanged_no_division_by_zero(self):
        from symbiosis_brain.search import _l2_normalize
        v = np.zeros(8, dtype=np.float32)
        out = _l2_normalize(v)  # must not raise / produce NaN
        assert np.array_equal(out, v)
        assert not np.isnan(out).any()

    def test_already_unit_vector_is_unchanged_by_a_second_pass(self):
        from symbiosis_brain.search import _l2_normalize
        v = np.array([1.0, 0.0, 0.0], dtype=np.float32)
        once = _l2_normalize(v)
        twice = _l2_normalize(once)
        assert twice == pytest.approx(once, abs=1e-7)


class TestEmbedDocumentsAndQueryNormalize:
    def test_embed_documents_output_is_unit_norm(self, monkeypatch):
        """The embedder itself is mocked (at _get_embedder, below _embed's own
        normalization step) to return NON-unit vectors — a passing assertion
        here proves _embed (called by _embed_documents) rescaled them, not
        that they happened to already be unit."""
        from symbiosis_brain import search as sb_search

        class StubEmbedder:
            def embed(self, texts, **kwargs):
                for _ in texts:
                    yield np.array([3.0, 4.0], dtype=np.float32)  # norm 5

        monkeypatch.setattr(sb_search, "_get_embedder", lambda: StubEmbedder())
        out = sb_search._embed_documents(["a", "b"], "BAAI/bge-small-en-v1.5")
        for vec in out:
            assert float(np.linalg.norm(vec)) == pytest.approx(1.0)

    def test_embed_query_output_is_unit_norm(self, monkeypatch):
        from symbiosis_brain import search as sb_search

        class StubEmbedder:
            def embed(self, texts, **kwargs):
                for _ in texts:
                    yield np.array([3.0, 4.0], dtype=np.float32)

        monkeypatch.setattr(sb_search, "_get_embedder", lambda: StubEmbedder())
        out = sb_search._embed_query("q", "BAAI/bge-small-en-v1.5")
        assert float(np.linalg.norm(out)) == pytest.approx(1.0)

    def test_search_vector_ranking_unaffected_by_normalize_when_input_already_unit(
            self, db_path: Path, monkeypatch):
        """For an embedder that already returns unit vectors (bge-small's real
        behaviour), normalizing again must not perturb the ranking search_vector
        produces — the whole point of normalizing on write AND read is that
        relative order is preserved when nothing needed rescaling."""
        from symbiosis_brain import search as sb_search

        storage = Storage(db_path)
        engine = SearchEngine(storage)
        if not engine._vec_enabled:
            pytest.skip("sqlite-vec unavailable")

        dim = 384  # the default model's table dimension
        vectors = {
            "a.md": [1.0] + [0.0] * (dim - 1),          # closest to query
            "b.md": [0.9, 0.1] + [0.0] * (dim - 2),       # second
            "c.md": [0.0, 1.0] + [0.0] * (dim - 2),       # farthest
        }
        for path, vec in vectors.items():
            storage.upsert_note(path=path, title=path, scope="global",
                                note_type="wiki", content="body", tags=[])
        for path, vec in vectors.items():
            unit = (np.array(vec) / np.linalg.norm(vec)).tolist()
            storage._conn.execute(
                "INSERT INTO notes_vec (path, embedding) VALUES (?, ?)",
                (path, np.array(unit, dtype=np.float32).tobytes()),
            )
        storage._conn.commit()

        # Query embedder already returns a unit vector too.
        monkeypatch.setattr(sb_search, "_embed_one", lambda text: [1.0] + [0.0] * (dim - 1))

        results = engine.search_vector("q", limit=3)
        assert [r["path"] for r in results] == ["a.md", "b.md", "c.md"]
        storage.close()


class TestEmbeddingDimension:
    """З2: notes_vec's vector width follows the current model."""

    def test_default_model_dimension_is_384(self, db_path: Path):
        storage = Storage(db_path)
        engine = SearchEngine(storage)
        if not engine._vec_enabled:
            pytest.skip("sqlite-vec unavailable")
        declared = storage._conn.execute(
            "SELECT sql FROM sqlite_master WHERE name='notes_vec'").fetchone()[0]
        assert "FLOAT[384]" in declared
        storage.close()

    def test_ensure_vec_table_uses_a_wider_real_model(self, db_path: Path):
        """paraphrase-multilingual-mpnet-base-v2 is dim 768 (fastembed metadata
        lookup — no weights loaded, no network)."""
        storage = Storage(db_path)
        engine = SearchEngine(
            storage, model_name="sentence-transformers/paraphrase-multilingual-mpnet-base-v2")
        if not engine._vec_enabled:
            pytest.skip("sqlite-vec unavailable")
        declared = storage._conn.execute(
            "SELECT sql FROM sqlite_master WHERE name='notes_vec'").fetchone()[0]
        assert "FLOAT[768]" in declared
        storage.close()

    def test_unknown_model_falls_back_to_default_dimension_and_logs(
            self, db_path: Path, caplog):
        storage = Storage(db_path)
        with caplog.at_level("ERROR", logger="symbiosis-brain.search"):
            engine = SearchEngine(storage, model_name="fixture/no-such-model")
        if not engine._vec_enabled:
            pytest.skip("sqlite-vec unavailable")
        declared = storage._conn.execute(
            "SELECT sql FROM sqlite_master WHERE name='notes_vec'").fetchone()[0]
        assert "FLOAT[384]" in declared
        assert any("fixture/no-such-model" in r.message for r in caplog.records)
        storage.close()

    def test_recreate_vec_table_redeclares_for_the_new_model(self, db_path: Path):
        storage = Storage(db_path)
        engine = SearchEngine(storage)
        if not engine._vec_enabled:
            pytest.skip("sqlite-vec unavailable")
        storage._conn.execute(
            "INSERT INTO notes_vec (path, embedding) VALUES (?, ?)",
            ("a.md", np.zeros(384, dtype=np.float32).tobytes()),
        )
        storage._conn.commit()

        engine.model_name = "sentence-transformers/paraphrase-multilingual-mpnet-base-v2"
        engine._recreate_vec_table()

        declared = storage._conn.execute(
            "SELECT sql FROM sqlite_master WHERE name='notes_vec'").fetchone()[0]
        assert "FLOAT[768]" in declared
        assert storage._conn.execute("SELECT COUNT(*) FROM notes_vec").fetchone()[0] == 0
        storage.close()

    def test_recreate_vec_table_is_one_explicit_transaction(self, db_path: Path, monkeypatch):
        """DROP + CREATE must be BEGIN IMMEDIATE ... COMMIT, never two
        autocommits — a kill between them must never leave notes_vec absent."""
        storage = Storage(db_path)
        engine = SearchEngine(storage)
        if not engine._vec_enabled:
            pytest.skip("sqlite-vec unavailable")

        # sqlite3.Connection.execute is a read-only slot — can't monkeypatch
        # an instance attribute directly — so wrap the connection instead.
        real_conn = storage._conn
        calls: list[str] = []

        class _ConnSpy:
            def execute(self, sql, *a, **kw):
                calls.append(sql.strip().split("\n")[0].strip())
                return real_conn.execute(sql, *a, **kw)

            def __getattr__(self, name):
                return getattr(real_conn, name)

        monkeypatch.setattr(storage, "_conn", _ConnSpy())
        engine._recreate_vec_table()

        assert calls[0] == "BEGIN IMMEDIATE"
        assert calls[-1] == "COMMIT"
        assert "DROP TABLE IF EXISTS notes_vec" in calls
        storage.close()


class TestModelSourceOfTruth:
    """З3: schema_version.embedding_model is authoritative; env is never read
    inside SearchEngine — only server.py applies it, as a request."""

    def test_resolve_model_name_prefers_db_over_default(self, db_path: Path):
        from symbiosis_brain.search import _resolve_model_name
        storage = Storage(db_path)
        storage.set_schema_version("embedding_model", "fixture/from-db")
        assert _resolve_model_name(storage) == "fixture/from-db"
        storage.close()

    def test_resolve_model_name_defaults_when_db_empty(self, db_path: Path):
        from symbiosis_brain.search import _resolve_model_name, _MODEL_NAME
        storage = Storage(db_path)
        assert _resolve_model_name(storage) == _MODEL_NAME
        storage.close()

    def test_search_engine_ignores_the_env_var(self, db_path: Path, monkeypatch):
        """SYMBIOSIS_BRAIN_EMBED_MODEL is a request server.py applies — never
        read directly by SearchEngine (hook/CLI subprocesses must never race
        ahead of whatever the server has actually migrated the index to)."""
        from symbiosis_brain.search import _MODEL_NAME
        monkeypatch.setenv("SYMBIOSIS_BRAIN_EMBED_MODEL", "fixture/should-be-ignored")
        storage = Storage(db_path)
        engine = SearchEngine(storage)
        assert engine.model_name == _MODEL_NAME
        storage.close()

    def test_explicit_model_name_overrides_db(self, db_path: Path):
        """The constructor override is for server.py's own migration code and
        tools/eval_search.py, not for env — see the two tests above."""
        storage = Storage(db_path)
        storage.set_schema_version("embedding_model", "fixture/from-db")
        engine = SearchEngine(storage, model_name="fixture/explicit")
        assert engine.model_name == "fixture/explicit"
        storage.close()


class TestModelPrefixes:
    """З4: query/doc text get different, model-specific prefixes."""

    def test_bge_small_has_no_prefix(self):
        from symbiosis_brain.search import _model_prefixes
        assert _model_prefixes("BAAI/bge-small-en-v1.5") == ("", "")

    def test_mpnet_has_no_prefix(self):
        from symbiosis_brain.search import _model_prefixes
        assert _model_prefixes(
            "sentence-transformers/paraphrase-multilingual-mpnet-base-v2") == ("", "")

    def test_e5_large_has_query_and_passage_prefixes(self):
        from symbiosis_brain.search import _model_prefixes
        assert _model_prefixes("intfloat/multilingual-e5-large") == ("query: ", "passage: ")

    def test_unknown_model_has_no_prefix(self):
        from symbiosis_brain.search import _model_prefixes
        assert _model_prefixes("fixture/unknown-model") == ("", "")

    def test_embed_documents_prefixes_e5_large_texts(self, monkeypatch):
        from symbiosis_brain import search as sb_search

        received: list[list[str]] = []

        def fake_embed(texts):
            texts = list(texts)
            received.append(texts)
            return [[1.0, 0.0] for _ in texts]

        monkeypatch.setattr(sb_search, "_embed", fake_embed)
        sb_search._embed_documents(["note body"], "intfloat/multilingual-e5-large")
        assert received[-1] == ["passage: note body"]

    def test_embed_query_prefixes_e5_large_text_differently_from_documents(self, monkeypatch):
        from symbiosis_brain import search as sb_search

        received: list[str] = []

        def fake_embed_one(text):
            received.append(text)
            return [1.0, 0.0]

        monkeypatch.setattr(sb_search, "_embed_one", fake_embed_one)
        sb_search._embed_query("a question", "intfloat/multilingual-e5-large")
        assert received[-1] == "query: a question"
        assert received[-1] != "passage: a question"

    def test_embed_documents_leaves_bge_small_text_unchanged(self, monkeypatch):
        from symbiosis_brain import search as sb_search

        received: list[list[str]] = []

        def fake_embed(texts):
            texts = list(texts)
            received.append(texts)
            return [[1.0, 0.0] for _ in texts]

        monkeypatch.setattr(sb_search, "_embed", fake_embed)
        sb_search._embed_documents(["note body"], "BAAI/bge-small-en-v1.5")
        assert received[-1] == ["note body"]

    def test_index_note_and_search_vector_use_different_prefixes_for_the_same_text(
            self, db_path: Path, monkeypatch):
        """End to end through SearchEngine: writing and querying the SAME text
        under a prefixed model must NOT call the embedder with the same
        string — index_note gets the doc prefix, search_vector gets the query
        prefix."""
        from symbiosis_brain import search as sb_search

        storage = Storage(db_path)
        engine = SearchEngine(storage, model_name="intfloat/multilingual-e5-large")
        if not engine._vec_enabled:
            pytest.skip("sqlite-vec unavailable")
        engine._recreate_vec_table()  # e5-large is dim 1024, table starts at 384

        received: list[str] = []

        def fake_embed(texts):
            texts = list(texts)
            received.extend(texts)
            return [[1.0] + [0.0] * 1023 for _ in texts]

        monkeypatch.setattr(sb_search, "_embed", fake_embed)
        monkeypatch.setattr(sb_search, "_embed_one", lambda text: fake_embed([text])[0])

        engine.index_note("same/text.md", "shared text")
        engine.search_vector("shared text")

        assert received == ["passage: shared text", "query: shared text"]
        storage.close()


# ================== CP-1: лексические режимы (I-17, I-18, I-19) ==================
# Корпус синтетический: выдуманные ноты, выдуманные пути, ни одного реального
# имени. Русская нота нужна потому, что 94,3 % нулевых выдач режима `all`
# приходится именно на русские запросы (§4.1).

from symbiosis_brain.search import (  # noqa: E402
    FTS_EFFECTIVE_FALLBACK_ANY,
    FTS_MODE_ALL,
    FTS_MODE_ALL_THEN_ANY,
    FTS_MODE_ANY,
)


@pytest.fixture
def lexicon_engine(db_path: Path) -> SearchEngine:
    storage = Storage(db_path)
    storage.upsert_note(
        path="wiki/rotation.md", title="Ротация журнала", scope="global",
        note_type="wiki", tags=["log"],
        content="Ротация журнала выдачи удаляет события старше 90 дней",
    )
    storage.upsert_note(
        path="wiki/overfetch.md", title="Vector overfetch", scope="global",
        note_type="wiki", tags=["search"],
        content="Scoped vector search over-fetches candidates instead of starving",
    )
    storage.upsert_note(
        path="wiki/dedup.md", title="Дедуп при записи", scope="global",
        note_type="wiki", tags=["dedup"],
        content="Сигнал похожести считается до записи ноты",
    )
    engine = SearchEngine(storage)
    engine.index_all()
    return engine


# 11,1 токена — средняя длина живого запроса (§4.1). Здесь шесть, и четыре из
# них в корпусе отсутствуют: ровно тот случай, на котором AND даёт ноль.
_MULTIWORD_RU = "ротация журнала по расписанию каждые сутки"


def test_sanitize_ors_tokens_only_in_any_mode():
    """I-17: режим — второй ПОЗИЦИОННЫЙ аргумент; дефолт остаётся AND."""
    san = SearchEngine._sanitize_fts_query
    assert san("ротация журнала", FTS_MODE_ANY) == '"ротация" OR "журнала"'
    assert san("ротация журнала", FTS_MODE_ALL) == '"ротация" "журнала"'
    assert san("ротация журнала") == '"ротация" "журнала"'
    # неизвестный режим НЕ расширяет запрос — падаем в сегодняшний AND
    assert san("ротация журнала", "нет-такого-режима") == '"ротация" "журнала"'
    assert san("   ", FTS_MODE_ANY) == '""'
    # операторы FTS5 по-прежнему вычищаются до квотирования
    assert san("a:b (c)", FTS_MODE_ANY) == '"a" OR "b" OR "c"'


def test_fts_all_requires_every_token(lexicon_engine: SearchEngine):
    """C1: сегодняшнее поведение — ноль строк на многословном запросе."""
    assert lexicon_engine.search_fts(_MULTIWORD_RU, limit=10, mode=FTS_MODE_ALL) == []


def test_fts_any_finds_note_matching_part_of_the_query(lexicon_engine: SearchEngine):
    """§4.6 п. 1: многословный русский запрос находит ноту с ЧАСТЬЮ слов."""
    rows = lexicon_engine.search_fts(_MULTIWORD_RU, limit=10, mode=FTS_MODE_ANY)
    assert [r["path"] for r in rows] == ["wiki/rotation.md"]


def test_fts_mode_is_keyword_only(lexicon_engine: SearchEngine):
    """I-18: mode — keyword-only, позиционно его передать нельзя."""
    with pytest.raises(TypeError):
        lexicon_engine.search_fts(_MULTIWORD_RU, None, 10, FTS_MODE_ANY)


def test_search_default_fts_mode_is_any(lexicon_engine: SearchEngine):
    """I-19: дефолт самой функции — `any` (безопаснее для будущих вызовов)."""
    stats: dict = {}
    lexicon_engine.search(_MULTIWORD_RU, limit=5, stats=stats)
    assert stats["fts_mode"] == FTS_MODE_ANY


def test_stats_carries_exactly_two_keys(lexicon_engine: SearchEngine):
    """I-7: ровно два ключа и ни одного больше."""
    stats: dict = {}
    lexicon_engine.search(_MULTIWORD_RU, limit=5, stats=stats)
    assert set(stats) == {"fts_mode", "vec_enabled"}
    assert isinstance(stats["vec_enabled"], bool)
    assert stats["vec_enabled"] is bool(lexicon_engine._vec_enabled)


def test_all_then_any_reports_fallback(lexicon_engine: SearchEngine):
    """§4.6 п. 2: AND дал ноль -> повтор OR -> в stats уходит `fallback_any`."""
    stats: dict = {}
    hits = lexicon_engine.search(_MULTIWORD_RU, limit=5, mode="gist",
                                 fts_mode=FTS_MODE_ALL_THEN_ANY, stats=stats)
    assert stats["fts_mode"] == FTS_EFFECTIVE_FALLBACK_ANY
    assert stats["fts_mode"] != FTS_MODE_ALL_THEN_ANY  # в журнал — НИКОГДА
    assert any(h["path"] == "wiki/rotation.md" for h in hits)


def test_all_then_any_reports_all_when_and_hits(lexicon_engine: SearchEngine):
    """Тот же режим, но AND сработал -> эффективный режим `all`, не `fallback_any`."""
    stats: dict = {}
    lexicon_engine.search("ротация журнала", limit=5,
                          fts_mode=FTS_MODE_ALL_THEN_ANY, stats=stats)
    assert stats["fts_mode"] == FTS_MODE_ALL


@pytest.mark.parametrize("requested", [FTS_MODE_ANY, FTS_MODE_ALL])
def test_plain_modes_report_themselves(lexicon_engine: SearchEngine, requested):
    stats: dict = {}
    lexicon_engine.search(_MULTIWORD_RU, limit=5, fts_mode=requested, stats=stats)
    assert stats["fts_mode"] == requested


def test_stats_filled_even_on_empty_lexical_half(lexicon_engine: SearchEngine):
    """I-7: stats заполняется ПЕРЕД возвратом всегда — в том числе когда обе
    попытки лексической половины дали ноль. Это и есть случай, ради которого
    метрика заводится."""
    stats: dict = {}
    lexicon_engine.search("zzzнетакоготокенаqqq", limit=5,
                          fts_mode=FTS_MODE_ALL_THEN_ANY, stats=stats)
    assert stats["fts_mode"] == FTS_EFFECTIVE_FALLBACK_ANY
    assert set(stats) == {"fts_mode", "vec_enabled"}


@pytest.mark.asyncio
async def test_brain_search_requests_any_mode(tmp_vault: Path, db_path: Path, monkeypatch):
    """§4.2/§4.6 п. 3, ПРЯМАЯ проверка: движок подменён моком, утверждается
    значение аргумента, а не запись в журнале."""
    from unittest.mock import MagicMock

    from symbiosis_brain import server

    fake = MagicMock()
    fake.search.return_value = []
    monkeypatch.setattr(server, "_search", fake)
    monkeypatch.setattr(server, "_storage", Storage(db_path))
    monkeypatch.setattr(server, "_ready", None)
    monkeypatch.setattr(server, "_vault_path", tmp_vault)

    await server.call_tool("brain_search", {"query": "ротация журнала выдачи"})

    assert fake.search.call_args.kwargs["fts_mode"] == FTS_MODE_ANY


@pytest.fixture
def c8_engine(db_path: Path) -> SearchEngine:
    """Мини-корпус для C8: три ноты, чьи темы совпадают со stem'ами файлов."""
    storage = Storage(db_path)
    storage.upsert_note(
        path="wiki/rotation.md", title="Rotation", scope="global", note_type="wiki",
        content="Rotation deletes retrieval events older than ninety days", tags=[],
    )
    storage.upsert_note(
        path="wiki/dedup.md", title="Dedup", scope="global", note_type="wiki",
        content="Dedup hints at a similar note before the write happens", tags=[],
    )
    storage.upsert_note(
        path="wiki/lexicon.md", title="Lexicon", scope="global", note_type="wiki",
        content="Lexical half stops requiring every token of the query", tags=[],
    )
    engine = SearchEngine(storage)
    engine.index_all()
    return engine


def test_edit_queries_for_different_files_give_different_top3(c8_engine: SearchEngine):
    """C8/§4.6 п. 5: сигналом стало имя файла, а не общий хвост каталогов.

    До I-21 обе правки давали запрос, в котором доминировали одни и те же
    токены пути (`src`, `deep`, `nested`), и топ-3 совпадали."""
    from symbiosis_brain.pre_action_recall import build_query

    q1 = build_query("Edit", {"file_path": "src/deep/nested/rotation.py",
                              "new_string": "days"}, max_chars=500)
    q2 = build_query("Edit", {"file_path": "src/deep/nested/dedup.py",
                              "new_string": "days"}, max_chars=500)
    assert q1 == "rotation days" and q2 == "dedup days"

    top1 = [h["path"] for h in c8_engine.search(q1, limit=3, fts_mode=FTS_MODE_ANY)]
    top2 = [h["path"] for h in c8_engine.search(q2, limit=3, fts_mode=FTS_MODE_ANY)]
    assert top1 != top2
    assert top1[0] == "wiki/rotation.md"
    assert "wiki/dedup.md" in top2


def test_strong_requires_top3_in_both_halves(lexicon_engine: SearchEngine, monkeypatch):
    """§4.6 п. 6: `_strong` — топ-3 ОБЕИХ половин; `_in_both` семантику сохраняет.

    Половины подменены детерминированными списками: на трёх нотах реального
    корпуса ранги ≥ 3 не встречаются вовсе, и правило было бы не проверено."""
    def _n(path: str) -> dict:
        return {"path": path, "title": path, "scope": "global",
                "content": "тело ноты", "frontmatter": {}}

    fts = [_n("p/a"), _n("p/b"), _n("p/c"), _n("p/d")]      # ранги 0,1,2,3
    vec = [_n("p/a"), _n("p/d"), _n("p/e")]                 # ранги 0,1,2
    monkeypatch.setattr(lexicon_engine, "search_fts", lambda *a, **k: fts)
    monkeypatch.setattr(lexicon_engine, "search_vector", lambda *a, **k: vec)

    by_path = {h["path"]: h for h in lexicon_engine.search("q", limit=5)}
    assert set(by_path) == {"p/a", "p/b", "p/c", "p/d", "p/e"}

    assert by_path["p/a"]["_fts_rank"] == 0 and by_path["p/a"]["_vec_rank"] == 0
    assert by_path["p/a"]["_in_both"] is True and by_path["p/a"]["_strong"] is True
    # в обеих половинах, но лексический ранг 3 -> не «сильный»
    assert by_path["p/d"]["_in_both"] is True and by_path["p/d"]["_strong"] is False
    # только в одной половине
    assert by_path["p/b"]["_in_both"] is False and by_path["p/b"]["_strong"] is False
    assert by_path["p/e"]["_fts_rank"] is None and by_path["p/e"]["_vec_rank"] == 2
    assert by_path["p/e"]["_strong"] is False


def test_fts_only_emits_hits_and_never_stars(lexicon_engine: SearchEngine, monkeypatch):
    """§4.6 п. 7: вектор выключен -> выдача не обнуляется, ★ нет ни у кого."""
    from symbiosis_brain.pre_action_recall import format_recall_block

    monkeypatch.setattr(lexicon_engine, "_vec_enabled", False)
    hits = lexicon_engine.search(_MULTIWORD_RU, limit=5, mode="gist",
                                 fts_mode=FTS_MODE_ALL_THEN_ANY)
    assert hits                                    # FTS-only всё равно выдаёт
    assert all(h["_strong"] is False for h in hits)
    assert all(h["_in_both"] is False for h in hits)
    assert all(h["_vec_rank"] is None for h in hits)
    assert "★" not in format_recall_block("q", hits)


def test_rank_keys_present_in_gist_mode(lexicon_engine: SearchEngine):
    """I-22: новые ключи видны обеим поверхностям, как и `_score`/`_in_both`."""
    hits = lexicon_engine.search("ротация", limit=5, mode="gist", fts_mode=FTS_MODE_ANY)
    assert hits
    for h in hits:
        assert "_fts_rank" in h and "_vec_rank" in h
        assert isinstance(h["_strong"], bool)
        assert isinstance(h["_in_both"], bool)


# ================== CP-1b: скоуп на векторной стороне (C5, I-20) ==================
# Векторы синтетические и вкладываются напрямую в notes_vec: только так корпус
# ГАРАНТИРОВАННО устроен так, что скоупные ноты лежат за пределами k глобального
# KNN (§4.4). Тест на «настоящих» эмбеддингах проверял бы модель, а не выборку.

import numpy as np  # noqa: E402

from symbiosis_brain.search import OVERFETCH_FACTORS  # noqa: E402

_VEC_DIM = 384
_FOREIGN_SCOPE = "beta"
_TARGET_SCOPE = "alpha-seti"


def _unit(index: int, value: float = 1.0, extra: tuple[int, float] | None = None) -> bytes:
    v = np.zeros(_VEC_DIM, dtype=np.float32)
    v[index] = value
    if extra is not None:
        v[extra[0]] = extra[1]
    return v.tobytes()


def _seed_vec_corpus(db_path: Path, *, n_foreign: int, n_target: int) -> SearchEngine:
    """`n_foreign` чужих нот вплотную к запросу + `n_target` скоупных вдалеке.

    Запрос — орт e0. Чужая нота i: e0 + i*0.001 по оси 1, то есть расстояние
    ~0.001*i (ранги 0..n_foreign-1). Скоупная нота: орт e100, расстояние √2 —
    гарантированно ПОСЛЕ всех чужих.
    """
    storage = Storage(db_path)
    engine = SearchEngine(storage)
    assert engine._vec_enabled, (
        "sqlite-vec недоступен — тест голодания скоупа проверяет именно векторную "
        "половину и без неё бессмыслен (пакет объявляет sqlite-vec обязательной "
        "зависимостью, pyproject.toml:25)"
    )
    for i in range(n_foreign):
        path = f"beta/foreign-{i:03d}.md"
        storage.upsert_note(path=path, title=f"Foreign {i}", scope=_FOREIGN_SCOPE,
                            note_type="wiki", content="чужая нота", tags=[])
        storage._conn.execute(
            "INSERT INTO notes_vec (path, embedding) VALUES (?, ?)",
            (path, _unit(0, extra=(1, 0.001 * i))),
        )
    for j in range(n_target):
        path = f"alpha-seti/target-{j:03d}.md"
        storage.upsert_note(path=path, title=f"Target {j}", scope=_TARGET_SCOPE,
                            note_type="wiki", content="скоупная нота", tags=[])
        storage._conn.execute(
            "INSERT INTO notes_vec (path, embedding) VALUES (?, ?)",
            (path, _unit(100)),
        )
    storage._conn.commit()
    return engine


def _patch_query_vector(monkeypatch) -> None:
    from symbiosis_brain import search as _search_mod

    q = np.zeros(_VEC_DIM, dtype=np.float32)
    q[0] = 1.0
    monkeypatch.setattr(_search_mod, "_embed_one", lambda text: q.tolist())


def test_overfetch_factors_are_the_measured_ladder():
    """I-20: лестница — часть контракта, а не «настройка на вкус»."""
    assert OVERFETCH_FACTORS == (2, 8, 32)


def test_vector_scope_not_starved(db_path: Path, monkeypatch):
    """C5/§4.4: скоупные ноты лежат ЗА пределами k глобального KNN, и выдача
    всё равно набирает `limit`."""
    limit = 5
    engine = _seed_vec_corpus(db_path, n_foreign=40, n_target=limit)
    _patch_query_vector(monkeypatch)

    # Премисса корпуса. Без неё тест зелен при ЛЮБОЙ реализации: если скоупные
    # ноты и так попадают в глобальный топ-limit*2, чинить нечего.
    from symbiosis_brain.search import _embed_one
    q_blob = np.array(_embed_one("q"), dtype=np.float32).tobytes()
    first_k = engine.storage._conn.execute(
        "SELECT v.path FROM notes_vec v WHERE v.embedding MATCH ?"
        " ORDER BY v.distance LIMIT ?", (q_blob, limit * OVERFETCH_FACTORS[0]),
    ).fetchall()
    assert first_k, "KNN ничего не вернул — корпус собран неправильно"
    assert all(not r[0].startswith("alpha-seti/") for r in first_k), (
        "скоупные ноты попали в первый k — тест стал бы зелёным по построению"
    )

    results = engine.search_vector("q", scope=_TARGET_SCOPE, limit=limit)

    assert len(results) == limit
    assert {r["path"] for r in results} == {
        f"alpha-seti/target-{j:03d}.md" for j in range(limit)
    }
    assert all("_distance" in r for r in results)


def test_vector_scope_stops_when_corpus_exhausted(db_path: Path, monkeypatch):
    """Второе стоп-условие: скоупных нот меньше, чем limit — отдаём что есть и
    не эскалируем бесконечно."""
    engine = _seed_vec_corpus(db_path, n_foreign=40, n_target=2)
    _patch_query_vector(monkeypatch)

    results = engine.search_vector("q", scope=_TARGET_SCOPE, limit=5)

    assert len(results) == 2
    assert {r["path"] for r in results} == {
        "alpha-seti/target-000.md", "alpha-seti/target-001.md",
    }


def test_vector_without_scope_takes_the_global_top(db_path: Path, monkeypatch):
    """Регресс: без скоупа поведение прежнее — ближайшие limit из глобального
    пула, первым же шагом лестницы."""
    engine = _seed_vec_corpus(db_path, n_foreign=40, n_target=5)
    _patch_query_vector(monkeypatch)

    results = engine.search_vector("q", scope=None, limit=5)

    assert [r["path"] for r in results] == [f"beta/foreign-{i:03d}.md" for i in range(5)]


def test_vector_empty_index_returns_empty(db_path: Path, monkeypatch):
    """Пустой notes_vec: `k >= count(notes_vec)` выполняется сразу, лестница не
    крутится вхолостую и исключения не бросает."""
    storage = Storage(db_path)
    engine = SearchEngine(storage)
    assert engine._vec_enabled
    _patch_query_vector(monkeypatch)

    assert engine.search_vector("q", scope=_TARGET_SCOPE, limit=5) == []


# ================== CP-7: дедуп-сигнал (I-23, I-24, I-35) ==================
# Числа в тестах выписаны заранее и вручную: токенизация двигает частоту
# срабатывания при неизменном пороге, поэтому «улучшение» нормализации обязано
# краснеть, а не тихо менять поведение (§5.3).

from symbiosis_brain.search import (  # noqa: E402
    DEDUP_CONTAINMENT_MIN,
    DEDUP_MAX_SHOWN,
    DEDUP_TOP_K,
    _DEDUP_ENV_CACHE,
    _dedup_tokens,
    containment,
    dedup_candidates,
)


@pytest.fixture(autouse=True)
def _clean_dedup_env_cache():
    """Ручки I-35 читаются ОДИН раз на процесс — в тестах кэш сбрасывается."""
    _DEDUP_ENV_CACHE.clear()
    yield
    _DEDUP_ENV_CACHE.clear()


def test_dedup_defaults_are_the_calibrated_ones():
    assert DEDUP_TOP_K == 5
    assert DEDUP_CONTAINMENT_MIN == 0.5
    assert DEDUP_MAX_SHOWN == 2


def test_dedup_tokens_and_containment_are_deterministic():
    """Фиксированная пара строк: пунктуация, смешанный регистр, RU/EN-смесь и
    токены длины ≤ 2 (`ms`, `и`). Ожидаемые множества выписаны в тесте."""
    a = _dedup_tokens("Retrieval LOG для hooks: origin, e2e_ms, и sqlite")
    b = _dedup_tokens("Sqlite retrieval log — origin и e2e для хуков!")
    assert a == frozenset({"retrieval", "log", "hooks", "origin", "e2e", "sqlite"})
    assert b == frozenset({"sqlite", "retrieval", "log", "origin", "e2e", "хуков"})
    assert containment(a, b) == pytest.approx(5 / 6)
    assert containment(b, a) == pytest.approx(5 / 6)   # min(), а не |B|


def test_dedup_tokens_fold_case_and_split_on_punctuation():
    assert _dedup_tokens("Brain_Write: containment≥0.5!") == frozenset(
        {"brain", "write", "containment"}
    )


def test_dedup_tokens_drop_short_and_stop_words():
    assert _dedup_tokens("ok и в на по с") == frozenset()
    assert _dedup_tokens("") == frozenset()
    assert _dedup_tokens(None) == frozenset()


def test_containment_is_zero_on_empty_side():
    """I-24: при min(|A|,|B|) == 0 результат 0.0 — не деление на ноль и не 1.0."""
    assert containment(frozenset(), frozenset({"x"})) == 0.0
    assert containment(frozenset({"x"}), frozenset()) == 0.0
    assert containment(frozenset(), frozenset()) == 0.0


class _FakeEngine:
    """Движок-пустышка: `dedup_candidates` обязан работать с duck-typed
    объектом, а не только с настоящим SearchEngine."""

    def __init__(self, hits):
        self.hits = hits
        self.calls: list[dict] = []

    def search(self, **kwargs):
        self.calls.append(kwargs)
        return self.hits


def _hit(path, title, gist, in_both=True):
    return {"path": path, "title": title, "gist": gist, "_in_both": in_both}


_NEW_TITLE = "Retrieval log rotation"
_NEW_GIST = "Rotation deletes retrieval events older than ninety days"


def test_dedup_candidates_query_and_search_arguments():
    """§5.2: запрос — `title + gist`, кандидаты — top-5 в режиме `any`, без скоупа."""
    engine = _FakeEngine([])
    dedup_candidates(engine, None, title=_NEW_TITLE, gist=_NEW_GIST, self_path="w/new.md")
    kwargs = engine.calls[0]
    assert kwargs["query"] == f"{_NEW_TITLE} {_NEW_GIST}"
    assert kwargs["scope"] is None
    assert kwargs["limit"] == DEDUP_TOP_K
    assert kwargs["mode"] == "gist"
    assert kwargs["fts_mode"] == "any"
    # Дедуп-запрос НЕ логируется: журнал знает шесть путей (§2.1), этого среди
    # них нет — значит ни log_ctx, ни stats сюда не передаются.
    assert "log_ctx" not in kwargs and "stats" not in kwargs


def test_dedup_candidates_reports_a_near_duplicate():
    engine = _FakeEngine([
        _hit("wiki/rotation.md", "Retrieval log rotation",
             "Rotation deletes retrieval events older than ninety days"),
    ])
    found = dedup_candidates(engine, None, title=_NEW_TITLE, gist=_NEW_GIST,
                             self_path="wiki/rotation-2.md")
    assert [c["path"] for c in found] == ["wiki/rotation.md"]
    assert found[0]["containment"] == pytest.approx(1.0)
    assert found[0]["gist"].startswith("Rotation deletes")


def test_dedup_candidates_silent_on_a_new_topic():
    engine = _FakeEngine([
        _hit("wiki/valves.md", "Valve sizing", "How to size a valve for a water network"),
    ])
    assert dedup_candidates(engine, None, title=_NEW_TITLE, gist=_NEW_GIST,
                            self_path="wiki/new.md") == []


def test_dedup_candidates_requires_in_both():
    """§5.3: одного покрытия мало — кандидат обязан быть в ОБЕИХ половинах."""
    engine = _FakeEngine([
        _hit("wiki/rotation.md", _NEW_TITLE, _NEW_GIST, in_both=False),
    ])
    assert dedup_candidates(engine, None, title=_NEW_TITLE, gist=_NEW_GIST,
                            self_path="wiki/new.md") == []


def test_dedup_candidates_excludes_itself():
    """Перезапись существующей ноты не должна показывать её саму."""
    engine = _FakeEngine([_hit("wiki/rotation.md", _NEW_TITLE, _NEW_GIST)])
    assert dedup_candidates(engine, None, title=_NEW_TITLE, gist=_NEW_GIST,
                            self_path="wiki/rotation.md") == []
    # тот же путь без расширения — тоже я
    assert dedup_candidates(engine, None, title=_NEW_TITLE, gist=_NEW_GIST,
                            self_path="wiki/rotation") == []


def test_dedup_candidates_caps_and_sorts():
    """Три кандидата с РАЗНЫМ покрытием (1.0 / 0.6 / 0.5), выдача — два лучших.

    Покрытия считаются вручную: |mine| = 9 токенов
    {retrieval, log, rotation, deletes, events, older, than, ninety, days}."""
    engine = _FakeEngine([
        _hit("w/c.md", "Rotation", "Rotation of the journal"),              # 1/2  = 0.5
        _hit("w/a.md", _NEW_TITLE, _NEW_GIST),                              # 9/9  = 1.0
        _hit("w/b.md", _NEW_TITLE, "Rotation policy for the journal"),      # 3/5  = 0.6
    ])
    found = dedup_candidates(engine, None, title=_NEW_TITLE, gist=_NEW_GIST,
                             self_path="w/new.md")
    assert [c["path"] for c in found] == ["w/a.md", "w/b.md"]
    assert len(found) == DEDUP_MAX_SHOWN
    assert found[0]["containment"] == pytest.approx(1.0)
    assert found[1]["containment"] == pytest.approx(0.6)


def test_dedup_threshold_is_inclusive():
    """Ровно на пороге кандидат ПОКАЗЫВАЕТСЯ (`>=`, а не `>`): w/c.md выше даёт
    ровно 0.5 и отсекается только капом, а не сравнением."""
    engine = _FakeEngine([_hit("w/c.md", "Rotation", "Rotation of the journal")])
    found = dedup_candidates(engine, None, title=_NEW_TITLE, gist=_NEW_GIST,
                             self_path="w/new.md")
    assert [c["path"] for c in found] == ["w/c.md"]
    assert found[0]["containment"] == pytest.approx(0.5)


def test_dedup_candidates_never_raises_on_engine_error():
    class _Boom:
        def search(self, **kwargs):
            raise RuntimeError("движок упал")

    assert dedup_candidates(_Boom(), None, title=_NEW_TITLE, gist=_NEW_GIST,
                            self_path="w/new.md") == []


def test_dedup_candidates_empty_query_does_not_search():
    engine = _FakeEngine([_hit("w/a.md", _NEW_TITLE, _NEW_GIST)])
    assert dedup_candidates(engine, None, title="", gist="", self_path="w/new.md") == []
    assert engine.calls == []


def test_dedup_min_zero_disables_everything(monkeypatch):
    """I-35: `0` = сигнал выключен целиком — поиск кандидатов не выполняется."""
    monkeypatch.setenv("SYMBIOSIS_BRAIN_DEDUP_MIN", "0")
    _DEDUP_ENV_CACHE.clear()
    engine = _FakeEngine([_hit("wiki/rotation.md", _NEW_TITLE, _NEW_GIST)])
    assert dedup_candidates(engine, None, title=_NEW_TITLE, gist=_NEW_GIST,
                            self_path="w/new.md") == []
    assert engine.calls == []


def test_dedup_max_shown_zero_disables_everything(monkeypatch):
    monkeypatch.setenv("SYMBIOSIS_BRAIN_DEDUP_MAX_SHOWN", "0")
    _DEDUP_ENV_CACHE.clear()
    engine = _FakeEngine([_hit("wiki/rotation.md", _NEW_TITLE, _NEW_GIST)])
    assert dedup_candidates(engine, None, title=_NEW_TITLE, gist=_NEW_GIST,
                            self_path="w/new.md") == []
    assert engine.calls == []


@pytest.mark.parametrize("raw", ["", "не число", "-0.5", "1.5", "0,7"])
def test_dedup_min_garbage_falls_back_to_default(monkeypatch, raw):
    monkeypatch.setenv("SYMBIOSIS_BRAIN_DEDUP_MIN", raw)
    _DEDUP_ENV_CACHE.clear()
    from symbiosis_brain.search import _dedup_min

    assert _dedup_min() == DEDUP_CONTAINMENT_MIN


def test_dedup_min_is_read_once_per_process(monkeypatch):
    """I-35: «читается один раз на процесс» — не декорация, а поведение."""
    from symbiosis_brain.search import _dedup_min

    monkeypatch.setenv("SYMBIOSIS_BRAIN_DEDUP_MIN", "0.9")
    _DEDUP_ENV_CACHE.clear()
    assert _dedup_min() == pytest.approx(0.9)
    monkeypatch.setenv("SYMBIOSIS_BRAIN_DEDUP_MIN", "0.1")
    assert _dedup_min() == pytest.approx(0.9)   # кэш, а не перечитывание


def test_dedup_candidates_falls_back_to_storage_for_missing_gist():
    """Аргумент `storage` (I-23) не декоративный: если хит пришёл без гиста
    (чужой duck-typed движок, `mode` не 'gist'), гист берётся из БД."""
    class _Storage:
        def get_note(self, path):
            return {"title": _NEW_TITLE, "frontmatter": {"gist": _NEW_GIST}}

    engine = _FakeEngine([{"path": "wiki/rotation.md", "_in_both": True}])
    found = dedup_candidates(engine, _Storage(), title=_NEW_TITLE, gist=_NEW_GIST,
                             self_path="w/new.md")
    assert [c["path"] for c in found] == ["wiki/rotation.md"]
    assert found[0]["gist"] == _NEW_GIST


# ================== CP-7: tools/dedup_calib.py ==================

def _load_dedup_calib():
    """tools/ — не пакет; грузим по пути, как tests/test_changelog_section.py."""
    import importlib.util
    import sys as _sys

    repo_root = Path(__file__).resolve().parents[1]
    module_path = repo_root / "tools" / "dedup_calib.py"
    spec = importlib.util.spec_from_file_location("dedup_calib", module_path)
    assert spec and spec.loader, f"cannot load {module_path}"
    module = importlib.util.module_from_spec(spec)
    _sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_dedup_calib_snapshot_is_read_only(tmp_path: Path):
    """I-32 п. 1: живая БД открывается только `?mode=ro`, копия — VACUUM INTO.

    Источник остаётся в WAL и с ОТКРЫТЫМ писателем — ровно та ситуация, в
    которой поштучное копирование трёх файлов WAL-набора дало бы рваную копию."""
    calib = _load_dedup_calib()
    src = tmp_path / "live.db"
    writer = sqlite3.connect(str(src))
    writer.execute("PRAGMA journal_mode=WAL")
    writer.execute("CREATE TABLE notes (path TEXT PRIMARY KEY)")
    writer.execute("INSERT INTO notes VALUES ('wiki/a.md')")
    writer.commit()

    dst = calib.snapshot(src, tmp_path / "snap.db")

    assert dst.exists()
    copy = sqlite3.connect(str(dst))
    assert copy.execute("SELECT COUNT(*) FROM notes").fetchone()[0] == 1
    copy.close()
    # исходник не тронут и по-прежнему пишется
    writer.execute("INSERT INTO notes VALUES ('wiki/b.md')")
    writer.commit()
    writer.close()


def test_dedup_calib_snapshot_refuses_to_write_the_source(tmp_path: Path):
    calib = _load_dedup_calib()
    src = tmp_path / "live.db"
    c = sqlite3.connect(str(src))
    c.execute("CREATE TABLE notes (path TEXT PRIMARY KEY)")
    c.commit()
    c.close()

    ro = sqlite3.connect(calib.read_only_uri(src), uri=True)
    with pytest.raises(sqlite3.OperationalError, match="readonly"):
        ro.execute("CREATE TABLE z (y)")
    ro.close()
