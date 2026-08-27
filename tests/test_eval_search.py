"""Tests for tools/eval_search.py — the Stage 2 search-eval harness (CP-8a).

Two jobs, and the second one is the reason the file exists:

1. Pin the metric arithmetic. recall@k / MRR / nDCG@k are hand-recomputed on a
   toy set below, so a change in a formula is a red test and not a quietly
   different number in a report nobody re-derives.
2. Run the SYNTHETIC set (tests/data/eval_search) as a plain pytest regression.
   Before CP-8a nothing in the suite caught a change in search ranking at all
   (report 03, F30); this is that guard.

The module lives in tools/ (repo tooling, not shipped in the wheel), so it is
loaded by path — same idiom as tests/test_changelog_section.py.
"""
import hashlib
import importlib.util
import json
import math
import sqlite3
import struct
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
_MODULE_PATH = REPO_ROOT / "tools" / "eval_search.py"
DATA_DIR = Path(__file__).resolve().parent / "data" / "eval_search"


def _load():
    spec = importlib.util.spec_from_file_location("eval_search", _MODULE_PATH)
    assert spec and spec.loader, f"cannot load {_MODULE_PATH}"
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


eval_search = _load()


# ---------------------------------------------------------------- metrics ---

TOY_ROW = {
    "query": "toy",
    "source": "mcp-search",
    "origin": "unknown",
    "scope": None,
    "lang": "en",
    "shown": ["n/a.md", "n/b.md", "n/c.md"],
    "read_after": ["n/a.md", "n/c.md", "n/x.md"],
}
TOY_RANKED = ["n/a.md", "n/b.md", "n/c.md", "n/d.md", "n/e.md"]


def test_grades_follow_the_relevance_table():
    """Spec 7.2: read_after -> 2, shown-and-ignored -> 0, never shown -> NO grade.
    The third row is the one that matters: an unjudged path must not silently
    become a negative, or every configuration is punished for finding something
    the live configuration never had the chance to show."""
    grades = eval_search.grades_for(TOY_ROW)
    assert grades["n/a.md"] == eval_search.GRADE_READ
    assert grades["n/c.md"] == eval_search.GRADE_READ
    assert grades["n/x.md"] == eval_search.GRADE_READ   # read, though never in `shown`
    assert grades["n/b.md"] == eval_search.GRADE_SHOWN  # shown and ignored
    assert "n/d.md" not in grades                       # never shown: no judgement


def test_recall_mrr_and_ndcg_match_the_hand_computation():
    """The toy set is small enough to recompute on paper, and that is the point:
    three relevant paths (a, c, x), two of them inside the top 5, the first at
    rank 1. Gains are linear in the grade, positions discounted by log2(i+1)."""
    k = 5
    grades = eval_search.grades_for(TOY_ROW)

    assert eval_search.recall_at_k(TOY_RANKED, grades, k) == pytest.approx(2 / 3)
    assert eval_search.reciprocal_rank(TOY_RANKED, grades, k) == pytest.approx(1.0)

    dcg = 2 / math.log2(2) + 2 / math.log2(4)                       # ranks 1 and 3
    idcg = 2 / math.log2(2) + 2 / math.log2(3) + 2 / math.log2(4)   # three 2s in a row
    assert eval_search.ndcg_at_k(TOY_RANKED, grades, k) == pytest.approx(dcg / idcg)
    assert eval_search.ndcg_at_k(TOY_RANKED, grades, k) == pytest.approx(0.70392, abs=5e-5)


def test_an_unjudged_query_stays_out_of_the_denominator():
    row = dict(TOY_ROW, shown=["n/b.md"], read_after=[])
    grades = eval_search.grades_for(row)
    assert eval_search.recall_at_k(TOY_RANKED, grades, 5) is None
    assert eval_search.reciprocal_rank(TOY_RANKED, grades, 5) is None
    assert eval_search.ndcg_at_k(TOY_RANKED, grades, 5) is None


def test_gold_outranks_the_transcript_proxy():
    """A hand-labelled pair is a judgement; `shown` is a proxy. When the synthetic
    set carries both, gold wins."""
    row = dict(TOY_ROW, shown=["n/g.md"], read_after=[], gold=["n/g.md"])
    assert eval_search.grades_for(row)["n/g.md"] == eval_search.GRADE_READ


def test_metrics_block_carries_exactly_the_I34_keys():
    runs = [eval_search.Run(row=TOY_ROW, ranked=TOY_RANKED, latency_ms=1.5,
                            effective_fts_mode="any")]
    block = eval_search._metrics(runs, 5)
    assert set(block) == {"n", "recall_at_k", "mrr", "ndcg_at_k",
                          "zero_hit_rate", "p50_latency_ms"}


# --------------------------------------------------------------- loading ----

def test_harness_text_rows_are_dropped_not_measured(tmp_path):
    """hook-prompt rows can carry a task-notification instead of a human prompt
    (mine_queries.py:203 walks back to the nearest user message, and a
    notification IS a user-role message). Counting those would measure the
    harness, not the memory."""
    src = tmp_path / "q.jsonl"
    rows = [
        {"query": "<task-notification>\n<task-id>abc</task-id>", "source": "hook-prompt",
         "origin": "main", "scope": None, "lang": "en", "shown": [], "read_after": []},
        {"query": "   ", "source": "hook-prompt", "origin": "main", "scope": None,
         "lang": "en", "shown": [], "read_after": []},
        {"query": "real question", "source": "mcp-search", "origin": "unknown",
         "scope": None, "lang": "en", "shown": [], "read_after": []},
    ]
    src.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")

    parsed, dropped = eval_search.load_queries(src)
    assert [r["query"] for r in parsed] == ["real question"]
    assert dropped == 2


def test_a_broken_jsonl_line_names_its_line_number(tmp_path):
    src = tmp_path / "q.jsonl"
    src.write_text('{"query": "ok", "lang": "en"}\nnot json at all\n', encoding="utf-8")
    with pytest.raises(eval_search.EvalError) as excinfo:
        eval_search.load_queries(src)
    assert ":2:" in str(excinfo.value)


# -------------------------------------------------------------- snapshot ----

def test_snapshot_opens_the_source_read_only_and_leaves_it_byte_identical(tmp_path, monkeypatch):
    """I-32 п. 1. Two assertions, and both are needed: the URI proves we asked
    for ?mode=ro (which cannot create or write the database), the hash proves we
    did not write to it anyway. Copying brain.db{,-wal,-shm} by hand would pass
    neither — it can tear the copy on a checkpoint (storage.py:34)."""
    live = tmp_path / "live.db"
    conn = sqlite3.connect(str(live))
    conn.execute("CREATE TABLE t (a TEXT)")
    conn.executemany("INSERT INTO t VALUES (?)", [("one",), ("two",)])
    conn.commit()
    conn.close()
    before = hashlib.sha256(live.read_bytes()).hexdigest()

    seen: list[str] = []
    real_connect = eval_search.sqlite3.connect

    def spy(target, *args, **kwargs):
        seen.append(str(target))
        return real_connect(target, *args, **kwargs)

    monkeypatch.setattr(eval_search.sqlite3, "connect", spy)
    dest = tmp_path / "copy" / "brain.db"
    eval_search.snapshot_db(live, dest)

    assert seen, "snapshot_db did not open the source at all"
    assert seen[0].startswith("file:") and seen[0].endswith("?mode=ro"), seen[0]
    assert hashlib.sha256(live.read_bytes()).hexdigest() == before
    assert not (tmp_path / "live.db-wal").exists()

    copy = sqlite3.connect(str(dest))
    try:
        assert [r[0] for r in copy.execute("SELECT a FROM t ORDER BY a")] == ["one", "two"]
    finally:
        copy.close()


def test_snapshot_replaces_a_previous_copy(tmp_path):
    """VACUUM INTO refuses an existing target, so a second run in the same
    --work-dir would die on the leftovers of the first."""
    live = tmp_path / "live.db"
    conn = sqlite3.connect(str(live))
    conn.execute("CREATE TABLE t (a TEXT)")
    conn.commit()
    conn.close()

    dest = tmp_path / "copy" / "brain.db"
    eval_search.snapshot_db(live, dest)
    dest.with_name("brain.db-wal").write_bytes(b"stale")
    eval_search.snapshot_db(live, dest)          # must not raise
    assert not dest.with_name("brain.db-wal").exists()


# ---------------------------------------------------------- model override --

def test_apply_model_override_sets_the_name_then_clears_the_singleton(monkeypatch):
    """I-32 п. 2, and the order is the whole contract: _get_embedder reads
    _MODEL_NAME only while _embedder is None (search.py:130-133), so clearing
    first and naming second would rebuild the OLD model."""
    from symbiosis_brain import search as sb_search

    monkeypatch.setattr(sb_search, "_MODEL_NAME", "fixture/model-zero")
    monkeypatch.setattr(sb_search, "_embedder", object())

    eval_search.apply_model_override("fixture/model-nine")

    assert sb_search._MODEL_NAME == "fixture/model-nine"
    assert sb_search._embedder is None


def test_apply_model_override_refuses_an_empty_name():
    with pytest.raises(eval_search.EvalError):
        eval_search.apply_model_override("   ")


# ------------------------------------------------------------- the report ---

def _payload_for_render():
    return {
        "meta": {
            "generated_at": "2026-01-01T00:00:00+00:00",
            "vault": "vault", "work_dir": "work", "queries": "q.jsonl",
            "k": 5, "model": "fixture/model-nine",
            "rows_total": 3, "rows_executed": 2, "rows_dropped_system": 1, "rows_judged": 1,
            "lexical_zero_hit_rate": {"all": 0.5, "any": 0.0},
            "fallback_any_share": {},
            "caveats": list(eval_search.CAVEATS),
        },
        "results": [{"config": "fts-any", "n": 1, "recall_at_k": 1.0, "mrr": 1.0,
                     "ndcg_at_k": 1.0, "zero_hit_rate": 0.0, "p50_latency_ms": 1.0,
                     "buckets": {}}],
    }


def test_the_report_header_carries_all_three_caveats():
    """Spec 7.2 and 4.3 both say the caveats must be PRINTED next to the numbers.
    A table without them reads as absolute truth, which it is not."""
    text = eval_search.render_report(_payload_for_render())
    assert len(eval_search.CAVEATS) == 3
    assert "4.3" in text                        # label set favours the live config
    assert "session_id" in text                 # join without a session id
    assert "pre_action_recall.py:112" in text   # hook queries are 60-char snippets
    assert "mine_queries.py:203" in text        # hook-prompt is the first 200 chars


def test_the_report_prints_the_label_independent_number():
    text = eval_search.render_report(_payload_for_render())
    assert "lexical zero-hit" in text
    assert "50.0%" in text and "0.0%" in text


# ------------------------------------------------------- the synthetic set --

@pytest.fixture(scope="module")
def synthetic_rows():
    rows, dropped = eval_search.load_queries(DATA_DIR / "queries.jsonl")
    assert dropped == 0, "the synthetic set must not contain harness text"
    return rows


@pytest.fixture(scope="module")
def synthetic_engine(tmp_path_factory):
    work = tmp_path_factory.mktemp("eval-work")
    db = eval_search.prepare_work_dir(DATA_DIR / "vault", work)
    engine, storage = eval_search.open_engine(db, model=None)
    try:
        yield engine
    finally:
        storage.close()


def test_the_synthetic_set_matches_the_I33_schema(synthetic_rows):
    required = {"query", "source", "origin", "scope", "lang", "shown", "read_after", "gold"}
    corpus = {p.relative_to(DATA_DIR / "vault").as_posix()
              for p in (DATA_DIR / "vault").rglob("*.md")}
    assert 12 <= len(synthetic_rows) <= 15, "spec 7.2 asks for 12-15 pairs"
    langs = {row["lang"] for row in synthetic_rows}
    assert langs == {"ru", "en", "mixed"}, f"RU/EN/MIX all required, got {langs}"
    for row in synthetic_rows:
        assert required <= set(row), f"missing keys: {required - set(row)}"
        assert row["lang"] in ("ru", "en", "mixed")
        assert row["gold"], "a synthetic row without gold measures nothing"
        for path in row["gold"]:
            assert path in corpus, f"gold path {path} is not in the synthetic corpus"
        assert row["shown"] == [] and row["read_after"] == [], (
            "the synthetic set has no transcript proxy — it is labelled by hand"
        )


def test_synthetic_fts_any_never_comes_back_empty(synthetic_engine, synthetic_rows):
    """The decisive, label-independent number of 4.3: with OR a multi-word query
    stops returning nothing. Every synthetic query shares at least one LITERAL
    token with its gold note — this FTS5 table is tokenize='porter'
    (storage.py:73-78), which stems English only, so Russian word forms must
    match exactly."""
    cfg = eval_search.configs()["fts-any"]
    result, _ = eval_search.evaluate(synthetic_engine, cfg, synthetic_rows, 5)
    assert result["zero_hit_rate"] == 0.0
    assert result["recall_at_k"] >= 0.7


def test_synthetic_fts_all_still_starves(synthetic_engine, synthetic_rows):
    """The mirror image, and the reason CP-1 exists: under AND one word that is
    nowhere in the corpus empties the whole result. Seven of the fourteen queries
    are built that way. If this ever reads 0.0, `all` has quietly become `any`
    and the two modes stopped being two modes."""
    cfg = eval_search.configs()["fts-all"]
    result, _ = eval_search.evaluate(synthetic_engine, cfg, synthetic_rows, 5)
    assert result["zero_hit_rate"] >= 0.35


def test_synthetic_vector_half_never_comes_back_empty(synthetic_engine, synthetic_rows):
    if not synthetic_engine._vec_enabled:
        pytest.skip("sqlite-vec unavailable — the vector half cannot be measured")
    cfg = eval_search.configs()["vec-current"]
    result, _ = eval_search.evaluate(synthetic_engine, cfg, synthetic_rows, 5)
    assert result["zero_hit_rate"] == 0.0


def test_synthetic_hybrid_keeps_the_gold_note_within_reach(synthetic_engine, synthetic_rows):
    """Floors are deliberately loose: the sharp assertion in this file is
    zero_hit_rate, which is label-independent. These two only have to go red when
    fusion or ranking breaks outright — the current embedder is English-only
    (search.py:20) and carries the Russian half of the set badly on purpose."""
    if not synthetic_engine._vec_enabled:
        pytest.skip("sqlite-vec unavailable — hybrid cannot be measured")
    cfg = eval_search.configs()["hybrid-any"]
    result, _ = eval_search.evaluate(synthetic_engine, cfg, synthetic_rows, 5)
    assert result["zero_hit_rate"] == 0.0
    # Пороги 0,6 / 0,4 выставлены ДО того, как написан корпус, — то есть они
    # угаданы, а не измерены. Оставлены намеренно (рядом стоит сильное
    # метко-независимое утверждение `zero_hit_rate == 0.0`), но исполнитель
    # ОБЯЗАН записать в `review/cp-08a-exec.md` фактические `recall@5` и `MRR`
    # и версию `fastembed`, под которой они получены: без этого «>= 0,6»
    # невозможно ни подтвердить, ни поправить при смене эмбеддера.
    assert result["recall_at_k"] >= 0.6
    assert result["mrr"] >= 0.4


def test_all_then_any_reports_the_effective_mode_not_the_requested_one(
        synthetic_engine, synthetic_rows):
    """Spec 2.9: `all_then_any` is never a value that travels onward — what
    travels is `all` or `fallback_any`. The harness reads it out of stats and
    turns it into meta.fallback_any_share, so a silently-zeroed fallback metric
    is visible here rather than in a report six weeks later."""
    if not synthetic_engine._vec_enabled:
        pytest.skip("sqlite-vec unavailable — hybrid cannot be measured")
    cfg = eval_search.configs()["hybrid-all-then-any"]
    _, runs = eval_search.evaluate(synthetic_engine, cfg, synthetic_rows, 5)
    modes = {run.effective_fts_mode for run in runs}
    assert "all_then_any" not in modes, modes
    assert modes <= {"all", "fallback_any"}, modes
    assert "fallback_any" in modes, (
        "seven synthetic queries are built to starve under AND — none fell back"
    )


def test_buckets_are_split_by_lang_and_by_source(synthetic_engine, synthetic_rows):
    cfg = eval_search.configs()["fts-any"]
    result, _ = eval_search.evaluate(synthetic_engine, cfg, synthetic_rows, 5)
    keys = set(result["buckets"])
    assert {"lang:ru", "lang:en", "lang:mixed"} <= keys, keys
    assert any(k.startswith("source:") for k in keys), keys


# ---------- CP-8b: model swap for real, rerank, and the cost of a rebuild ----------

def test_rebuild_vector_index_redeclares_the_table_for_a_narrower_model(tmp_path, monkeypatch):
    """I-32 п. 2 end to end, offline. notes_vec is declared FLOAT[384]
    (search.py:245-254), so a model of another width needs the table dropped and
    recreated — otherwise every INSERT of a differently-sized blob fails, or
    worse, two models share one table and the distances mean nothing.

    The fake embedder is eight floats wide and deterministic: the point of this
    test is the plumbing, not the vectors, and a real model would make it a
    download test instead."""
    from symbiosis_brain import search as sb_search
    from symbiosis_brain.storage import Storage

    db = eval_search.prepare_work_dir(DATA_DIR / "vault", tmp_path / "work")
    storage = Storage(db)
    try:
        engine = sb_search.SearchEngine(storage)
        if not engine._vec_enabled:
            pytest.skip("sqlite-vec unavailable — there is no index to rebuild")

        def fake_embed(texts):
            return [[float((len(t) + i) % 7) for i in range(8)] for t in texts]

        # monkeypatch records the originals, so the module singletons are restored
        # even though rebuild_vector_index assigns to them directly.
        monkeypatch.setattr(sb_search, "_MODEL_NAME", "BAAI/bge-small-en-v1.5")
        monkeypatch.setattr(sb_search, "_embedder", None)
        monkeypatch.setattr(sb_search, "_embed", fake_embed)
        monkeypatch.setattr(sb_search, "_embed_one", lambda text: fake_embed([text])[0])

        dim = eval_search.rebuild_vector_index(engine, storage, model="fixture/model-eight")

        assert dim == 8
        assert sb_search._MODEL_NAME == "fixture/model-eight"
        assert sb_search._embedder is None

        declared = storage._conn.execute(
            "SELECT sql FROM sqlite_master WHERE name='notes_vec'").fetchone()[0]
        assert "FLOAT[8]" in declared, declared
        indexed = storage._conn.execute("SELECT COUNT(*) FROM notes_vec").fetchone()[0]
        assert indexed == storage.count_notes()
        assert engine.search_vector("quartz ingest", limit=3)
    finally:
        storage.close()


def test_peak_rss_is_a_positive_number_or_an_honest_none():
    """A benchmark that dies because it could not read a counter is worse than a
    benchmark with one blank cell — spec 7.5 asks for the number, not for a new
    way to fail."""
    value = eval_search.peak_rss_bytes()
    assert value is None or (isinstance(value, int) and value > 0)


def test_the_reranker_reorders_inside_the_overfetched_pool(synthetic_engine, monkeypatch):
    """The rerank configuration of spec 7.3 must (a) draw from an OVER-fetched
    hybrid pool, (b) return at most k, (c) actually let the cross-encoder decide
    the order. A fake ascending scorer reverses the pool, which is the cheapest
    unambiguous proof of (c)."""
    if not synthetic_engine._vec_enabled:
        pytest.skip("sqlite-vec unavailable — hybrid cannot be measured")

    row = {"query": "redis cache eviction policy", "source": "mcp-search",
           "origin": "unknown", "scope": None, "lang": "en",
           "shown": [], "read_after": [], "gold": []}

    pool_run = eval_search._run_one(
        synthetic_engine, eval_search.configs()["hybrid-any"], row,
        5 * eval_search.RERANK_OVERFETCH)

    class _Ascending:
        def rerank(self, query, documents, **kwargs):
            return [float(i) for i, _ in enumerate(documents)]

    monkeypatch.setattr(eval_search, "_cross_encoder", lambda: _Ascending())
    run = eval_search._run_one(synthetic_engine, eval_search.configs()["hybrid-any+rerank"], row, 5)

    assert len(run.ranked) <= 5
    assert set(run.ranked) <= set(pool_run.ranked)
    assert run.ranked == list(reversed(pool_run.ranked))[:5]


def test_the_report_prints_the_cost_of_a_rebuild_when_a_model_was_swapped():
    payload = _payload_for_render()
    payload["meta"]["model"] = "fixture/model-nine"
    payload["meta"]["reindex_seconds"] = 143.2
    payload["meta"]["peak_rss_bytes"] = 1_181_116_006
    text = eval_search.render_report(payload)
    assert "reindex" in text
    assert "143.2" in text
    assert "1.10 GiB" in text or "1.1 GiB" in text


def test_the_report_stays_quiet_about_a_rebuild_that_did_not_happen():
    """CP-8a payloads carry neither key; the renderer must not invent a line —
    an empty "reindex: 0 s" would read as "the rebuild was free"."""
    text = eval_search.render_report(_payload_for_render())
    assert "reindex" not in text


# ---------- CP-8b: E1 --rerank-model, E2 --query-prefix/--doc-prefix (lead directive §3) ----------

def test_rerank_model_flag_reaches_the_cross_encoder_factory(monkeypatch, tmp_path, synthetic_engine):
    """E1.1: --rerank-model must travel all the way to TextCrossEncoder(model_name=...)
    (patched here so the test never touches the network), and the result label
    must carry the model slug — otherwise two reranker JSONs collide in the
    summary table (lead directive §3, E1)."""
    if not synthetic_engine._vec_enabled:
        pytest.skip("sqlite-vec unavailable — rerank cannot be measured")

    seen = {}

    class _RecordingCrossEncoder:
        def __init__(self, model_name, **kwargs):
            seen["model_name"] = model_name

        def rerank(self, query, documents, **kwargs):
            return [1.0 for _ in documents]

    monkeypatch.setattr(
        "fastembed.rerank.cross_encoder.TextCrossEncoder", _RecordingCrossEncoder)
    monkeypatch.setattr(eval_search, "_cross_encoder_singleton", None)

    payload = eval_search.run_eval(
        vault=DATA_DIR / "vault", work_dir=tmp_path / "work",
        queries=DATA_DIR / "queries.jsonl",
        config_names=["hybrid-any+rerank"],
        rerank_model="fixture/candidate-reranker")

    assert seen.get("model_name") == "fixture/candidate-reranker"
    assert payload["meta"]["rerank_model"] == "fixture/candidate-reranker"
    assert payload["results"][0]["config"] == "hybrid-any+rerank(candidate-reranker)"


def test_prefix_flags_reshape_texts_and_restore_originals_after_run(tmp_path, monkeypatch,
                                                                     synthetic_engine):
    """E2.2: a fake _embed records every text it is handed. After rebuild_vector_index
    every document text starts with doc_prefix, and a query through search_vector
    starts with query_prefix; once run_eval returns, the module singletons
    _embed/_embed_one must be back to what they were before the call — a leaked
    wrapper would silently prefix the NEXT process-lifetime config too."""
    if not synthetic_engine._vec_enabled:
        pytest.skip("sqlite-vec unavailable — there is no index to rebuild")

    from symbiosis_brain import search as sb_search

    recorded_batches = []

    def fake_embed(texts):
        texts = list(texts)
        recorded_batches.append(texts)
        return [[float((len(t) + i) % 7) for i in range(8)] for t in texts]

    monkeypatch.setattr(sb_search, "_MODEL_NAME", "BAAI/bge-small-en-v1.5")
    monkeypatch.setattr(sb_search, "_embedder", None)
    monkeypatch.setattr(sb_search, "_embed", fake_embed)
    monkeypatch.setattr(sb_search, "_embed_one", lambda text: fake_embed([text])[0])

    embed_before, embed_one_before = sb_search._embed, sb_search._embed_one

    eval_search.run_eval(
        vault=DATA_DIR / "vault", work_dir=tmp_path / "work",
        queries=DATA_DIR / "queries.jsonl",
        config_names=["vec-current"],
        model="fixture/model-eight",
        query_prefix="query: ", doc_prefix="passage: ")

    doc_batches = [b for b in recorded_batches if len(b) > 1]
    assert doc_batches, "index_all should have embedded more than one document at once"
    assert all(t.startswith("passage: ") for t in doc_batches[0]), doc_batches[0]

    single_texts = [b[0] for b in recorded_batches if len(b) == 1]
    assert single_texts, "search_vector should have embedded at least one query"
    assert any(t.startswith("query: ") for t in single_texts), single_texts

    # sb_search._embed / _embed_one were captured as monkeypatch fixtures above
    # (fake_embed / the lambda); run_eval must hand back exactly those objects,
    # not some other wrapper.
    assert sb_search._embed is embed_before
    assert sb_search._embed_one is embed_one_before


def test_query_prefix_without_a_model_is_an_eval_error(tmp_path):
    """E2.3: prefixes rewrite the module singletons that --model itself swaps in;
    without --model there is no swapped-in embedder to wrap, so the combination
    is refused up front rather than silently prefixing the installed model."""
    with pytest.raises(eval_search.EvalError):
        eval_search.run_eval(
            vault=DATA_DIR / "vault", work_dir=tmp_path / "work",
            queries=DATA_DIR / "queries.jsonl",
            config_names=["fts-any"], query_prefix="query: ")


def test_the_report_stays_quiet_about_rerank_and_prefixes_the_payload_does_not_carry():
    """E2.4 / CP-8a payloads carry neither key — the renderer must not invent a
    line for either."""
    text = eval_search.render_report(_payload_for_render())
    assert "rerank" not in text
    assert "prefixes" not in text


def test_the_report_prints_rerank_model_and_prefixes_when_present():
    payload = _payload_for_render()
    payload["meta"]["rerank_model"] = "fixture/candidate-reranker"
    payload["meta"]["query_prefix"] = "query: "
    payload["meta"]["doc_prefix"] = "passage: "
    text = eval_search.render_report(payload)
    assert "rerank" in text and "fixture/candidate-reranker" in text
    assert "prefixes" in text and "query: " in text and "passage: " in text


# ---------- CP-8b: E3 --normalize (lead pre-flight, fastembed 0.8.0) --------

def test_normalize_flag_l2_normalises_indexed_vectors_and_the_query_embedder(tmp_path, monkeypatch):
    """E3.1: fastembed 0.8.0 normalises the OUTPUT of only some models (measured
    by the lead: bge-small 1.0000, the multilingual candidates 2.0-28), and
    notes_vec is declared `vec0(path TEXT PRIMARY KEY, embedding FLOAT[N])`
    without distance_metric (search.py:245-254) — sqlite-vec therefore scores
    it by plain L2, so an unnormalised candidate is measured with a handicap.
    The fake embedder here returns vectors whose norm depends on text length
    (never already unit), so a passing assertion proves the wrapper rescaled
    them rather than happening to already be normalised.

    The query path is checked by calling sb_search._embed_one directly rather
    than poking at search_vector's internals: after open_engine installs the
    wrapper it IS the function search_vector calls, so its return value is the
    same thing a real query embedding would produce."""
    from symbiosis_brain import search as sb_search
    from symbiosis_brain.storage import Storage

    db = eval_search.prepare_work_dir(DATA_DIR / "vault", tmp_path / "work")
    storage = Storage(db)
    try:
        engine = sb_search.SearchEngine(storage)
        if not engine._vec_enabled:
            pytest.skip("sqlite-vec unavailable — there is no index to rebuild")

        def fake_embed(texts):
            return [[float((len(t) + i) % 7) + 1.0 for i in range(8)] for t in texts]

        monkeypatch.setattr(sb_search, "_MODEL_NAME", "BAAI/bge-small-en-v1.5")
        monkeypatch.setattr(sb_search, "_embedder", None)
        monkeypatch.setattr(sb_search, "_embed", fake_embed)
        monkeypatch.setattr(sb_search, "_embed_one", lambda text: fake_embed([text])[0])

        eval_search._install_embed_wrappers(sb_search, None, None, True)
        eval_search.rebuild_vector_index(engine, storage, model="fixture/model-eight")

        rows = storage._conn.execute("SELECT embedding FROM notes_vec").fetchall()
        assert rows, "rebuild_vector_index wrote nothing to notes_vec"
        for (blob,) in rows:
            floats = struct.unpack(f"<{len(blob) // 4}f", blob)
            norm = math.sqrt(sum(x * x for x in floats))
            assert math.isclose(norm, 1.0, abs_tol=1e-5), norm

        queried = sb_search._embed_one("does the query wrapper normalise too")
        q_norm = math.sqrt(sum(x * x for x in queried))
        assert math.isclose(q_norm, 1.0, abs_tol=1e-5), q_norm
    finally:
        storage.close()


def test_install_embed_wrappers_composes_doc_prefix_then_normalize(monkeypatch):
    """E3.2: composition is prefix -> embed -> normalize. The fake embedder
    records the exact text it was handed (proving the prefix ran BEFORE the
    embed call) and always returns a fixed non-unit vector (proving the
    rescale ran AFTER — on the model's output, not on its input)."""
    from symbiosis_brain import search as sb_search

    received: list[list[str]] = []

    def fake_embed(texts):
        texts = list(texts)
        received.append(texts)
        return [[3.0, 4.0] for _ in texts]   # norm 5, deliberately not unit

    monkeypatch.setattr(sb_search, "_embed", fake_embed)
    monkeypatch.setattr(sb_search, "_embed_one", lambda text: fake_embed([text])[0])

    eval_search._install_embed_wrappers(sb_search, "query: ", "passage: ", True)

    doc_vecs = sb_search._embed(["note body"])
    assert received[-1] == ["passage: note body"], received[-1]
    assert doc_vecs[0] == pytest.approx([0.6, 0.8])

    query_vec = sb_search._embed_one("a question")
    assert received[-1] == ["query: a question"], received[-1]
    assert query_vec == pytest.approx([0.6, 0.8])


def test_normalize_without_a_model_is_an_eval_error(tmp_path):
    """E3.3: --normalize rewrites the embedder that --model itself swaps in;
    without --model the installed index still holds raw vectors, and
    normalising only the query would compare unlike with unlike."""
    with pytest.raises(eval_search.EvalError, match="normalize"):
        eval_search.run_eval(
            vault=DATA_DIR / "vault", work_dir=tmp_path / "work",
            queries=DATA_DIR / "queries.jsonl",
            config_names=["fts-any"], normalize=True)


def test_normalize_flag_defaults_to_off_and_leaves_meta_untouched(tmp_path):
    payload = eval_search.run_eval(
        vault=DATA_DIR / "vault", work_dir=tmp_path / "work",
        queries=DATA_DIR / "queries.jsonl",
        config_names=["fts-any"])
    assert "normalize" not in payload["meta"]
    assert payload["results"][0]["config"] == "fts-any"


def test_normalize_flag_appends_a_label_suffix_after_prefixed_and_sets_meta(
        tmp_path, monkeypatch, synthetic_engine):
    """E3.4: the label carries +norm AFTER +prefixed (lead directive example:
    hybrid-any+multilingual-e5-large+prefixed+norm), and meta.normalize is
    written only when the flag is actually on."""
    if not synthetic_engine._vec_enabled:
        pytest.skip("sqlite-vec unavailable — there is no index to rebuild")

    from symbiosis_brain import search as sb_search

    def fake_embed(texts):
        return [[float((len(t) + i) % 7) + 1.0 for i in range(8)] for t in texts]

    monkeypatch.setattr(sb_search, "_MODEL_NAME", "BAAI/bge-small-en-v1.5")
    monkeypatch.setattr(sb_search, "_embedder", None)
    monkeypatch.setattr(sb_search, "_embed", fake_embed)
    monkeypatch.setattr(sb_search, "_embed_one", lambda text: fake_embed([text])[0])

    payload = eval_search.run_eval(
        vault=DATA_DIR / "vault", work_dir=tmp_path / "work",
        queries=DATA_DIR / "queries.jsonl",
        config_names=["vec-current"],
        model="fixture/model-eight",
        query_prefix="query: ", doc_prefix="passage: ",
        normalize=True)

    assert payload["results"][0]["config"].endswith("+prefixed+norm"), \
        payload["results"][0]["config"]
    assert payload["meta"]["normalize"] is True


def test_the_report_stays_quiet_about_normalize_the_payload_does_not_carry():
    text = eval_search.render_report(_payload_for_render())
    assert "normalize" not in text


def test_the_report_prints_the_normalize_line_when_present():
    payload = _payload_for_render()
    payload["meta"]["normalize"] = True
    text = eval_search.render_report(payload)
    assert "normalize" in text
    assert "L2-normalised" in text


# ---------- CP-8b addendum: m1 peak RSS always, M4.2 notes/notes_vec in meta -

def test_peak_rss_is_recorded_even_without_a_model_swap(tmp_path):
    """m1 (pre-flight addendum): hybrid-any+rerank downloads a 1.04 GB
    cross-encoder without ever touching --model, so a run that never swaps the
    embedder would otherwise report no RSS at all for that download.
    reindex_seconds stays --model-only — there is no rebuild to time without
    one."""
    payload = eval_search.run_eval(
        vault=DATA_DIR / "vault", work_dir=tmp_path / "work",
        queries=DATA_DIR / "queries.jsonl",
        config_names=["fts-any"])
    assert "peak_rss_bytes" in payload["meta"]
    assert "reindex_seconds" not in payload["meta"]


def test_the_report_prints_peak_rss_even_when_no_model_was_swapped():
    """m1: the CP-8a payload shape (no reindex_seconds) must still get a peak
    RSS line once the key is present — the line no longer implies a rebuild
    happened."""
    payload = _payload_for_render()
    payload["meta"]["peak_rss_bytes"] = 1_181_116_006
    text = eval_search.render_report(payload)
    assert "peak RSS" in text
    assert "1.10 GiB" in text or "1.1 GiB" in text
    assert "reindex" not in text


def test_meta_carries_notes_and_notes_vec_counts_and_they_agree_on_the_synthetic_set(
        tmp_path, synthetic_engine):
    """M4.2 (pre-flight addendum): notes vs notes_vec drift is a red flag for
    the run that produced these numbers — printing both on every run turns a
    silent drift into something visible instead of a report six weeks later."""
    if not synthetic_engine._vec_enabled:
        pytest.skip("sqlite-vec unavailable — notes_vec has nothing to count")
    payload = eval_search.run_eval(
        vault=DATA_DIR / "vault", work_dir=tmp_path / "work",
        queries=DATA_DIR / "queries.jsonl",
        config_names=["fts-any"])
    assert payload["meta"]["notes"] > 0
    assert payload["meta"]["notes_vec"] == payload["meta"]["notes"]


def test_the_report_prints_the_index_line_with_no_drift_mark_when_counts_agree():
    payload = _payload_for_render()
    payload["meta"]["notes"] = 14
    payload["meta"]["notes_vec"] = 14
    text = eval_search.render_report(payload)
    assert "index" in text and "14 notes" in text and "14 vectors" in text
    assert "⚠" not in text


def test_the_report_flags_index_drift_between_notes_and_notes_vec():
    payload = _payload_for_render()
    payload["meta"]["notes"] = 14
    payload["meta"]["notes_vec"] = 12
    text = eval_search.render_report(payload)
    assert "14 notes" in text and "12 vectors" in text
    assert "⚠" in text


def test_the_report_stays_quiet_about_the_index_line_the_payload_does_not_carry():
    text = eval_search.render_report(_payload_for_render())
    assert "index " not in text


@pytest.mark.skipif(sys.platform != "win32", reason="Windows counter")
def test_peak_rss_is_actually_read_on_windows():
    """test_peak_rss_is_a_positive_number_or_an_honest_none accepts None as a
    pass, so it stays green even when the Windows counter is silently broken.
    On win32 a None is not an honest answer, it is a bug: GetCurrentProcess()
    returns a pseudo-handle that ctypes truncates to 32 bits without explicit
    argtypes, GetProcessMemoryInfo then fails on the truncated handle, and the
    wrapper's `except Exception: return None` swallows it. Assert the number
    is actually read — this process (pytest, already running) is well over
    10 MiB of working set, so a low bar like ">0" would pass on a truncated
    zero just as easily as on a truncated garbage value."""
    value = eval_search.peak_rss_bytes()
    assert isinstance(value, int) and value > 0
    assert value > 10 * 1024 * 1024, value
