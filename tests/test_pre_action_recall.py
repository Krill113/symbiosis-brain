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


# ---------- _note_type ----------

from symbiosis_brain.pre_action_recall import _note_type


def test_note_type_reads_from_frontmatter():
    assert _note_type({"frontmatter": {"type": "feedback"}}) == "feedback"


def test_note_type_returns_none_when_no_frontmatter():
    assert _note_type({"path": "x"}) is None


def test_note_type_returns_none_when_frontmatter_is_string():
    # Edge case: frontmatter could be raw string in some storage paths
    assert _note_type({"frontmatter": "raw yaml string"}) is None


def test_note_type_returns_none_when_frontmatter_is_none():
    assert _note_type({"frontmatter": None}) is None


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


# ---------- CLI subcommand integration ----------

import json
import os
import subprocess
import sys
from pathlib import Path


@pytest.fixture
def populated_vault(tmp_path: Path) -> Path:
    """Minimal vault with one feedback note + pre-built indexes (FTS+vector).

    Production hook does NOT call index_all() — too slow. In real use, vector
    index is prewarmed at SessionStart and persisted. Tests must build it
    explicitly to mirror that warmed state.
    """
    from symbiosis_brain.storage import Storage
    from symbiosis_brain.search import SearchEngine
    from symbiosis_brain.sync import VaultSync

    vault = tmp_path / "vault"
    (vault / "feedback").mkdir(parents=True)
    note = vault / "feedback" / "commit-style.md"
    note.write_text(
        "---\n"
        "name: commit-style\n"
        "type: feedback\n"
        "scope: global\n"
        "gist: Не добавляй Co-Authored-By Claude в коммиты\n"
        "---\n\n"
        "# Commit style\n\n"
        "Без AI-trailer'ов. Никаких [[wiki/claude-code]] упоминаний в commit message.\n"
        "Применяется для git commit / git push команд.\n",
        encoding="utf-8",
    )

    # Pre-build indexes (mirrors prewarmed SessionStart state)
    db_path = vault / ".index" / "brain.db"
    storage = Storage(db_path)
    VaultSync(vault, storage).sync_all()
    SearchEngine(storage).index_all()

    return vault


def _run_cli(stdin_payload: dict, vault: Path) -> tuple[int, str, str]:
    """Run `python -m symbiosis_brain pre-action-recall` with payload via stdin pipe."""
    proc = subprocess.run(
        [sys.executable, "-m", "symbiosis_brain", "pre-action-recall",
         "--vault", str(vault)],
        input=json.dumps(stdin_payload),
        capture_output=True, text=True, timeout=30,
    )
    return proc.returncode, proc.stdout, proc.stderr


def test_cli_emits_additional_context_for_bash_commit(populated_vault: Path):
    payload = {
        "tool_name": "Bash",
        "tool_input": {"command": "git commit -m 'feat: x'"},
        "session_id": "test-session-123",
    }
    rc, out, _ = _run_cli(payload, populated_vault)
    assert rc == 0
    assert "additionalContext" in out
    data = json.loads(out)
    assert data["hookSpecificOutput"]["hookEventName"] == "PreToolUse"
    assert "[recall:" in data["hookSpecificOutput"]["additionalContext"]


def test_cli_emits_nothing_for_bash_ls(populated_vault: Path):
    payload = {
        "tool_name": "Bash",
        "tool_input": {"command": "ls -la"},
        "session_id": "test-session-123",
    }
    rc, out, _ = _run_cli(payload, populated_vault)
    assert rc == 0
    assert out.strip() == ""  # silent skip — not in whitelist


def test_cli_emits_nothing_for_unknown_tool(populated_vault: Path):
    payload = {
        "tool_name": "Read",
        "tool_input": {"file_path": "/x"},
        "session_id": "test-session-123",
    }
    rc, out, _ = _run_cli(payload, populated_vault)
    assert rc == 0
    assert out.strip() == ""


def test_cli_emits_nothing_when_kill_switch_set(populated_vault: Path, monkeypatch):
    monkeypatch.setenv("SYMBIOSIS_BRAIN_PRE_ACTION_DISABLED", "1")
    payload = {
        "tool_name": "Task",
        "tool_input": {"prompt": "anything"},
        "session_id": "test-session-123",
    }
    rc, out, _ = _run_cli(payload, populated_vault)
    assert rc == 0
    assert out.strip() == ""


def test_cli_handles_malformed_stdin_json(populated_vault: Path):
    proc = subprocess.run(
        [sys.executable, "-m", "symbiosis_brain", "pre-action-recall",
         "--vault", str(populated_vault)],
        input="{not valid",
        capture_output=True, text=True, timeout=10,
    )
    assert proc.returncode == 0  # fail-open
    assert proc.stdout.strip() == ""  # silent skip


def test_cli_handles_bad_argparse_args(populated_vault: Path):
    """argparse sys.exit(2) on bad args must be caught — fail-open as exit 0."""
    proc = subprocess.run(
        [sys.executable, "-m", "symbiosis_brain", "pre-action-recall",
         "--unknown-flag", "xyz"],
        input="{}",
        capture_output=True, text=True, timeout=10,
    )
    assert proc.returncode == 0  # NOT 2
    assert proc.stdout.strip() == ""


def test_cli_handles_missing_vault_arg(populated_vault: Path):
    """--vault is required by argparse — missing should fail-open."""
    proc = subprocess.run(
        [sys.executable, "-m", "symbiosis_brain", "pre-action-recall"],
        input="{}",
        capture_output=True, text=True, timeout=10,
    )
    assert proc.returncode == 0  # NOT 2
    assert proc.stdout.strip() == ""
