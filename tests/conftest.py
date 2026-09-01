import os
import sqlite3
import tempfile
from pathlib import Path

import pytest

from symbiosis_brain.sync import VAULT_DIRS


@pytest.fixture(autouse=True)
def _restore_embedder_singleton():
    """Guard against cross-test leakage of the legacy embedder singleton
    (symbiosis_brain.search._MODEL_NAME / _embedder).

    SearchEngine resolves each instance's own model from the DB (З3), but the
    actual embed call still goes through this one process-wide singleton
    (_get_embedder/_embed) — _set_active_model swaps it, lazily, whenever a
    call needs a model different from whichever one is currently loaded. In
    production that swap fires at most once per process (a server only ever
    runs one model at a time); in a test session many different models get
    exercised back to back, and any test that constructs a SearchEngine for a
    non-default model — or calls _embed_documents/_embed_query with one
    directly — would otherwise leave that model active for every test that
    runs after it: the next SearchEngine on an empty DB resolves to it as the
    "default" (_resolve_model_name's DB-empty fallback IS this global), and
    its first real embed call tries to load — or download — a model nobody
    asked it for. Snapshot/restore here, once, rather than trust every current
    and future test to monkeypatch both names by hand.
    """
    import symbiosis_brain.search as _sb_search
    model_before, embedder_before = _sb_search._MODEL_NAME, _sb_search._embedder
    yield
    _sb_search._MODEL_NAME, _sb_search._embedder = model_before, embedder_before


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


@pytest.fixture(scope="session", autouse=True)
def _isolate_hook_artifacts(tmp_path_factory):
    """Keep the test suite out of the developer's live hook artifacts (A-N3).

    Hook-side code derives its temp dir from TMPDIR/TEMP (pre_action_config
    ._tmp_dir) and its debug log from SYMBIOSIS_BRAIN_DEBUG_LOG, so without this
    a plain `pytest` run appends progress bars and pytest-of-* paths straight
    into the user's $TMP/brain-hook-debug.log and drops seen-store files next to
    the live ones. Subprocess-spawning tests inherit these variables through
    os.environ.copy(), so the isolation reaches them too.

    ORDER MATTERS: mktemp() runs FIRST so pytest resolves its own basetemp under
    the real system temp before we move TMPDIR. Setting TMPDIR earlier would nest
    every tmp_path inside this directory and blow past Windows' 260-char limit.
    Per-test monkeypatch.setenv("TMPDIR", ...) still wins over this fixture —
    the existing tests that isolate themselves keep working unchanged.
    """
    real_tmp = tempfile.gettempdir()          # ДО подмены TMPDIR — см. ниже
    base = tmp_path_factory.mktemp("sb-hook-tmp")
    mp = pytest.MonkeyPatch()
    mp.setenv("TMPDIR", str(base))
    mp.setenv("TEMP", str(base))
    mp.setenv("SYMBIOSIS_BRAIN_DEBUG_LOG", str(base / "brain-hook-debug.log"))
    # fastembed caches its ONNX model under <tempdir>/fastembed_cache
    # (fastembed/common/utils.py:53-54). With TMPDIR moved above, every subprocess
    # that runs `search-gist` without --skip-memory would re-download ~130 MB into a
    # throwaway dir: 4 tests today, 13 after CP-5. Pin the cache to the REAL temp dir.
    mp.setenv("FASTEMBED_CACHE_PATH", str(Path(real_tmp) / "fastembed_cache"))
    yield base
    mp.undo()
