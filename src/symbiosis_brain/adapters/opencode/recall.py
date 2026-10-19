"""OpenCode recall bridge that prepares hits without consuming delivery state."""

from __future__ import annotations

from typing import Any

from symbiosis_brain.pre_action_config import PreActionConfig
from symbiosis_brain.pre_action_recall import run_recall


def recall_hits(query: str, scope: str, engine: Any, *, limit: int) -> list[dict]:
    """Use the canonical ranked/temporal search path without marking hits seen."""
    if not isinstance(scope, str) or not scope.strip():
        raise ValueError("explicit scope required")
    if not isinstance(query, str) or not query.strip():
        return []
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise ValueError("limit must be a positive integer")
    config = PreActionConfig(hit_limit=limit, recall_dedup_enabled=False)
    return run_recall(query, scope, config, engine, seen=None, log_ctx=None)
