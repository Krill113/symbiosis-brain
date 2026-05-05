import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

HOOK = Path(__file__).parent.parent / "hooks" / "brain-session-start.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("brain_session_start", HOOK)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_normalize_scope_camelcase():
    mod = _load_module()
    assert mod.normalize_scope("AlphaDiagnostics") == "alpha-diagnostics"
    assert mod.normalize_scope("ABCService") == "abc-service"
    assert mod.normalize_scope("LoadCatalog") == "load-catalog"


def test_normalize_scope_underscore_dot_space():
    mod = _load_module()
    assert mod.normalize_scope("my_project") == "my-project"
    assert mod.normalize_scope("foo.bar") == "foo-bar"
    assert mod.normalize_scope("hello world") == "hello-world"


def test_normalize_scope_strips_non_alnum_and_collapses_dashes():
    mod = _load_module()
    assert mod.normalize_scope("foo--bar") == "foo-bar"
    assert mod.normalize_scope("--foo--") == "foo"
    assert mod.normalize_scope("foo!@#bar") == "foobar"


def test_normalize_scope_empty():
    mod = _load_module()
    assert mod.normalize_scope("") == ""
    assert mod.normalize_scope(None) == ""


def test_main_writes_env_file_with_scope_and_vault(tmp_path, monkeypatch):
    env_file = tmp_path / "env"
    env_file.write_text("", encoding="utf-8")
    monkeypatch.setenv("CLAUDE_ENV_FILE", str(env_file))
    monkeypatch.setenv("SYMBIOSIS_BRAIN_VAULT", "/tmp/myvault")
    monkeypatch.chdir(tmp_path / "MyProject" if False else tmp_path)
    # Use a directory whose name normalizes deterministically
    proj = tmp_path / "AlphaDiagnostics"
    proj.mkdir()
    monkeypatch.chdir(proj)

    proc = subprocess.run(
        [sys.executable, str(HOOK)],
        input=json.dumps({"session_id": "abc-123"}),
        capture_output=True, text=True,
    )
    assert proc.returncode == 0, proc.stderr
    env_lines = env_file.read_text(encoding="utf-8").splitlines()
    assert any("SYMBIOSIS_BRAIN_SCOPE=\"alpha-diagnostics\"" in l for l in env_lines)
    assert any("SYMBIOSIS_BRAIN_VAULT=\"/tmp/myvault\"" in l for l in env_lines)
    assert any("CLAUDE_SESSION_ID=\"abc-123\"" in l for l in env_lines)


def test_main_falls_back_to_global_when_basename_empty(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_ENV_FILE", str(tmp_path / "env"))
    monkeypatch.setenv("SYMBIOSIS_BRAIN_VAULT", "/tmp/v")
    monkeypatch.delenv("PWD", raising=False)
    # Run from an empty-named edge case is hard; just verify scope fallback path:
    # Use a subdirectory with hyphen-only name → normalizes to empty → fallback "global"
    proj = tmp_path / "---"
    proj.mkdir()
    monkeypatch.chdir(proj)
    proc = subprocess.run(
        [sys.executable, str(HOOK)],
        input='{"session_id":"x"}',
        capture_output=True, text=True,
    )
    assert proc.returncode == 0
    out = proc.stdout
    assert "[scope: global]" in out


def test_main_emits_critical_facts_when_present(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "CRITICAL_FACTS.md").write_text("USER: K.D.\n", encoding="utf-8")
    monkeypatch.setenv("CLAUDE_ENV_FILE", str(tmp_path / "env"))
    monkeypatch.setenv("SYMBIOSIS_BRAIN_VAULT", str(vault))
    proj = tmp_path / "myproj"
    proj.mkdir()
    monkeypatch.chdir(proj)

    proc = subprocess.run([sys.executable, str(HOOK)],
                          input='{"session_id":"s1"}', capture_output=True, text=True)
    assert "=== Symbiosis Brain ===" in proc.stdout
    assert "USER: K.D." in proc.stdout
    assert "Available tools:" in proc.stdout


def test_main_cleans_session_flags(tmp_path, monkeypatch):
    """Ported flag-cleanup behaviour: brain-session-start removes per-session tmp files."""
    monkeypatch.setenv("CLAUDE_ENV_FILE", str(tmp_path / "env"))
    monkeypatch.setenv("SYMBIOSIS_BRAIN_VAULT", str(tmp_path))
    monkeypatch.setenv("TMPDIR", str(tmp_path))   # Force hook to use tmp_path for flag files
    monkeypatch.setenv("TEMP", str(tmp_path))     # Windows fallback
    monkeypatch.chdir(tmp_path)

    sid = "cleanup-test"
    tmp_dir = tmp_path  # Same as what the hook will see via _tmp_dir()
    flags = [
        tmp_dir / f"brain-triggered-{sid}",
        tmp_dir / f"brain-precompact-{sid}",
        tmp_dir / f"brain-precompact-pending-{sid}",
        tmp_dir / f"brain-last-save-pct-{sid}",
        tmp_dir / f"brain-save-later-{sid}",
        tmp_dir / f"brain-rules-shown-{sid}",
        tmp_dir / f"brain-rules-turn-counter-{sid}",
    ]
    for f in flags:
        f.write_text("stale", encoding="utf-8")

    subprocess.run([sys.executable, str(HOOK)],
                   input=json.dumps({"session_id": sid}),
                   capture_output=True, text=True)

    for f in flags:
        assert not f.exists(), f"{f} still exists, should have been cleaned"
