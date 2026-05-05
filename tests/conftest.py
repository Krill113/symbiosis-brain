import os
import sqlite3
import tempfile
from pathlib import Path

import pytest

from symbiosis_brain.sync import VAULT_DIRS


@pytest.fixture
def tmp_vault(tmp_path: Path) -> Path:
    """Create a temporary vault directory with standard structure."""
    for d in VAULT_DIRS:
        (tmp_path / d).mkdir()
    return tmp_path


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    """Path for temporary SQLite database."""
    return tmp_path / ".index" / "brain.db"


@pytest.fixture
def tmp_vault_with_taxonomy(tmp_vault: Path) -> Path:
    (tmp_vault / "reference").mkdir(exist_ok=True)
    (tmp_vault / "reference" / "scope-taxonomy.md").write_text(
        "## Whitelist\n\n| scope | purpose |\n|---|---|\n"
        "| `global` | x |\n| `symbiosis-brain` | x |\n"
        "| `alpha` | x |\n| `alpha-seti` | x |\n| `alpha-details` | x |\n"
        "| `alpha-pdf` | x |\n| `alpha-local` | x |\n| `alpha-faq` | x |\n"
        "| `widgetcompare` | x |\n| `beta` | x |\n\n"
        "## Folder ↔ type convention\n\n"
        "| folder | type |\n|---|---|\n"
        "| `decisions/` | `decision` |\n| `patterns/` | `pattern` |\n"
        "| `projects/` | `project` |\n| `wiki/` | `wiki` |\n"
        "| `feedback/` | `feedback` |\n| `mistakes/` | `mistake` |\n"
        "| `research/` | `research` |\n| `user/` | `user` |\n"
        "| `reference/` | `wiki` |\n",
        encoding="utf-8",
    )
    return tmp_vault


@pytest.fixture
def sample_note_content() -> str:
    """Sample markdown note with frontmatter and wiki-links."""
    return """---
title: Dapper vs EF Core
type: decision
scope: beta
created_at: "2025-03-15T10:00:00"
valid_from: "2025-03-15"
tags: [orm, database, performance]
---

## Decision

Chose [[Dapper]] over [[EF Core]] for the [[beta]] project.

## Reasoning

- Performance on large datasets (100k+ rows)
- More control over SQL queries
- Team familiarity with raw SQL

## Related

- See also [[Database Architecture]] for connection pooling setup
- Contradicts earlier preference for [[EF Core]] in smaller projects
"""
