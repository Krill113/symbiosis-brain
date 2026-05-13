"""Pre-action recall orchestrator (B1 hook).

Pure-Python module — no I/O side effects (caller wires SearchEngine).
"""
from __future__ import annotations

from typing import Any, Optional

from symbiosis_brain.pre_action_config import PreActionConfig


def build_query(tool_name: str, tool_input: dict[str, Any], max_chars: int) -> Optional[str]:
    """Extract a search query from a tool call's input args.

    Returns None for unsupported tools (so caller can skip cleanly).
    Returns "" if the supported tool has an empty/missing primary field.
    """
    if tool_name == "Task":
        prompt = tool_input.get("prompt") or ""
        return prompt[:max_chars]
    if tool_name == "Edit":
        path = tool_input.get("file_path", "")
        new_s = (tool_input.get("new_string") or "")[:300]
        combined = " ".join(p for p in (path, new_s) if p)
        return combined[:max_chars]
    if tool_name == "Write":
        path = tool_input.get("file_path", "")
        content = (tool_input.get("content") or "")[:300]
        combined = " ".join(p for p in (path, content) if p)
        return combined[:max_chars]
    if tool_name == "MultiEdit":
        path = tool_input.get("file_path", "")
        edits = tool_input.get("edits") or []
        new_strings = [(e.get("new_string") or "")[:100] for e in edits]
        combined = " ".join([path, *new_strings]).strip()
        return combined[:max_chars]
    if tool_name == "NotebookEdit":
        src = tool_input.get("new_source") or ""
        return src[:max_chars]
    if tool_name == "Bash":
        cmd = tool_input.get("command") or ""
        return cmd[:max_chars]
    return None


def _note_type(note: dict[str, Any]) -> Optional[str]:
    """Extract note type from SearchEngine result. Type lives inside
    `frontmatter` dict, NOT at top level (see search.py mode=gist handling)."""
    fm = note.get("frontmatter") or {}
    return fm.get("type") if isinstance(fm, dict) else None


def run_recall(
    query: str,
    scope: Optional[str],
    config: PreActionConfig,
    engine: Any,
) -> list[dict[str, Any]]:
    """Run search via injected engine, filter excluded types, trim to hit_limit.

    `engine` is a duck-typed object with `search(query, scope, limit, mode="gist")`
    returning a list of dicts with shape {path, title, scope, frontmatter, gist}.
    Injected so this fn is unit-testable.
    """
    if not query:
        return []
    over_limit = max(config.hit_limit * 2, 5)
    raw = engine.search(query=query, scope=scope, limit=over_limit, mode="gist")
    excluded = set(config.excluded_note_types)
    filtered = [r for r in raw if _note_type(r) not in excluded]
    return filtered[:config.hit_limit]


def format_recall_block(query: str, hits: list[dict[str, Any]]) -> str:
    """Format hits as a [recall: N hits for "..."] block. Empty if no hits."""
    if not hits:
        return ""
    snippet = (query or "")[:60].rstrip()
    lines = [f'[recall: {len(hits)} hits for "{snippet}"]']
    for h in hits:
        path = h.get("path", "?")
        gist = h.get("gist") or "(no gist)"
        lines.append(f"- {path} — {gist}")
    return "\n".join(lines)
