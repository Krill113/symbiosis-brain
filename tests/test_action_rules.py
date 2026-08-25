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

# health-checked absolute bash; bare "bash" is the WSL stub on Windows CI.
# Package form for imports from the repo root, bare form under pytest prepend mode.
try:
    from tests._bash_resolver import _bash
except ImportError:  # pragma: no cover - pytest prepend mode
    from _bash_resolver import _bash

from symbiosis_brain import action_rules as ar

# Same resolver the compiler uses (PATH, then Git-for-Windows roots derived
# from bash/git) — a dev box whose PATH lacks Git\usr\bin still runs these.
pytestmark = pytest.mark.skipif(
    ar._find_grep() is None, reason="grep not found — cannot validate ERE rules"
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

# Synthetic throughout (id, hint, wikilink target, vectors) — fixtures never mirror
# a real vault or a real tool-routing.local.json; a Cyrillic hint with a newline and
# a wikilink is all the round-trip test needs.
CYRILLIC_RULE = {
    "id": "stop-daemon-by-image-name",
    "class": "action",
    "priority": 88,
    "command_triggers": {
        "bash": [{"re": "(^|[;&|]+)[[:space:]]*[Tt][Aa][Ss][Kk][Kk][Ii][Ll][Ll].*(/|//)+[Ii][Mm][[:space:]]*demo"}],
    },
    "hint": "Останавливай демон по PID, а не по имени образа — под тем же именем живут соседи.\nСмотри [[mistakes/y]].",
    "test_match": {"bash": ["taskkill //IM demo.exe //F"]},
    "test_nomatch": {"bash": ["taskkill //PID 42 //F"]},
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
    # git-reset rule has both bash+powershell triggers -> 2 rows; stop-daemon only bash -> 1 row
    assert len(rows) == 3
    for row in rows:
        assert len(row) == 4
    ids = {row[1] for row in rows}
    assert ids == {"git-reset-hard-after-fetch", "stop-daemon-by-image-name"}
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
    assert rule_id == "stop-daemon-by-image-name"
    # hint_field is ready to be dropped straight into a JSON string literal
    restored = json.loads('"' + hint_field + '"')
    assert restored == (
        "Останавливай демон по PID, а не по имени образа — под тем же именем живут соседи. "
        "Смотри [[mistakes/y]]."
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
        [_bash(),str(hook)], input=payload, text=True, encoding="utf-8",
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
        [_bash(),str(hook)], input=payload, text=True, encoding="utf-8",
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


# ── Compiler-level regression coverage for the adversarial-review fixes ──

def test_no_test_match_vectors_drops_the_rule(tmp_path):
    """A tool side with no test_match vectors must NOT compile 'trusted by
    default' — an unvalidated pattern could be overbroad and, because the
    hook exits on its first hit, would silently swallow every rule (and the
    normal python recall) behind it."""
    rule = {
        "id": "overbroad-no-vectors",
        "class": "action",
        "priority": 99,
        "command_triggers": {"bash": [{"re": ".*"}]},
        "hint": "fires on literally everything",
        # no test_match at all
        "test_nomatch": {"bash": ["echo hi"]},
    }
    _write_local(tmp_path, [rule])
    out = ar.compile_action_rules(tmp_path)
    assert out.read_text(encoding="utf-8") == ""
    meta = json.loads((tmp_path / ".index" / "action-rules.meta.json").read_text(encoding="utf-8"))
    assert meta["rules_compiled"] == 0
    skipped = {s["id"]: s["reason"] for s in meta["skipped"]}
    assert "overbroad-no-vectors" in skipped
    assert "test_match" in skipped["overbroad-no-vectors"]


def test_newline_in_regex_excludes_rule(tmp_path):
    """Only `\\t` was guarded before; a `\\n`/`\\r` in the pattern would
    corrupt the TSV (extra line) just as badly as a tab."""
    rule = dict(VALID_RULE)
    rule = {
        "id": "newline-in-regex-rule",
        "class": "action",
        "priority": 60,
        "command_triggers": {"bash": [{"re": "^foo$\n^bar$"}]},
        "hint": "should be dropped — embedded newline would corrupt the TSV",
        "test_match": {"bash": ["foo"]},
        "test_nomatch": {"bash": ["baz"]},
    }
    _write_local(tmp_path, [VALID_RULE, rule])
    out = ar.compile_action_rules(tmp_path)
    meta = json.loads((tmp_path / ".index" / "action-rules.meta.json").read_text(encoding="utf-8"))
    skipped_ids = {s["id"] for s in meta["skipped"]}
    assert "newline-in-regex-rule" in skipped_ids
    for line in out.read_text(encoding="utf-8").splitlines():
        assert len(line.split("\t")) == 4


def test_invalid_id_chars_excludes_rule(tmp_path):
    rule = {
        "id": 'evil"id',
        "class": "action",
        "priority": 60,
        "command_triggers": {"bash": [{"re": "^echo hi$"}]},
        "hint": "should be dropped — id has a quote in it",
        "test_match": {"bash": ["echo hi"]},
        "test_nomatch": {"bash": ["echo bye"]},
    }
    _write_local(tmp_path, [rule])
    out = ar.compile_action_rules(tmp_path)
    assert out.read_text(encoding="utf-8") == ""
    meta = json.loads((tmp_path / ".index" / "action-rules.meta.json").read_text(encoding="utf-8"))
    assert meta["rules_compiled"] == 0
    reasons = " ".join(s["reason"] for s in meta["skipped"])
    assert "id" in reasons


def test_fast_reject_files_written_per_toolkey(tmp_path):
    _write_local(tmp_path, [VALID_RULE, CYRILLIC_RULE])
    ar.compile_action_rules(tmp_path)
    bash_re = (tmp_path / ".index" / "action-rules.bash.re").read_text(encoding="utf-8")
    ps_re = (tmp_path / ".index" / "action-rules.powershell.re").read_text(encoding="utf-8")
    # both bash rows land in the bash fast-reject file (git-reset + taskkill)
    assert bash_re.count("\n") == 2
    assert "git" in bash_re and "askkill" in bash_re.lower() or "Tt" in bash_re
    # only the git-reset rule has a powershell side
    assert ps_re.count("\n") == 1


# ── Hook-level regression coverage (bash matcher) ──

def _run_hook(tmp_path, command, tool_name="Bash", session_id="hooktest",
              config=None, extra_env=None):
    import os
    import subprocess

    repo_root = Path(__file__).resolve().parents[1]
    hook = repo_root / "hooks" / "brain-pre-action-trigger.sh"

    env = dict(os.environ)
    env["SYMBIOSIS_BRAIN_VAULT"] = str(tmp_path)
    env.pop("SYMBIOSIS_BRAIN_TOOLS", None)
    env.pop("SYMBIOSIS_BRAIN_PRE_ACTION_DISABLED", None)
    env["TMPDIR"] = str(tmp_path)
    env["TEMP"] = str(tmp_path)
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True, exist_ok=True)
    if config is not None:
        (home / ".claude" / "symbiosis-brain-pre-action.json").write_text(
            json.dumps(config), encoding="utf-8"
        )
    env["HOME"] = str(home)
    env["USERPROFILE"] = str(home)
    if extra_env:
        env.update(extra_env)

    payload = json.dumps({
        "tool_name": tool_name,
        "tool_input": {"command": command},
        "session_id": session_id,
    })
    result = subprocess.run(
        [_bash(),str(hook)], input=payload, text=True, encoding="utf-8",
        env=env, capture_output=True, timeout=15,
    )
    return result


def test_hook_matches_multiline_command(tmp_path):
    """A risky command on line 2 of a multi-step script must still be caught
    — the JSON-escaped `\\n` needs decoding to a real newline before grep
    sees it, or every anchored rule only ever looks at line 1."""
    _write_local(tmp_path, [VALID_RULE])
    ar.compile_action_rules(tmp_path)
    result = _run_hook(tmp_path, "cd /repo\ngit reset --hard origin/main")
    assert result.returncode == 0, result.stderr
    out = json.loads(result.stdout.strip())
    assert "[action-rule git-reset-hard-after-fetch]" in out["hookSpecificOutput"]["additionalContext"]


def test_hook_matches_pretty_printed_payload(tmp_path):
    """`"command": "..."` (space after the colon) must match just like the
    compact `"command":"..."` shape — the extraction regex used to be
    stricter than the tool_name/session_id ones right above it."""
    import subprocess

    _write_local(tmp_path, [VALID_RULE])
    ar.compile_action_rules(tmp_path)

    repo_root = Path(__file__).resolve().parents[1]
    hook = repo_root / "hooks" / "brain-pre-action-trigger.sh"
    import os
    env = dict(os.environ)
    env["SYMBIOSIS_BRAIN_VAULT"] = str(tmp_path)
    env.pop("SYMBIOSIS_BRAIN_TOOLS", None)
    env.pop("SYMBIOSIS_BRAIN_PRE_ACTION_DISABLED", None)
    env["TMPDIR"] = str(tmp_path)
    env["TEMP"] = str(tmp_path)
    payload = json.dumps({
        "tool_name": "Bash",
        "tool_input": {"command": "git reset --hard origin/main"},
        "session_id": "pretty",
    }, indent=2)
    result = subprocess.run(
        [_bash(),str(hook)], input=payload, text=True, encoding="utf-8",
        env=env, capture_output=True, timeout=15,
    )
    assert result.returncode == 0, result.stderr
    out = json.loads(result.stdout.strip())
    assert "[action-rule git-reset-hard-after-fetch]" in out["hookSpecificOutput"]["additionalContext"]


def test_hook_honors_config_enabled_false(tmp_path):
    _write_local(tmp_path, [VALID_RULE])
    ar.compile_action_rules(tmp_path)
    result = _run_hook(tmp_path, "git reset --hard origin/main",
                        config={"enabled": False})
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == ""


def test_hook_honors_config_matchers_excluding_bash(tmp_path):
    _write_local(tmp_path, [VALID_RULE])
    ar.compile_action_rules(tmp_path)
    result = _run_hook(tmp_path, "git reset --hard origin/main",
                        config={"matchers": ["Task", "Edit"]})
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == ""


def test_hook_config_matchers_including_bash_still_fires(tmp_path):
    _write_local(tmp_path, [VALID_RULE])
    ar.compile_action_rules(tmp_path)
    result = _run_hook(tmp_path, "git reset --hard origin/main",
                        config={"matchers": ["Task", "Bash"]})
    assert result.returncode == 0, result.stderr
    out = json.loads(result.stdout.strip())
    assert "[action-rule git-reset-hard-after-fetch]" in out["hookSpecificOutput"]["additionalContext"]


def test_find_grep_derives_from_git_on_windows(tmp_path):
    # Only git on PATH (Git\cmd), grep lives under Git\usr\bin -> must be found.
    root = tmp_path / "Git"
    grep_exe = root / "usr" / "bin" / "grep.exe"
    grep_exe.parent.mkdir(parents=True)
    grep_exe.write_text("", encoding="utf-8")
    git_exe = root / "cmd" / "git.exe"
    git_exe.parent.mkdir(parents=True)
    git_exe.write_text("", encoding="utf-8")

    def fake_which(name):
        return str(git_exe) if name in ("git", "git.exe") else None

    found = ar._find_grep(which=fake_which, environ={}, windows=True)
    assert found is not None
    assert Path(found) == grep_exe


def test_find_grep_none_when_nothing_on_path(tmp_path):
    found = ar._find_grep(which=lambda name: None, environ={}, windows=True)
    assert found is None
    # non-Windows: PATH only, no root probing
    assert ar._find_grep(which=lambda name: None, environ={}, windows=False) is None


def test_compile_keeps_previous_artifacts_when_grep_unavailable(tmp_path, monkeypatch):
    _write_local(tmp_path, [VALID_RULE])
    ar.compile_action_rules(tmp_path)
    tsv = tmp_path / ".index" / "action-rules.tsv"
    re_bash = tmp_path / ".index" / "action-rules.bash.re"
    before_tsv = tsv.read_text(encoding="utf-8")
    before_re = re_bash.read_text(encoding="utf-8")
    assert before_tsv.strip() and before_re.strip()

    monkeypatch.setattr(ar, "_find_grep", lambda *a, **k: None)
    ar.compile_action_rules(tmp_path)

    # previous compiled artifacts survive an unvalidatable run
    assert tsv.read_text(encoding="utf-8") == before_tsv
    assert re_bash.read_text(encoding="utf-8") == before_re
    meta = json.loads((tmp_path / ".index" / "action-rules.meta.json").read_text(encoding="utf-8"))
    assert meta["validation"] == "unavailable"
    assert meta["kept_previous"] is True
    assert meta["rules_total"] == 1
