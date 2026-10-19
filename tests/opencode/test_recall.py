from symbiosis_brain.adapters.opencode import recall as adapter
from symbiosis_brain.pre_action_recall import run_recall as core_run


def test_lookup_does_not_consume_delivery_dedup(monkeypatch):
    calls = []

    def traced(query, scope, config, engine, seen=None, *, fts_mode="all_then_any", log_ctx=None):
        calls.append((seen, log_ctx))
        return core_run(query, scope, config, engine, seen=seen, fts_mode=fts_mode, log_ctx=log_ctx)

    monkeypatch.setattr(adapter, "run_recall", traced)

    class Engine:
        def search(self, **kwargs):
            assert kwargs["scope"] == "alpha"
            assert kwargs["include_closed"] is False
            return [{"path": "alpha/lesson.md", "note_type": "pattern", "scope": "alpha",
                     "title": "Fixture lesson", "gist": "Use the prepared lookup."}]

    engine = Engine()
    first = adapter.recall_hits("prepared lookup", "alpha", engine, limit=5)
    second = adapter.recall_hits("prepared lookup", "alpha", engine, limit=5)
    assert first == second
    assert first == [{"path": "alpha/lesson.md", "note_type": "pattern", "scope": "alpha",
                      "title": "Fixture lesson", "gist": "Use the prepared lookup."}]
    assert calls == [(None, None), (None, None)]


def test_recall_requires_explicit_scope():
    import pytest

    with pytest.raises(ValueError, match="explicit scope required"):
        adapter.recall_hits("query", "", object(), limit=3)
