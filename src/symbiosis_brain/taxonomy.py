"""Parse reference/scope-taxonomy.md as single source of truth for scopes + folder-type map."""
from __future__ import annotations

import re
from pathlib import Path

_TAXONOMY_REL = Path("reference") / "scope-taxonomy.md"


def _read_taxonomy(vault_path: Path) -> str:
    file_path = vault_path / _TAXONOMY_REL
    if not file_path.exists():
        raise FileNotFoundError(f"Taxonomy missing: {file_path}")
    return file_path.read_text(encoding="utf-8")


def _extract_section(text: str, header_pattern: str, section_name: str) -> str:
    # Match "## <header>" up to next "## " or EOF.
    pattern = re.compile(
        rf"^##\s+{header_pattern}\s*$(.*?)(?=^##\s|\Z)",
        re.MULTILINE | re.DOTALL,
    )
    m = pattern.search(text)
    if not m:
        raise ValueError(f"{section_name} section not found in taxonomy file")
    return m.group(1)


def _iter_backtick_table_rows(section: str) -> list[list[str]]:
    """Return backtick-quoted cell values from table rows. Header/separator rows (no backticks) auto-skipped."""
    rows: list[list[str]] = []
    for line in section.splitlines():
        stripped = line.strip()
        if not stripped.startswith("|"):
            continue
        if "---" in stripped:
            continue
        cells = re.findall(r"`([^`]+)`", stripped)
        if cells:
            rows.append(cells)
    return rows


def load_valid_scopes(vault_path: Path) -> frozenset[str]:
    """Parse scope whitelist from `## Whitelist` table; values are first backtick-quoted cell per row."""
    text = _read_taxonomy(vault_path)
    section = _extract_section(text, r"Whitelist", "Whitelist")
    scopes: set[str] = set()
    for cells in _iter_backtick_table_rows(section):
        scopes.add(cells[0])
    if not scopes:
        raise ValueError("Whitelist section contained no scopes")
    return frozenset(scopes)


def load_folder_type_map(vault_path: Path) -> dict[str, str]:
    """Parse folder↔type table. Keys are folder names (no trailing slash), values are type strings."""
    text = _read_taxonomy(vault_path)
    section = _extract_section(text, r"Folder\s*↔\s*type\s+convention", "Folder ↔ type convention")
    mapping: dict[str, str] = {}
    for cells in _iter_backtick_table_rows(section):
        if len(cells) >= 2:
            mapping[cells[0].rstrip("/")] = cells[1]
    if not mapping:
        raise ValueError("Folder ↔ type convention section contained no rows")
    return mapping
