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
