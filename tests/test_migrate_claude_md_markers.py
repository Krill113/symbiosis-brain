import json
from pathlib import Path
import pytest

from symbiosis_brain.scope_resolver import parse_marker

# Import the script as a module via importlib (script lives in scripts/, not src/).
import importlib.util
import sys

SCRIPT_PATH = Path(__file__).parent.parent / "scripts" / "migrate_claude_md_markers.py"
spec = importlib.util.spec_from_file_location("migrate_claude_md_markers", SCRIPT_PATH)
mig = importlib.util.module_from_spec(spec)
sys.modules["migrate_claude_md_markers"] = mig
spec.loader.exec_module(mig)


def write_card(vault: Path, scope: str, umbrella: str | None = None) -> None:
    body = f"---\nscope: {scope}\n"
    if umbrella:
        body += f"umbrella: {umbrella}\n"
    body += "type: project\ntitle: X\n---\n\nbody\n"
    (vault / "projects" / f"{scope}.md").write_text(body, encoding="utf-8")


def test_writes_marker_when_missing(tmp_path):
    vault = tmp_path / "vault"
    (vault / "projects").mkdir(parents=True)
    write_card(vault, "beta")
    proj = tmp_path / "beta"
    proj.mkdir()
    (proj / "CLAUDE.md").write_text("# Beta\n\nSome rules.\n", encoding="utf-8")

    result = mig.migrate(vault, {"beta": str(proj)})
    assert result["written"] == ["beta"]
    assert result["skipped_existing"] == []
    m = parse_marker(proj / "CLAUDE.md")
    assert m.scope == "beta"


def test_skips_when_marker_already_present(tmp_path):
    vault = tmp_path / "vault"
    (vault / "projects").mkdir(parents=True)
    write_card(vault, "beta")
    proj = tmp_path / "beta"
    proj.mkdir()
    (proj / "CLAUDE.md").write_text(
        "# Beta\n<!-- symbiosis-brain v1: scope=beta -->\n", encoding="utf-8"
    )

    result = mig.migrate(vault, {"beta": str(proj)})
    assert result["written"] == []
    assert "beta" in result["skipped_existing"]


def test_creates_claude_md_when_absent(tmp_path):
    vault = tmp_path / "vault"
    (vault / "projects").mkdir(parents=True)
    write_card(vault, "beta")
    proj = tmp_path / "beta"
    proj.mkdir()  # no CLAUDE.md

    result = mig.migrate(vault, {"beta": str(proj)})
    assert result["written"] == ["beta"]
    assert (proj / "CLAUDE.md").exists()
    text = (proj / "CLAUDE.md").read_text(encoding="utf-8")
    assert text.startswith("# beta\n") or text.startswith("# Beta")
    assert "symbiosis-brain v1: scope=beta" in text


def test_skips_when_path_not_in_map(tmp_path):
    vault = tmp_path / "vault"
    (vault / "projects").mkdir(parents=True)
    write_card(vault, "lonely")

    result = mig.migrate(vault, {})
    assert result["written"] == []
    assert "lonely" in result["skipped_no_path"]


def test_includes_umbrella_when_present(tmp_path):
    vault = tmp_path / "vault"
    (vault / "projects").mkdir(parents=True)
    write_card(vault, "alpha-seti", umbrella="alpha")
    proj = tmp_path / "alphanets"
    proj.mkdir()
    (proj / "CLAUDE.md").write_text("# Alpha-Сети\n", encoding="utf-8")

    result = mig.migrate(vault, {"alpha-seti": str(proj)})
    m = parse_marker(proj / "CLAUDE.md")
    assert m.scope == "alpha-seti"
    assert m.umbrella == "alpha"


def test_idempotent_double_run(tmp_path):
    vault = tmp_path / "vault"
    (vault / "projects").mkdir(parents=True)
    write_card(vault, "beta")
    proj = tmp_path / "beta"
    proj.mkdir()
    (proj / "CLAUDE.md").write_text("# Beta\n", encoding="utf-8")

    mig.migrate(vault, {"beta": str(proj)})
    result2 = mig.migrate(vault, {"beta": str(proj)})
    assert result2["written"] == []
    text = (proj / "CLAUDE.md").read_text(encoding="utf-8")
    assert text.count("symbiosis-brain v1") == 1
