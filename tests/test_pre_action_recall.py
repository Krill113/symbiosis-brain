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


@pytest.fixture(autouse=True)
def _isolate_recall_tmp(tmp_path_factory, monkeypatch):
    """Route recall seen-files + debug log into a throwaway dir so dedup state
    (seen-file keyed by session_id in the shared OS temp) never leaks across
    tests or repeated runs. Without this, a second run within the TTL window
    would see hits as 'already shown' and suppress recall → flaky."""
    d = tmp_path_factory.mktemp("recall-tmp")
    monkeypatch.setenv("TMPDIR", str(d))
    monkeypatch.setenv("TEMP", str(d))


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


# ---------- format ★ STRONG marker (Stage 1) ----------

def test_format_marks_strong_hits_with_star():
    hits = [
        {"path": "a/strong", "gist": "g1", "_in_both": True},
        {"path": "b/weak", "gist": "g2", "_in_both": False},
        {"path": "c/legacy", "gist": "g3"},  # missing _in_both → no star
    ]
    out = format_recall_block("q", hits)
    lines = out.splitlines()
    strong = next(l for l in lines if "a/strong" in l)
    weak = next(l for l in lines if "b/weak" in l)
    legacy = next(l for l in lines if "c/legacy" in l)
    assert "★" in strong
    assert "★" not in weak
    assert "★" not in legacy


# ---------- run_recall dedup (Stage 1) ----------

class _FakeSeen:
    def __init__(self, already=()):
        self.already = set(already)
        self.recorded: list = []

    def is_seen(self, path: str) -> bool:
        return path in self.already

    def record(self, paths):
        self.recorded.extend(paths)


def _feedback_hits(n: int) -> list:
    return [
        {"path": f"p/{i}", "title": f"T{i}", "scope": "global",
         "gist": f"g{i}", "frontmatter": {"type": "feedback"}}
        for i in range(n)
    ]


def test_run_recall_dedup_skips_seen_and_fills_cap():
    cfg = PreActionConfig(hit_limit=3)
    engine = MagicMock()
    engine.search.return_value = _feedback_hits(5)
    seen = _FakeSeen(already=["p/0", "p/1"])
    hits = run_recall("q", None, cfg, engine, seen=seen)
    paths = [h["path"] for h in hits]
    assert paths == ["p/2", "p/3", "p/4"]  # seen skipped, fresh fill cap of 3
    assert set(seen.recorded) == {"p/2", "p/3", "p/4"}  # only emitted recorded


def test_run_recall_no_dedup_when_seen_none():
    cfg = PreActionConfig(hit_limit=2)
    engine = MagicMock()
    engine.search.return_value = _feedback_hits(5)
    hits = run_recall("q", None, cfg, engine, seen=None)
    assert [h["path"] for h in hits] == ["p/0", "p/1"]  # unchanged behaviour


def test_run_recall_dedup_disabled_by_config():
    cfg = PreActionConfig(hit_limit=3, recall_dedup_enabled=False)
    engine = MagicMock()
    engine.search.return_value = _feedback_hits(5)
    seen = _FakeSeen(already=["p/0"])
    hits = run_recall("q", None, cfg, engine, seen=seen)
    assert hits[0]["path"] == "p/0"  # dedup off → p/0 not skipped
    assert seen.recorded == []  # nothing recorded when disabled


# ---------- CLI dedup integration (Stage 1, live behaviour) ----------

def test_cli_dedup_suppresses_repeat_within_session(populated_vault: Path):
    payload = {
        "tool_name": "Bash",
        "tool_input": {"command": "git commit -m 'feat: x'"},
        "session_id": "dedup-sess-1",
    }
    rc1, out1, _ = _run_cli(payload, populated_vault)
    assert rc1 == 0 and "[recall:" in out1  # first call emits
    # Pin the cause to dedup (not incidental emptiness): call 1 actually wrote
    # a seen-file into the isolated tmp.
    seen_dir = Path(os.environ["TMPDIR"])
    assert list(seen_dir.glob("brain-recall-seen-*.json")), "dedup seen-file not written"
    rc2, out2, _ = _run_cli(payload, populated_vault)
    assert rc2 == 0
    assert out2.strip() == ""  # repeat within TTL suppressed (all hits seen)


def test_cli_dedup_independent_across_sessions(populated_vault: Path):
    base = {"tool_name": "Bash", "tool_input": {"command": "git commit -m x"}}
    rc1, out1, _ = _run_cli({**base, "session_id": "s-A"}, populated_vault)
    rc2, out2, _ = _run_cli({**base, "session_id": "s-B"}, populated_vault)
    assert "[recall:" in out1
    assert "[recall:" in out2  # different session → not suppressed


class _RaisingSeen:
    """Dedup store whose every method raises — pins run_recall's fail-open."""

    def is_seen(self, path):
        raise RuntimeError("boom")

    def record(self, paths):
        raise RuntimeError("boom")


def test_run_recall_fail_open_when_seen_raises():
    cfg = PreActionConfig(hit_limit=3)
    engine = MagicMock()
    engine.search.return_value = _feedback_hits(5)
    # A raising dedup store must NOT propagate or empty recall — degrade to
    # un-deduped cap-top-N (locked constraint: a dedup error never empties recall).
    hits = run_recall("q", None, cfg, engine, seen=_RaisingSeen())
    assert [h["path"] for h in hits] == ["p/0", "p/1", "p/2"]
