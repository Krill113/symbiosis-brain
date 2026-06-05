"""CLI subcommand `python -m symbiosis_brain search-gist` for hook usage."""
import json
import os
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


# --- Stage-4 backward-compat + routing envelope -----------------------------
# CRITICAL SAFETY PROPERTY: the deployed ~/.claude bash hook still parses the
# OLD bare list (it is not redeployed until Phase B). So `search-gist` MUST
# keep returning a bare list `[{path,title,scope,gist}]` BY DEFAULT (no new
# flag), byte-shape-identical to the legacy contract. The envelope
# `{memory_hits, route_hints}` is returned ONLY under --prompt-from-stdin (or
# an explicit --envelope). The three legacy tests above (which assert
# `isinstance(data, list)`) are part of this guard; the test below makes the
# property explicit and self-documenting.


def test_search_gist_no_flag_returns_bare_list(tmp_vault_with_taxonomy: Path):
    """Backward-compat: the OLD calling convention (--query, NO --prompt-from-stdin
    / --envelope) MUST return the bare list exactly as the deployed hook expects."""
    note_path = tmp_vault_with_taxonomy / "patterns" / "bc.md"
    note_path.parent.mkdir(parents=True, exist_ok=True)
    note_path.write_text(
        "---\ntitle: BC\ntype: pattern\nscope: global\ngist: legacy gist\ntags: []\n---\n\n## Body\n\nBody.\n",
        encoding="utf-8",
    )
    result = subprocess.run(
        [sys.executable, "-m", "symbiosis_brain", "search-gist",
         "--vault", str(tmp_vault_with_taxonomy),
         "--query", "BC", "--limit", "5"],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, f"stderr: {result.stderr}"
    data = json.loads(result.stdout)
    # Bare list, NOT an envelope dict.
    assert isinstance(data, list)
    assert "memory_hits" not in (data if isinstance(data, dict) else {})
    assert len(data) >= 1
    first = data[0]
    assert set(first.keys()) == {"path", "title", "scope", "gist"}
    assert first["gist"] == "legacy gist"
    assert first["path"] == "patterns/bc.md"


def test_search_gist_missing_vault_no_flag_returns_bare_empty_list(tmp_path: Path):
    """Legacy missing-vault behavior: bare `[]`, not an envelope."""
    result = subprocess.run(
        [sys.executable, "-m", "symbiosis_brain", "search-gist",
         "--vault", str(tmp_path / "does-not-exist"),
         "--query", "x"],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, f"stderr: {result.stderr}"
    assert json.loads(result.stdout) == []


def test_search_gist_envelope_under_prompt_from_stdin(tmp_vault_with_taxonomy: Path):
    """Envelope path: --prompt-from-stdin returns {memory_hits, route_hints}."""
    note_path = tmp_vault_with_taxonomy / "patterns" / "env.md"
    note_path.parent.mkdir(parents=True, exist_ok=True)
    note_path.write_text(
        "---\ntitle: Env\ntype: pattern\nscope: global\ngist: envelope gist\ntags: []\n---\n\n## Body\n\nBody.\n",
        encoding="utf-8",
    )
    payload = json.dumps({"prompt": "Env"})
    result = subprocess.run(
        [sys.executable, "-m", "symbiosis_brain", "search-gist",
         "--vault", str(tmp_vault_with_taxonomy),
         "--prompt-from-stdin", "--limit", "5"],
        input=payload, capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, f"stderr: {result.stderr}"
    out = json.loads(result.stdout)
    assert isinstance(out, dict)
    assert "memory_hits" in out and "route_hints" in out
    assert isinstance(out["memory_hits"], list)
    assert isinstance(out["route_hints"], list)
    assert any(h.get("gist") == "envelope gist" for h in out["memory_hits"])


def test_search_gist_envelope_under_explicit_envelope_flag(tmp_vault_with_taxonomy: Path):
    """Explicit --envelope (still using --query) also opts into the dict shape."""
    result = subprocess.run(
        [sys.executable, "-m", "symbiosis_brain", "search-gist",
         "--vault", str(tmp_vault_with_taxonomy),
         "--query", "anything", "--envelope"],
        capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, f"stderr: {result.stderr}"
    out = json.loads(result.stdout)
    assert isinstance(out, dict)
    assert "memory_hits" in out and "route_hints" in out


def test_search_gist_stdin_prompt_not_truncated_with_embedded_quote(tmp_path: Path):
    """The prompt is read untruncated from raw stdin JSON (NOT from a truncated
    --query), and an embedded double-quote survives json.loads. We assert the
    Windows route fires off a long prompt whose UNC-path trigger sits PAST the
    point a truncated --query would have cut, proving the full prompt was used."""
    long_prefix = "обсудим " + ("очень длинный контекст " * 30)
    # Embedded double-quote + a UNC path that triggers powershell-on-windows.
    prompt = f'{long_prefix} он сказал "запиши путь" в \\\\server\\share вот так'
    payload = json.dumps({"prompt": prompt})
    result = subprocess.run(
        [sys.executable, "-m", "symbiosis_brain", "search-gist",
         "--vault", str(tmp_path), "--prompt-from-stdin", "--skip-memory"],
        input=payload, capture_output=True, text=True, timeout=30,
        env={**os.environ, "OSTYPE": "msys"},
    )
    assert result.returncode == 0, f"stderr: {result.stderr}"
    out = json.loads(result.stdout)
    assert isinstance(out, dict)
    assert out["memory_hits"] == []  # --skip-memory honored
    assert any(h["id"] == "powershell-on-windows" for h in out["route_hints"]), out
