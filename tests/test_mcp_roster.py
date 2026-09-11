"""The MCP roster cache: one `claude mcp list` per cwd per day, not per session.

`claude mcp list` health-checks every server, which starts every stdio server
— including a second `symbiosis-brain serve` against the live vault — and on
04.09 a crashed terminal produced five such polls in 2.5 minutes. The cache
keys a ready answer on an EXPLICIT snapshot of the context the answer depends
on: cwd, the config directory, the claude version, and the content of every
configuration layer we can see, field by field, so a miss names the field
that moved. The TTL (a day, owner's decision) only insures what we cannot see.

The per-session file `brain-mcp-roster-<sid>` and its reader `_roster_set`
are untouched: on a hit the shared answer is copied under the session's name.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from symbiosis_brain import mcp_roster
from symbiosis_brain.mcp_roster import (
    EXIT_HIT, EXIT_MISS, EXIT_REJECTED, FIELDS, TTL_S,
    check, compare, publish, session_path, shared_paths, snapshot,
)

ROSTER = ("Checking MCP server health…\n\n"
          "serena: uv run serena - ✓ Connected\n"
          "vs-mcp: http://localhost:1 - ✘ Failed to connect\n")
BANNER_ONLY = "Checking MCP server health…\n\n"


def _home_with_claude_json(tmp_path: Path, cwd: Path, project: dict | None = None,
                           top: dict | None = None) -> Path:
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True, exist_ok=True)
    key = cwd.as_posix()
    cfg = {"mcpServers": top if top is not None else {"serena": {"command": "uv"}},
           "lastCost": 1.5,
           "projects": {key: project if project is not None else
                        {"mcpServers": {}, "disabledMcpServers": ["vs-mcp"],
                         "enabledMcpjsonServers": [], "lastCost": 0.1}}}
    (home / ".claude.json").write_text(json.dumps(cfg), encoding="utf-8")
    return home


def _env(home: Path, tmp: Path, **extra) -> dict:
    # Both home variables: `claude` is node, and libuv's os.homedir() reads
    # USERPROFILE on Windows and HOME elsewhere — the snapshot mirrors that.
    env = {"HOME": str(home), "USERPROFILE": str(home), "TMPDIR": str(tmp),
           "PATH": os.environ.get("PATH", "")}
    env.update(extra)
    return env


def _snap(cwd: Path, env: dict) -> dict:
    return snapshot(str(cwd), env, version_probe=lambda e: "2.1.260")


# --- T5: the key, field by field --------------------------------------------------

def test_snapshot_carries_every_field_in_a_stable_order(tmp_path):
    cwd = tmp_path / "proj"; cwd.mkdir()
    home = _home_with_claude_json(tmp_path, cwd)
    snap = _snap(cwd, _env(home, tmp_path))
    assert list(snap) == list(FIELDS)
    assert snap["claude_version"] == "2.1.260"
    assert snap["config_dir"].endswith(".claude")


def test_compare_identical_is_none_and_first_difference_is_named(tmp_path):
    cwd = tmp_path / "proj"; cwd.mkdir()
    home = _home_with_claude_json(tmp_path, cwd)
    a = _snap(cwd, _env(home, tmp_path))
    assert compare(a, dict(a)) is None
    b = dict(a); b["plugins"] = "x"; b["cwd"] = "y"
    assert compare(a, b) == "cwd", "the first field in FIELDS order, not the first edited"
    c = dict(a); del c["mcp_json"]
    assert compare(a, c) == "mcp_json", "a field missing on one side is a difference"


def test_snapshot_reads_claude_json_under_the_environments_home(tmp_path):
    """The snapshot takes home from the environment it is handed, the way
    `claude` finds it (libuv os.homedir: USERPROFILE on Windows, HOME
    elsewhere) — not Path.home() of the test process. Two homes, two config
    files: each snapshot carries its own file's entry. compare() names
    config_dir first, because the default config dir lives under home — so the
    entry itself is checked, not just the first difference."""
    cwd = tmp_path / "proj"; cwd.mkdir()
    home_a = _home_with_claude_json(tmp_path / "a", cwd, project={"disabledMcpServers": ["x"]})
    home_b = _home_with_claude_json(tmp_path / "b", cwd, project={"disabledMcpServers": ["y"]})
    sa = _snap(cwd, _env(home_a, tmp_path))
    sb = _snap(cwd, _env(home_b, tmp_path))
    field = "claude_json.project.disabledMcpServers"
    assert sa[field] != sb[field]
    assert "absent" not in (sa[field], sb[field])
    assert compare(sa, sb) == "config_dir"


def test_project_entry_matches_forward_slashes_and_any_case(tmp_path):
    """`~/.claude.json` stores project keys with forward slashes in the case
    Claude Code saw; the hook passes $PWD as bash spells it."""
    cwd = tmp_path / "Proj"; cwd.mkdir()
    home = _home_with_claude_json(tmp_path, cwd, project={"disabledMcpServers": ["z"]})
    spelled = str(cwd).replace("/", "\\") if os.name == "nt" else str(cwd)
    snap = snapshot(spelled.lower() if os.name == "nt" else spelled, _env(home, tmp_path),
                    version_probe=lambda e: "v")
    empty = _snap(tmp_path / "nowhere", _env(home, tmp_path))
    assert snap["claude_json.project.disabledMcpServers"] != empty["claude_json.project.disabledMcpServers"]


def test_only_the_changed_field_moves_and_unrelated_keys_move_nothing(tmp_path):
    """T4's core: editing projects[cwd].disabledMcpServers moves exactly that
    field; editing lastCost (or anything outside the mcp subtrees) moves none —
    the key is content, not the file's mtime or size."""
    cwd = tmp_path / "proj"; cwd.mkdir()
    home = _home_with_claude_json(tmp_path, cwd)
    before = _snap(cwd, _env(home, tmp_path))
    cfg_path = home / ".claude.json"
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    cfg["lastCost"] = 99.0
    cfg["projects"][cwd.as_posix()]["lastCost"] = 42.0
    cfg_path.write_text(json.dumps(cfg), encoding="utf-8")
    assert compare(before, _snap(cwd, _env(home, tmp_path))) is None
    cfg["projects"][cwd.as_posix()]["disabledMcpServers"] = []
    cfg_path.write_text(json.dumps(cfg), encoding="utf-8")
    after = _snap(cwd, _env(home, tmp_path))
    assert compare(before, after) == "claude_json.project.disabledMcpServers"
    assert [f for f in FIELDS if before[f] != after[f]] == ["claude_json.project.disabledMcpServers"]


@pytest.mark.parametrize("touch, field", [
    (lambda cwd, home: (cwd / ".mcp.json").write_text('{"mcpServers": {"x": {}}}', encoding="utf-8"), "mcp_json"),
    (lambda cwd, home: (home / ".claude" / "settings.json").write_text('{"enableAllProjectMcpServers": true}', encoding="utf-8"), "settings.user.mcp"),
    (lambda cwd, home: ((cwd / ".claude").mkdir(exist_ok=True), (cwd / ".claude" / "settings.local.json").write_text('{"disabledMcpjsonServers": ["a"]}', encoding="utf-8")), "settings.local.mcp"),
    (lambda cwd, home: ((home / ".claude" / "plugins").mkdir(exist_ok=True), (home / ".claude" / "plugins" / "installed_plugins.json").write_text('{"p": 1}', encoding="utf-8")), "plugins"),
    (lambda cwd, home: (home / ".claude" / "settings.json").write_text('{"theme": "dark"}', encoding="utf-8"), None),
], ids=["mcp.json", "user-settings-mcp-key", "local-settings-mcp-key", "plugins", "settings-without-mcp-keys"])
def test_every_observed_layer_is_in_the_key(tmp_path, touch, field):
    cwd = tmp_path / "proj"; cwd.mkdir()
    home = _home_with_claude_json(tmp_path, cwd)
    before = _snap(cwd, _env(home, tmp_path))
    touch(cwd, home)
    assert compare(before, _snap(cwd, _env(home, tmp_path))) == field


def test_config_dir_comes_from_CLAUDE_CONFIG_DIR(tmp_path):
    cwd = tmp_path / "proj"; cwd.mkdir()
    home = _home_with_claude_json(tmp_path, cwd)
    a = _snap(cwd, _env(home, tmp_path))
    b = _snap(cwd, _env(home, tmp_path, CLAUDE_CONFIG_DIR=str(tmp_path / "other")))
    assert compare(a, b) == "config_dir"


# --- check: hit, miss by field, miss by TTL ------------------------------------------

def _published(tmp_path, cwd, home, sid="s1", roster=ROSTER):
    env = _env(home, tmp_path)
    code = publish(sid, str(cwd), roster, env, tmp=tmp_path, version_probe=lambda e: "v")
    assert code == EXIT_HIT
    return env


def test_check_misses_when_nothing_is_published(tmp_path, monkeypatch):
    cwd = tmp_path / "proj"; cwd.mkdir()
    home = _home_with_claude_json(tmp_path, cwd)
    monkeypatch.setenv("SYMBIOSIS_BRAIN_DEBUG_LOG", str(tmp_path / "debug.log"))
    code, reason = check("s2", str(cwd), _env(home, tmp_path), tmp=tmp_path, version_probe=lambda e: "v")
    assert (code, reason) == (EXIT_MISS, "absent")
    assert not session_path(tmp_path, "s2").exists()
    assert "roster: miss absent" in (tmp_path / "debug.log").read_text(encoding="utf-8")


def test_check_hits_and_adopts_the_shared_answer_for_the_session(tmp_path, monkeypatch):
    cwd = tmp_path / "proj"; cwd.mkdir()
    home = _home_with_claude_json(tmp_path, cwd)
    monkeypatch.setenv("SYMBIOSIS_BRAIN_DEBUG_LOG", str(tmp_path / "debug.log"))
    env = _published(tmp_path, cwd, home, sid="s1")

    code, reason = check("s2", str(cwd), env, tmp=tmp_path, version_probe=lambda e: "v")

    assert (code, reason) == (EXIT_HIT, "hit")
    assert session_path(tmp_path, "s2").read_text(encoding="utf-8") == ROSTER
    assert "roster: hit" in (tmp_path / "debug.log").read_text(encoding="utf-8")


def test_check_misses_on_a_moved_field_and_names_it(tmp_path, monkeypatch):
    """T2: the key on disk says another cwd — the miss is logged WITH the
    field name, which is the whole point of storing the snapshot field by
    field instead of one hash."""
    cwd = tmp_path / "proj"; cwd.mkdir()
    home = _home_with_claude_json(tmp_path, cwd)
    monkeypatch.setenv("SYMBIOSIS_BRAIN_DEBUG_LOG", str(tmp_path / "debug.log"))
    env = _published(tmp_path, cwd, home)
    _, key_path = shared_paths(tmp_path, str(cwd))
    key = json.loads(key_path.read_text(encoding="utf-8"))
    key["cwd"] = "/somewhere/else"
    key_path.write_text(json.dumps(key), encoding="utf-8")

    code, reason = check("s2", str(cwd), env, tmp=tmp_path, version_probe=lambda e: "v")

    assert (code, reason) == (EXIT_MISS, "cwd")
    assert not session_path(tmp_path, "s2").exists()
    assert "roster: miss cwd" in (tmp_path / "debug.log").read_text(encoding="utf-8")


def test_check_misses_when_the_shared_answer_is_older_than_a_day(tmp_path, monkeypatch):
    cwd = tmp_path / "proj"; cwd.mkdir()
    home = _home_with_claude_json(tmp_path, cwd)
    monkeypatch.setenv("SYMBIOSIS_BRAIN_DEBUG_LOG", str(tmp_path / "debug.log"))
    env = _published(tmp_path, cwd, home)
    shared, key_path = shared_paths(tmp_path, str(cwd))
    old = time.time() - TTL_S - 60
    os.utime(shared, (old, old))

    code, reason = check("s2", str(cwd), env, tmp=tmp_path, version_probe=lambda e: "v")

    assert (code, reason) == (EXIT_MISS, "ttl")
    assert TTL_S == 24 * 3600, "a day — owner's decision 2026-09-04"


# --- publish: three files atomically, or nothing ---------------------------------------

def test_publish_writes_shared_key_and_per_session(tmp_path):
    cwd = tmp_path / "proj"; cwd.mkdir()
    home = _home_with_claude_json(tmp_path, cwd)
    env = _published(tmp_path, cwd, home, sid="s1")
    shared, key_path = shared_paths(tmp_path, str(cwd))
    assert shared.read_text(encoding="utf-8") == ROSTER
    assert session_path(tmp_path, "s1").read_text(encoding="utf-8") == ROSTER
    assert json.loads(key_path.read_text(encoding="utf-8")) == snapshot(
        str(cwd), env, version_probe=lambda e: "v")
    assert not list(tmp_path.glob("*.tmp*")), "no temp files left behind"


def test_publish_rejects_a_roster_without_a_single_server_line(tmp_path, monkeypatch):
    """T6: an exit-0 run that printed only the banner must not become the
    shared truth for every new session in this cwd — and not the session's
    own file either, which `_roster_set` would read as 'no roster' anyway."""
    cwd = tmp_path / "proj"; cwd.mkdir()
    home = _home_with_claude_json(tmp_path, cwd)
    monkeypatch.setenv("SYMBIOSIS_BRAIN_DEBUG_LOG", str(tmp_path / "debug.log"))

    code = publish("s1", str(cwd), BANNER_ONLY, _env(home, tmp_path), tmp=tmp_path,
                   version_probe=lambda e: "v")

    assert code == EXIT_REJECTED
    shared, key_path = shared_paths(tmp_path, str(cwd))
    assert not shared.exists() and not key_path.exists()
    assert not session_path(tmp_path, "s1").exists()
    assert "roster: rejected" in (tmp_path / "debug.log").read_text(encoding="utf-8")


def test_shared_paths_do_not_match_the_per_session_gc_pattern(tmp_path):
    """The hook sweeps `brain-mcp-roster-*` at 60 minutes; the shared answer
    lives a day, so it must not answer to that glob."""
    shared, key_path = shared_paths(tmp_path, str(tmp_path / "proj"))
    for p in (shared, key_path):
        assert p.parent == tmp_path
        assert not p.name.startswith("brain-mcp-roster-")
        assert p.name.startswith("brain-mcp-shared-")


# --- the CLI the hook calls ----------------------------------------------------------

def test_cli_check_and_publish_exit_codes(tmp_path, monkeypatch):
    cwd = tmp_path / "proj"; cwd.mkdir()
    home = _home_with_claude_json(tmp_path, cwd)
    for k, v in _env(home, tmp_path).items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(mcp_roster, "_claude_version", lambda env: "v")
    roster_file = tmp_path / "out.txt"
    roster_file.write_text(ROSTER, encoding="utf-8")

    assert mcp_roster.run(["check", "--session-id", "s1", "--cwd", str(cwd)]) == EXIT_MISS
    assert mcp_roster.run(["publish", "--session-id", "s1", "--cwd", str(cwd),
                           "--from", str(roster_file)]) == EXIT_HIT
    assert mcp_roster.run(["check", "--session-id", "s2", "--cwd", str(cwd)]) == EXIT_HIT
    assert session_path(tmp_path, "s2").read_text(encoding="utf-8") == ROSTER
    roster_file.write_text(BANNER_ONLY, encoding="utf-8")
    assert mcp_roster.run(["publish", "--session-id", "s3", "--cwd", str(cwd),
                           "--from", str(roster_file)]) == EXIT_REJECTED


@pytest.mark.parametrize("text", [ROSTER, BANNER_ONLY, ""], ids=["servers", "banner", "empty"])
def test_publish_rejects_exactly_what_the_gate_reader_would_ignore(tmp_path, monkeypatch, text):
    """`_roster_set` (tool_routing.py) is the reader and is not ours to change.
    publish's "has a server line" test duplicates its rule; this pins the two
    together, so the copy cannot drift from the original in silence."""
    from symbiosis_brain.tool_routing import _roster_set
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    session_path(tmp_path, "probe").write_text(text, encoding="utf-8")
    readable = _roster_set("probe") is not None
    cwd = tmp_path / "proj"; cwd.mkdir()
    home = _home_with_claude_json(tmp_path, cwd)

    code = publish("s1", str(cwd), text, _env(home, tmp_path), tmp=tmp_path,
                   version_probe=lambda e: "v")

    assert (code == EXIT_HIT) == readable


def test_hook_sweeps_the_shared_answer_at_the_ttl_not_at_60_minutes():
    """The hook's GC restates the TTL in minutes: a restated number is allowed,
    its drift is not."""
    from symbiosis_brain.mcp_roster import SHARED_PREFIX
    hook = (Path(__file__).resolve().parents[1] / "hooks" / "brain-session-start.sh"
            ).read_text(encoding="utf-8")
    assert f"-name '{SHARED_PREFIX}*' -mmin +{TTL_S // 60} -delete" in hook


def test_python_m_symbiosis_brain_dispatches_mcp_roster(tmp_path):
    """The hook's entry point is `python -m symbiosis_brain mcp-roster ...`.
    Without the dispatcher line the argv falls through to the MCP server, so
    the vault here points at a path that does not exist (serve refuses to
    create one) and stdin is closed: a missing dispatcher fails fast, it never
    starts a server against the live vault."""
    import subprocess
    import sys
    cwd = tmp_path / "proj"; cwd.mkdir()
    home = _home_with_claude_json(tmp_path, cwd)
    env = dict(os.environ)
    env.update(_env(home, tmp_path))
    env["SYMBIOSIS_BRAIN_VAULT"] = str(tmp_path / "no-vault")
    env["SYMBIOSIS_BRAIN_DEBUG_LOG"] = str(tmp_path / "debug.log")

    r = subprocess.run(
        [sys.executable, "-m", "symbiosis_brain", "mcp-roster", "check",
         "--session-id", "s1", "--cwd", str(cwd)],
        stdin=subprocess.DEVNULL, capture_output=True, encoding="utf-8",
        errors="replace", env=env, timeout=60)

    assert r.returncode == EXIT_MISS, r.stderr[-2000:]
    assert "mcp_roster: miss absent" in (tmp_path / "debug.log").read_text(encoding="utf-8")
