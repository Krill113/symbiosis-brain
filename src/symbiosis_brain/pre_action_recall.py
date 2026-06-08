"""Pre-action recall orchestrator (B1 hook).

Pure-Python module — no I/O side effects (caller wires SearchEngine).
"""
from __future__ import annotations

import os
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
    seen: Any = None,
) -> list[dict[str, Any]]:
    """Run search via injected engine, filter excluded types, dedup, trim to hit_limit.

    `engine` is a duck-typed object with `search(query, scope, limit, mode="gist")`
    returning a list of dicts with shape {path, title, scope, frontmatter, gist}.
    `seen` is an optional duck-typed dedup store (`is_seen(path) -> bool`,
    `record(paths)`); when supplied and `config.recall_dedup_enabled`, already-shown
    hits are dropped BEFORE the cap so fresh hits fill the N slots, then the emitted
    hits are recorded. Both injected so this fn stays unit-testable (no I/O here).

    The cap (`hit_limit`) is itself the relevance gate — top-N of fused RRF, never
    emit-only-STRONG (a multi-token tool-input often matches vector-only, so an
    `_in_both` drop-gate would empty recall in production; see
    [[decisions/2026-06-03-recall-behavior]]). `_in_both` is a label, not a filter.
    """
    if not query:
        return []
    over_limit = min(max(config.hit_limit * 2, 5), 50)
    raw = engine.search(query=query, scope=scope, limit=over_limit, mode="gist")
    excluded = set(config.excluded_note_types)
    filtered = [r for r in raw if _note_type(r) not in excluded]
    dedup_on = seen is not None and config.recall_dedup_enabled
    if dedup_on:
        try:
            filtered = [r for r in filtered if not seen.is_seen(r.get("path", ""))]
        except Exception:
            pass  # fail-open: a dedup error must never drop or empty recall
    hits = filtered[:config.hit_limit]
    if dedup_on:
        try:
            seen.record(h.get("path", "") for h in hits)
        except Exception:
            pass  # fail-open
    return hits


def format_recall_block(query: str, hits: list[dict[str, Any]]) -> str:
    """Format hits as a [recall: N hits for "..."] block. Empty if no hits."""
    if not hits:
        return ""
    snippet = (query or "")[:60].rstrip()
    lines = [f'[recall: {len(hits)} hits for "{snippet}"]']
    for h in hits:
        path = h.get("path", "?")
        gist = h.get("gist") or "(no gist)"
        mark = "★ " if h.get("_in_both") else ""
        lines.append(f"- {mark}{path} — {gist}")
    return "\n".join(lines)


# Tools whose target is an actual code edit (F4 Serena pre-edit advisory).
_SERENA_ADVISORY_TOOLS = {"Edit", "Write", "MultiEdit"}

# Code-file extensions that warrant a "map dependencies first" nudge.
# Default-closed: unknown extension → no advisory.
_CODE_EXTS = {
    ".cs", ".vb", ".py", ".ts", ".tsx", ".js", ".jsx", ".go", ".rs",
    ".java", ".kt", ".cpp", ".cc", ".cxx", ".h", ".hpp", ".c",
}


def serena_advisory(
    tool_name: str,
    tool_input: dict[str, Any],
    *,
    serena_present: bool,
    seen: Any = None,
) -> Optional[str]:
    """Return a one-line Serena pre-edit advisory, or None.

    Fires only when ALL hold: the tool is a code edit (Edit/Write/MultiEdit),
    Serena is present, the target is a code file by extension, and this file
    has not yet been advised this session (per-file-once dedup via `seen`).

    Advisory-only: the caller injects this as additionalContext and NEVER
    blocks the edit. `seen` is a duck-typed SeenStore (`is_seen`, `record`);
    all dedup errors fail-open (advise rather than crash).
    """
    if tool_name not in _SERENA_ADVISORY_TOOLS:
        return None
    if not serena_present:
        return None
    file_path = tool_input.get("file_path") or ""
    if not file_path:
        return None
    if os.path.splitext(file_path)[1].lower() not in _CODE_EXTS:
        return None
    if seen is not None:
        try:
            if seen.is_seen(file_path):
                return None
        except Exception:
            pass  # fail-open: dedup must never suppress on error
        try:
            seen.record([file_path])
        except Exception:
            pass  # fail-open
    name = os.path.basename(file_path)
    return (
        f"[serena] Перед правкой {name}: сначала зависимости через Serena "
        f"(find_referencing_symbols / find_implementations по затрагиваемым "
        f"символам) — увидь картину целиком, не редактируй вслепую."
    )
