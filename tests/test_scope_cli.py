"""CLI tests for `brain-cli` subcommands (scope-resolve, parse-marker, locks).

These tests spawn `python -m symbiosis_brain.scope_cli` as a subprocess to
exercise the real CLI surface used by skills via the Bash tool.

We use `sys.executable` (the venv's Python) rather than `uv run python` so
that each subprocess invocation does not trigger an implicit `uv sync` —
syncs are slow and on Windows can fail if `symbiosis-brain.exe` is locked
by a running MCP server. The module entry `python -m symbiosis_brain.scope_cli`
exercises the same code path as the `brain-cli` console script (both call
`scope_cli.main`).
"""
import json
import subprocess
import sys


CLI = [sys.executable, "-m", "symbiosis_brain.scope_cli"]


def run_cli(*args, cwd=None):
    return subprocess.run([*CLI, *args], capture_output=True, text=True, cwd=cwd)


# ---------------------------------------------------------------------------
# scope-resolve
# ---------------------------------------------------------------------------


def test_scope_resolve_no_claude_md(tmp_path):
    # Folder without CLAUDE.md → source=hook, scope from basename
    proj = tmp_path / "MyCoolProject"
    proj.mkdir()
    res = run_cli("scope-resolve", str(proj))
    assert res.returncode == 0, res.stderr
    data = json.loads(res.stdout)
    assert data["scope"] == "my-cool-project"
    assert data["source"] == "hook"
    assert data["umbrella"] is None
    assert data["marker_version"] is None


def test_scope_resolve_with_v1_marker(tmp_path):
    proj = tmp_path / "alphanets"
    proj.mkdir()
    (proj / "CLAUDE.md").write_text(
        "# Alpha-Сети\n\n<!-- symbiosis-brain v1: scope=alpha-seti, umbrella=alpha -->\n",
        encoding="utf-8",
    )
    res = run_cli("scope-resolve", str(proj))
    data = json.loads(res.stdout)
    assert data["scope"] == "alpha-seti"
    assert data["umbrella"] == "alpha"
    assert data["source"] == "marker_v1"
    assert data["marker_version"] == 1


def test_scope_resolve_with_v2_marker(tmp_path):
    proj = tmp_path / "future"
    proj.mkdir()
    (proj / "CLAUDE.md").write_text(
        "<!-- symbiosis-brain v2: scope=future, team=alpha -->\n",
        encoding="utf-8",
    )
    res = run_cli("scope-resolve", str(proj))
    data = json.loads(res.stdout)
    assert data["source"] == "marker_future"
    assert data["scope"] == "future"
    assert data["marker_version"] == 2


def test_scope_resolve_with_draft_status(tmp_path):
    proj = tmp_path / "draft"
    proj.mkdir()
    (proj / "CLAUDE.md").write_text(
        "<!-- symbiosis-brain v1: scope=draft, status=draft -->\n",
        encoding="utf-8",
    )
    res = run_cli("scope-resolve", str(proj))
    data = json.loads(res.stdout)
    assert data["scope"] == "draft"
    assert data["marker_status"] == "draft"
    assert data["marker_version"] == 1


# ---------------------------------------------------------------------------
# locks
# ---------------------------------------------------------------------------


def test_lock_acquire_release_via_cli(tmp_path, monkeypatch):
    monkeypatch.setenv("SYMBIOSIS_BRAIN_LOCK_DIR", str(tmp_path))
    res = run_cli("acquire-onboard-lock", "foo")
    assert res.returncode == 0
    res2 = run_cli("acquire-onboard-lock", "foo")
    assert res2.returncode == 1  # busy
    rel = run_cli("release-onboard-lock", "foo")
    assert rel.returncode == 0
    res3 = run_cli("acquire-onboard-lock", "foo")
    assert res3.returncode == 0  # acquired again


# ---------------------------------------------------------------------------
# parse-marker
# ---------------------------------------------------------------------------


def test_parse_marker_via_cli(tmp_path):
    f = tmp_path / "CLAUDE.md"
    f.write_text("<!-- symbiosis-brain v1: scope=foo -->\n", encoding="utf-8")
    res = run_cli("parse-marker", str(f))
    assert res.returncode == 0
    data = json.loads(res.stdout)
    assert data["scope"] == "foo"


def test_parse_marker_missing_file(tmp_path):
    res = run_cli("parse-marker", str(tmp_path / "nope.md"))
    assert res.returncode == 1


# ---------------------------------------------------------------------------
# acquire-onboard-lock extensions: --timeout-s + OSError handling
# ---------------------------------------------------------------------------


def test_acquire_lock_with_custom_timeout(tmp_path, monkeypatch):
    """Custom --timeout-s passed through to acquire_lock."""
    monkeypatch.setenv("SYMBIOSIS_BRAIN_LOCK_DIR", str(tmp_path))
    res = run_cli("acquire-onboard-lock", "foo", "--timeout-s", "120")
    assert res.returncode == 0


def test_acquire_lock_default_timeout_when_unspecified(tmp_path, monkeypatch):
    """Without --timeout-s flag still works (default 30)."""
    monkeypatch.setenv("SYMBIOSIS_BRAIN_LOCK_DIR", str(tmp_path))
    res = run_cli("acquire-onboard-lock", "foo")
    assert res.returncode == 0


def test_acquire_lock_lockdir_unwritable(tmp_path, monkeypatch):
    """OSError → exit 2 + error:lockdir-unwritable in stderr."""
    # Point LOCK_DIR at a path that doesn't exist and can't be created (file, not dir).
    bad = tmp_path / "not-a-directory"
    bad.write_text("file blocking dir creation\n")
    bad_subpath = bad / "subdir-cant-make"
    monkeypatch.setenv("SYMBIOSIS_BRAIN_LOCK_DIR", str(bad_subpath))
    res = run_cli("acquire-onboard-lock", "foo")
    assert res.returncode == 2, f"got {res.returncode}: {res.stderr}"
    assert "error:lockdir-unwritable:foo" in res.stderr
