"""Tests for the action-rules compiler (Stage 1 action-recall).

compile_action_rules() merges the default + local tool-routing catalogs,
keeps only class:"action" routes with command_triggers, validates each
regex against its own test_match/test_nomatch vectors via `grep -E`
(the rules are POSIX ERE, not Python `re` — see module docstring), and
writes a flat TSV + a meta.json summary under <vault>/.index/.
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from symbiosis_brain import action_rules as ar

pytestmark = pytest.mark.skipif(
    shutil.which("grep") is None, reason="grep not on PATH — cannot validate ERE rules"
)


def _write_local(vault: Path, routes: list[dict]) -> None:
    (vault / "tool-routing.local.json").write_text(
        json.dumps(routes, ensure_ascii=False), encoding="utf-8"
    )


VALID_RULE = {
    "id": "git-reset-hard-after-fetch",
    "class": "action",
    "priority": 84,
    "command_triggers": {
        "bash": [{"re": "(^|[;&|]+)[[:space:]]*git[[:space:]]+reset[[:space:]]+--hard[[:space:]]+origin/"}],
        "powershell": [{"re": "(^|[;&|]+)[[:space:]]*git[[:space:]]+reset[[:space:]]+--hard[[:space:]]+origin/"}],
    },
    "hint": "Never reset --hard origin/* right after fetch: a failed fetch leaves a stale ref. [[mistakes/x]]",
    "test_match": {
        "bash": ["git reset --hard origin/main"],
        "powershell": ["git reset --hard origin/main"],
    },
    "test_nomatch": {
        "bash": ["git reset --soft HEAD~1"],
        "powershell": ["git reset HEAD README.md"],
    },
}

CYRILLIC_RULE = {
    "id": "kill-process-by-name-hits-mcp",
    "class": "action",
    "priority": 88,
    "command_triggers": {
        "bash": [{"re": "(^|[;&|]+)[[:space:]]*[Tt][Aa][Ss][Kk][Kk][Ii][Ll][Ll].*(/|//)+[Ii][Mm][[:space:]]*python"}],
    },
    "hint": "Убей процесс по PID, не по имени — так убивается и MCP-сервер python.\nСмотри [[mistakes/kill-process-by-name-killed-mcp-servers]].",
    "test_match": {"bash": ["taskkill //IM python.exe //F"]},
    "test_nomatch": {"bash": ["taskkill //PID 1234 //F"]},
}

BAD_VECTOR_RULE = {
    "id": "bad-vector-rule",
    "class": "action",
    "priority": 70,
    "command_triggers": {"bash": [{"re": "^echo hi$"}]},
    "hint": "should be dropped — nomatch vector actually matches",
    "test_match": {"bash": ["echo hi"]},
    "test_nomatch": {"bash": ["echo hi"]},  # matches -> must be excluded
}

BAD_MATCH_RULE = {
    "id": "bad-match-rule",
    "class": "action",
    "priority": 65,
    "command_triggers": {"bash": [{"re": "^this-will-never-match-xyz$"}]},
    "hint": "should be dropped — match vector does not match",
    "test_match": {"bash": ["totally different command"]},
    "test_nomatch": {"bash": ["echo hi"]},
}

TAB_RULE = {
    "id": "tab-in-regex-rule",
    "class": "action",
    "priority": 60,
    "command_triggers": {"bash": [{"re": "^echo\thi$"}]},
    "hint": "should be dropped — tab char would corrupt the TSV",
    "test_match": {"bash": ["echo\thi"]},
    "test_nomatch": {"bash": ["echo hi"]},
}


def test_valid_compilation_writes_tsv_and_meta(tmp_path):
    _write_local(tmp_path, [VALID_RULE, CYRILLIC_RULE])
    out = ar.compile_action_rules(tmp_path)

    assert out == tmp_path / ".index" / "action-rules.tsv"
    assert out.exists()
    rows = [
        line.split("\t")
        for line in out.read_text(encoding="utf-8").splitlines()
        if line
    ]
    # git-reset rule has both bash+powershell triggers -> 2 rows; kill-process only bash -> 1 row
    assert len(rows) == 3
    for row in rows:
        assert len(row) == 4
    ids = {row[1] for row in rows}
    assert ids == {"git-reset-hard-after-fetch", "kill-process-by-name-hits-mcp"}
    tools = {row[0] for row in rows}
    assert tools == {"bash", "powershell"}

    meta_path = tmp_path / ".index" / "action-rules.meta.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    assert meta["rules_total"] == 2
    assert meta["rules_compiled"] == 2
    assert meta["skipped"] == []
    assert "compiled_at" in meta
    # ISO-8601 parseable
    import datetime as _dt
    _dt.datetime.fromisoformat(meta["compiled_at"])


def test_failing_test_match_vector_excludes_rule(tmp_path):
    _write_local(tmp_path, [VALID_RULE, BAD_MATCH_RULE])
    ar.compile_action_rules(tmp_path)
    meta = json.loads((tmp_path / ".index" / "action-rules.meta.json").read_text(encoding="utf-8"))
    assert meta["rules_total"] == 2
    assert meta["rules_compiled"] == 1
    skipped_ids = {s["id"] for s in meta["skipped"]}
    assert "bad-match-rule" in skipped_ids


def test_failing_test_nomatch_vector_excludes_rule(tmp_path):
    _write_local(tmp_path, [VALID_RULE, BAD_VECTOR_RULE])
    ar.compile_action_rules(tmp_path)
    meta = json.loads((tmp_path / ".index" / "action-rules.meta.json").read_text(encoding="utf-8"))
    assert meta["rules_compiled"] == 1
    skipped_ids = {s["id"] for s in meta["skipped"]}
    assert "bad-vector-rule" in skipped_ids


def test_tab_in_regex_excludes_rule(tmp_path):
    _write_local(tmp_path, [VALID_RULE, TAB_RULE])
    out = ar.compile_action_rules(tmp_path)
    meta = json.loads((tmp_path / ".index" / "action-rules.meta.json").read_text(encoding="utf-8"))
    skipped_ids = {s["id"] for s in meta["skipped"]}
    assert "tab-in-regex-rule" in skipped_ids
    # and no literal tab survived into the TSV (would corrupt column count)
    for line in out.read_text(encoding="utf-8").splitlines():
        cols = line.split("\t")
        assert len(cols) == 4


def test_hint_tsv_roundtrip_and_cyrillic_and_wikilink(tmp_path):
    _write_local(tmp_path, [CYRILLIC_RULE])
    out = ar.compile_action_rules(tmp_path)
    lines = [ln for ln in out.read_text(encoding="utf-8").splitlines() if ln]
    assert len(lines) == 1
    tool, rule_id, regex, hint_field = lines[0].split("\t")
    assert tool == "bash"
    assert rule_id == "kill-process-by-name-hits-mcp"
    # hint_field is ready to be dropped straight into a JSON string literal
    restored = json.loads('"' + hint_field + '"')
    assert restored == (
        "Убей процесс по PID, не по имени — так убивается и MCP-сервер python. "
        "Смотри [[mistakes/kill-process-by-name-killed-mcp-servers]]."
    )
    assert "\n" not in hint_field
    assert "\t" not in hint_field


def test_order_by_priority_desc_then_id_asc(tmp_path):
    low = dict(BAD_MATCH_RULE)  # will be excluded, not part of ordering
    r_a = {
        "id": "zzz-low-priority",
        "class": "action",
        "priority": 10,
        "command_triggers": {"bash": [{"re": "^alpha$"}]},
        "hint": "a",
        "test_match": {"bash": ["alpha"]},
        "test_nomatch": {"bash": ["beta"]},
    }
    r_b = {
        "id": "aaa-high-priority",
        "class": "action",
        "priority": 99,
        "command_triggers": {"bash": [{"re": "^beta$"}]},
        "hint": "b",
        "test_match": {"bash": ["beta"]},
        "test_nomatch": {"bash": ["alpha"]},
    }
    r_c = {
        "id": "bbb-mid-priority",
        "class": "action",
        "priority": 50,
        "command_triggers": {"bash": [{"re": "^gamma$"}]},
        "hint": "c",
        "test_match": {"bash": ["gamma"]},
        "test_nomatch": {"bash": ["delta"]},
    }
    _write_local(tmp_path, [r_a, r_b, r_c])
    out = ar.compile_action_rules(tmp_path)
    ids = [ln.split("\t")[1] for ln in out.read_text(encoding="utf-8").splitlines() if ln]
    assert ids == ["aaa-high-priority", "bbb-mid-priority", "zzz-low-priority"]


def test_grep_unavailable_skips_without_raising(tmp_path, monkeypatch):
    _write_local(tmp_path, [VALID_RULE])
    monkeypatch.setattr(ar, "_find_grep", lambda: None)
    out = ar.compile_action_rules(tmp_path)
    assert out.exists()
    assert out.read_text(encoding="utf-8") == ""
    meta = json.loads((tmp_path / ".index" / "action-rules.meta.json").read_text(encoding="utf-8"))
    assert meta["rules_compiled"] == 0
    assert meta["rules_total"] == 1
    assert meta["skipped"][0]["id"] == "git-reset-hard-after-fetch"
    assert "grep" in meta["skipped"][0]["reason"].lower()


def test_no_action_routes_writes_empty_tsv(tmp_path):
    _write_local(tmp_path, [])
    out = ar.compile_action_rules(tmp_path)
    assert out.exists()
    assert out.read_text(encoding="utf-8") == ""
    meta = json.loads((tmp_path / ".index" / "action-rules.meta.json").read_text(encoding="utf-8"))
    assert meta["rules_total"] == 0
    assert meta["rules_compiled"] == 0


def test_missing_local_override_still_compiles_defaults_only(tmp_path):
    # No tool-routing.local.json at all — merged catalog is just the shipped
    # default (augment/supersede only, no command_triggers) -> empty TSV.
    out = ar.compile_action_rules(tmp_path)
    assert out.exists()
    meta = json.loads((tmp_path / ".index" / "action-rules.meta.json").read_text(encoding="utf-8"))
    assert meta["rules_total"] == 0


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash not on PATH")
def test_hook_bash_block_matches_compiled_rule(tmp_path):
    """End-to-end: compiled TSV + hook script's pure-bash matcher fires
    without invoking uv/python, and records a hit."""
    import os
    import subprocess

    _write_local(tmp_path, [VALID_RULE])
    ar.compile_action_rules(tmp_path)

    repo_root = Path(__file__).resolve().parents[1]
    hook = repo_root / "hooks" / "brain-pre-action-trigger.sh"

    env = dict(os.environ)
    env["SYMBIOSIS_BRAIN_VAULT"] = str(tmp_path)
    env.pop("SYMBIOSIS_BRAIN_TOOLS", None)  # force: must not need uv/python
    env.pop("SYMBIOSIS_BRAIN_PRE_ACTION_DISABLED", None)
    env["TMPDIR"] = str(tmp_path)
    env["TEMP"] = str(tmp_path)

    # Compact JSON (no spaces after ':'/',') — matches the real Claude Code
    # PreToolUse payload shape the fixed grep pattern in the hook expects.
    payload = json.dumps({
        "tool_name": "Bash",
        "tool_input": {"command": "git reset --hard origin/main"},
        "session_id": "hooktest",
    }, separators=(",", ":"))
    result = subprocess.run(
        ["bash", str(hook)], input=payload, text=True, encoding="utf-8",
        env=env, capture_output=True, timeout=15,
    )
    assert result.returncode == 0, result.stderr
    out = json.loads(result.stdout.strip())
    ctx = out["hookSpecificOutput"]["additionalContext"]
    assert "[action-rule git-reset-hard-after-fetch]" in ctx
    assert "stale ref" in ctx

    hits_path = tmp_path / ".index" / "action-rule-hits.jsonl"
    assert hits_path.exists()
    hit = json.loads(hits_path.read_text(encoding="utf-8").strip().splitlines()[-1])
    assert hit["rule_id"] == "git-reset-hard-after-fetch"
    assert hit["tool"] == "bash"
    assert hit["session_id"] == "hooktest"


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash not on PATH")
def test_hook_bash_block_no_match_falls_through_silently(tmp_path):
    import os
    import subprocess

    _write_local(tmp_path, [VALID_RULE])
    ar.compile_action_rules(tmp_path)

    repo_root = Path(__file__).resolve().parents[1]
    hook = repo_root / "hooks" / "brain-pre-action-trigger.sh"

    env = dict(os.environ)
    env["SYMBIOSIS_BRAIN_VAULT"] = str(tmp_path)
    env.pop("SYMBIOSIS_BRAIN_TOOLS", None)
    env.pop("SYMBIOSIS_BRAIN_PRE_ACTION_DISABLED", None)
    env["TMPDIR"] = str(tmp_path)
    env["TEMP"] = str(tmp_path)

    payload = json.dumps({
        "tool_name": "Bash",
        "tool_input": {"command": "ls -la"},
        "session_id": "hooktest2",
    }, separators=(",", ":"))
    result = subprocess.run(
        ["bash", str(hook)], input=payload, text=True, encoding="utf-8",
        env=env, capture_output=True, timeout=15,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == ""


def test_cli_compile_action_rules_subcommand(tmp_path):
    """`python -m symbiosis_brain compile-action-rules --vault X` writes the
    TSV and exits 0, matching the other subcommands' contract."""
    import subprocess
    import sys

    _write_local(tmp_path, [VALID_RULE])
    repo_root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [sys.executable, "-m", "symbiosis_brain", "compile-action-rules", "--vault", str(tmp_path)],
        capture_output=True, text=True, encoding="utf-8", cwd=str(repo_root), timeout=60,
    )
    assert result.returncode == 0, result.stderr
    tsv_path = tmp_path / ".index" / "action-rules.tsv"
    assert tsv_path.exists()
    assert result.stdout.strip() == str(tsv_path)


def test_cli_compile_action_rules_missing_vault_arg_fails_open(tmp_path):
    import subprocess
    import sys

    repo_root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [sys.executable, "-m", "symbiosis_brain", "compile-action-rules"],
        capture_output=True, text=True, encoding="utf-8", cwd=str(repo_root), timeout=60,
    )
    assert result.returncode == 0
