from pathlib import Path

import pytest

from symbiosis_brain.taxonomy import load_valid_scopes, load_folder_type_map


@pytest.fixture
def fake_taxonomy(tmp_path: Path) -> Path:
    (tmp_path / "reference").mkdir()
    (tmp_path / "reference" / "scope-taxonomy.md").write_text(
        "---\ntitle: Taxonomy\n---\n\n"
        "## Whitelist\n\n"
        "| scope | purpose |\n"
        "|-------|---------|\n"
        "| `global` | shared |\n"
        "| `symbiosis-brain` | SB |\n"
        "| `alpha-seti` | water |\n\n"
        "## Folder ↔ type convention\n\n"
        "| folder | type |\n"
        "|--------|------|\n"
        "| `decisions/` | `decision` |\n"
        "| `patterns/` | `pattern` |\n"
        "| `feedback/` | `feedback` |\n"
        "| `wiki/` | `wiki` |\n",
        encoding="utf-8",
    )
    return tmp_path


def test_load_valid_scopes_returns_frozenset_of_strings(fake_taxonomy: Path):
    scopes = load_valid_scopes(fake_taxonomy)
    assert scopes == frozenset({"global", "symbiosis-brain", "alpha-seti"})


def test_load_folder_type_map_strips_trailing_slash(fake_taxonomy: Path):
    mapping = load_folder_type_map(fake_taxonomy)
    assert mapping == {
        "decisions": "decision",
        "patterns": "pattern",
        "feedback": "feedback",
        "wiki": "wiki",
    }


def test_load_valid_scopes_raises_on_missing_file(tmp_path: Path):
    with pytest.raises(FileNotFoundError):
        load_valid_scopes(tmp_path)


def test_load_folder_type_map_raises_on_missing_section(tmp_path: Path):
    (tmp_path / "reference").mkdir()
    (tmp_path / "reference" / "scope-taxonomy.md").write_text(
        "## Whitelist\n\n| scope |\n|---|\n| `global` |\n", encoding="utf-8"
    )
    with pytest.raises(ValueError, match="Folder .* type convention"):
        load_folder_type_map(tmp_path)
