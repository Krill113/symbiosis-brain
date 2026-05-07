"""Test the `python -m symbiosis_brain prewarm` subcommand.

Pre-warm runs fastembed import + a single-query embedder pass + sqlite-vec import
to warm the OS page cache for the first real prompt-check invocation. It must
exit cleanly and quickly (<60s cold, <5s warm), produce no stdout, and never
raise on a missing/empty vault (graceful no-op)."""
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent


def _run_prewarm(vault_arg: str, env: dict | None = None) -> subprocess.CompletedProcess:
    e = os.environ.copy()
    if env:
        e.update(env)
    return subprocess.run(
        [sys.executable, "-m", "symbiosis_brain", "prewarm", "--vault", vault_arg],
        capture_output=True, text=True, encoding="utf-8", env=e, cwd=str(ROOT),
        timeout=120,
    )


def test_prewarm_exits_zero_on_real_vault(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "test.md").write_text(
        "---\nname: T\ntype: wiki\nscope: global\n---\nbody",
        encoding="utf-8",
    )
    proc = _run_prewarm(str(vault))
    assert proc.returncode == 0, f"stderr: {proc.stderr}"


def test_prewarm_silent_stdout(tmp_path):
    """Hook spawns prewarm as background; any stdout would risk leaking into
    SessionStart hook output, polluting the L0 context block."""
    vault = tmp_path / "vault"
    vault.mkdir()
    proc = _run_prewarm(str(vault))
    assert proc.stdout == "", f"prewarm leaked stdout: {proc.stdout!r}"


def test_prewarm_missing_vault_graceful(tmp_path):
    """User may have wrong vault path or first-time install. Must not raise."""
    proc = _run_prewarm(str(tmp_path / "nonexistent"))
    assert proc.returncode == 0
