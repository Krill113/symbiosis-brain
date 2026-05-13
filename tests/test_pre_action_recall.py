"""Tests for pre_action_recall orchestrator + CLI subcommand (B1 hook)."""
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from symbiosis_brain.pre_action_recall import (
    build_query,
    format_recall_block,
    run_recall,
)
from symbiosis_brain.pre_action_config import PreActionConfig


# ---------- build_query ----------

def test_query_from_task_uses_prompt():
    q = build_query("Task", {"prompt": "find the bug in foo"}, max_chars=500)
    assert q == "find the bug in foo"


def test_query_from_edit_combines_path_and_new_string():
    q = build_query(
        "Edit",
        {"file_path": "/x/y.py", "new_string": "def foo(): pass"},
        max_chars=500,
    )
    assert "/x/y.py" in q
    assert "def foo" in q


def test_query_from_write_combines_path_and_content():
    q = build_query(
        "Write",
        {"file_path": "/x/y.py", "content": "import os\nprint(1)"},
        max_chars=500,
    )
    assert "/x/y.py" in q
    assert "import os" in q


def test_query_from_multiedit_concatenates_new_strings():
    q = build_query(
        "MultiEdit",
        {
            "file_path": "/x/y.py",
            "edits": [
                {"new_string": "A"},
                {"new_string": "B"},
            ],
        },
        max_chars=500,
    )
    assert "A" in q and "B" in q
    assert "/x/y.py" in q


def test_query_from_bash_uses_command():
    q = build_query("Bash", {"command": "git commit -m fix"}, max_chars=500)
    assert q == "git commit -m fix"


def test_query_from_notebookedit_uses_new_source():
    q = build_query("NotebookEdit", {"new_source": "import pandas"}, max_chars=500)
    assert q == "import pandas"


def test_query_truncated_to_max_chars():
    long = "x" * 1000
    q = build_query("Task", {"prompt": long}, max_chars=500)
    assert q is not None and len(q) <= 500


def test_query_unknown_tool_returns_none():
    assert build_query("Read", {"file_path": "x"}, max_chars=500) is None


def test_query_missing_field_returns_none_or_empty():
    # Task with no prompt → empty string → caller treats as "no query"
    assert build_query("Task", {}, max_chars=500) == ""


# ---------- format_recall_block ----------

def test_format_with_hits():
    hits = [
        {"path": "feedback/x", "type": "feedback", "gist": "do this"},
        {"path": "mistake/y", "type": "mistake", "gist": "don't do that"},
    ]
    out = format_recall_block("git commit", hits)
    assert "[recall: 2 hits" in out
    assert "feedback/x" in out
    assert "mistake/y" in out
    assert "do this" in out
    assert 'git commit' in out


def test_format_empty_hits_returns_empty_string():
    assert format_recall_block("foo", []) == ""


def test_format_truncates_query_snippet():
    long = "x" * 200
    out = format_recall_block(long, [{"path": "a", "type": "wiki", "gist": "g"}])
    assert long not in out  # truncated
    assert "xxxx" in out  # snippet portion present


# ---------- run_recall (integration with SearchEngine via mock) ----------

def test_run_recall_filters_excluded_types():
    cfg = PreActionConfig(excluded_note_types=["user"])
    fake_engine = MagicMock()
    fake_engine.search.return_value = [
        {"path": "user/profile", "title": "Profile", "scope": "global",
         "gist": "...", "frontmatter": {"type": "user"}},
        {"path": "feedback/x", "title": "X", "scope": "global",
         "gist": "do this", "frontmatter": {"type": "feedback"}},
    ]
    hits = run_recall(
        query="anything",
        scope="global",
        config=cfg,
        engine=fake_engine,
    )
    assert len(hits) == 1
    assert hits[0]["path"] == "feedback/x"


def test_run_recall_respects_hit_limit():
    cfg = PreActionConfig(hit_limit=2)
    fake_engine = MagicMock()
    fake_engine.search.return_value = [
        {"path": f"p/{i}", "title": f"T{i}", "scope": "global",
         "gist": f"g{i}", "frontmatter": {"type": "feedback"}}
        for i in range(10)
    ]
    hits = run_recall(query="anything", scope="global", config=cfg, engine=fake_engine)
    assert len(hits) == 2


def test_run_recall_zero_results():
    cfg = PreActionConfig()
    fake_engine = MagicMock()
    fake_engine.search.return_value = []
    assert run_recall(query="x", scope=None, config=cfg, engine=fake_engine) == []
