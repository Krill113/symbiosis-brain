"""CLI subcommand `python -m symbiosis_brain search-gist` for hook usage."""
import json
import subprocess
import sys
from pathlib import Path

import pytest


def test_cli_search_gist_returns_json(tmp_vault_with_taxonomy: Path):
    """Smoke test: invoke `python -m symbiosis_brain search-gist` and parse JSON output."""
    # Pre-populate vault with one note
    note_path = tmp_vault_with_taxonomy / "patterns" / "x.md"
    note_path.parent.mkdir(parents=True, exist_ok=True)
    note_path.write_text(
        "---\ntitle: X\ntype: pattern\nscope: global\ngist: A useful gist\ntags: []\n---\n\n## Body\n\nBody.\n",
        encoding="utf-8",
    )

    result = subprocess.run(
        [sys.executable, "-m", "symbiosis_brain", "search-gist",
         "--vault", str(tmp_vault_with_taxonomy),
         "--query", "X",
         "--limit", "5"],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, f"stderr: {result.stderr}"
    data = json.loads(result.stdout)
    assert isinstance(data, list)
    assert len(data) >= 1
    assert data[0]["gist"] == "A useful gist"
    assert data[0]["path"] == "patterns/x.md"
    assert data[0]["title"] == "X"
    assert data[0]["scope"] == "global"


def test_cli_search_gist_empty_vault_returns_empty_list(tmp_vault_with_taxonomy: Path):
    result = subprocess.run(
        [sys.executable, "-m", "symbiosis_brain", "search-gist",
         "--vault", str(tmp_vault_with_taxonomy),
         "--query", "nothing", "--limit", "5"],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0
    data = json.loads(result.stdout)
    assert data == []


def test_cli_search_gist_handles_cyrillic_and_arrow(tmp_vault_with_taxonomy: Path):
    """Regression: gist with cyrillic + `→` arrow must not crash with cp1251 UnicodeEncodeError on Windows.

    Why: on Windows default stdout codec is cp1251 unless reconfigured.
    `print(json.dumps(... ensure_ascii=False))` then crashes on `→` (U+2192).
    Hook callers run `python -m symbiosis_brain search-gist` and silently
    discard stderr, so this manifests as empty A1 recall in production.
    """
    note_path = tmp_vault_with_taxonomy / "mistakes" / "cp1251.md"
    note_path.parent.mkdir(parents=True, exist_ok=True)
    note_path.write_text(
        "---\ntitle: Кодировка\ntype: mistake\nscope: global\ngist: cp1251 → utf-8 — кириллица и стрелка ломают stdout\ntags: []\n---\n\n## Body\n\n.\n",
        encoding="utf-8",
    )

    # Force child to default Windows stdout (no PYTHONIOENCODING / PYTHONUTF8).
    import os
    env = {k: v for k, v in os.environ.items()
           if k not in ("PYTHONIOENCODING", "PYTHONUTF8")}
    result = subprocess.run(
        [sys.executable, "-m", "symbiosis_brain", "search-gist",
         "--vault", str(tmp_vault_with_taxonomy),
         "--query", "кодировка", "--limit", "5"],
        capture_output=True, timeout=30, env=env,
    )
    assert result.returncode == 0, (
        f"stderr: {result.stderr.decode('utf-8', errors='replace')}"
    )
    data = json.loads(result.stdout.decode("utf-8"))
    assert any("→" in n.get("gist", "") for n in data)
