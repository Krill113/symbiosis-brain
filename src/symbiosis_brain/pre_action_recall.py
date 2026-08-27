"""Pre-action recall orchestrator (B1 hook).

Pure-Python module — no I/O side effects (caller wires SearchEngine).
"""
from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any, Optional

from symbiosis_brain.pre_action_config import PreActionConfig

_DEFAULT_FTS_MODE = "all_then_any"
"""Запрошенный режим лексической половины для ОБОИХ инжект-путей (§4.2, Q3).

Литерал, а не `from symbiosis_brain.search import FTS_MODE_ALL_THEN_ANY`:
этот модуль импортируется КАЖДЫМ PreToolUse-процессом, в том числе теми, что
выходят до всякого поиска (`__main__.py:429-442`), а `search` тянет за собой
numpy (`search.py:13`). Канонические константы живут в search.py (I-17);
tests/test_pre_action_recall.py следит, чтобы значения не разъехались."""


_EDIT_BODY_MAX_CHARS = 400
"""Сколько символов самой правки идёт в запрос (I-21, §4.4).

Не путать с `PreActionConfig.query_max_chars` (500): тот режет ИТОГ, этот —
содержимое до склейки с именем файла."""


def _edit_query(file_path: str, new_text: str, max_chars: int) -> str:
    """Query for an Edit/Write/MultiEdit call: file STEM + head of the edit.

    Directories are dropped on purpose (I-21, §4.4 C8). The old formula fed the
    whole `file_path` in, and once the lexical half started ORing tokens a
    single edit pulled 417 of 1466 notes on path tokens alone. The basename is
    signal — the tree is noise; the edit body is what carries the meaning.
    """
    stem = os.path.splitext(os.path.basename(file_path or ""))[0]
    head = " ".join((new_text or "").split())[:_EDIT_BODY_MAX_CHARS]
    return f"{stem} {head}".strip()[:max_chars]


def build_query(tool_name: str, tool_input: dict[str, Any], max_chars: int) -> Optional[str]:
    """Extract a search query from a tool call's input args.

    Returns None for unsupported tools (so caller can skip cleanly).
    Returns "" if the supported tool has an empty/missing primary field.
    """
    if tool_name in ("Task", "Agent"):
        prompt = tool_input.get("prompt") or ""
        return prompt[:max_chars]
    if tool_name == "Edit":
        return _edit_query(tool_input.get("file_path", ""),
                           tool_input.get("new_string") or "", max_chars)
    if tool_name == "Write":
        return _edit_query(tool_input.get("file_path", ""),
                           tool_input.get("content") or "", max_chars)
    if tool_name == "MultiEdit":
        edits = tool_input.get("edits") or []
        joined = " ".join(
            (e.get("new_string") or "") for e in edits if isinstance(e, dict)
        )
        return _edit_query(tool_input.get("file_path", ""), joined, max_chars)
    if tool_name == "NotebookEdit":
        src = tool_input.get("new_source") or ""
        return src[:max_chars]
    if tool_name in ("Bash", "PowerShell"):
        cmd = tool_input.get("command") or ""
        return cmd[:max_chars]
    return None


def _note_type(note: dict[str, Any]) -> Optional[str]:
    """Note type of a SearchEngine result.

    The canonical source is the top-level ``note_type`` COLUMN. ``parse_note``
    pops ``type`` out of the YAML frontmatter into its own field, so a real
    search row NEVER carries it under ``frontmatter`` — that dict only holds
    the leftover ``extra`` keys (a note with a gist gives ``{"gist": ...}``).
    Reading only ``frontmatter["type"]`` made ``excluded_note_types`` a silent
    no-op on every path; the frontmatter lookup stays as a fallback for
    hand-built rows and for any caller that shapes its own dicts.
    """
    nt = note.get("note_type")
    if isinstance(nt, str) and nt:
        return nt
    fm = note.get("frontmatter") or {}
    return fm.get("type") if isinstance(fm, dict) else None


def run_recall(
    query: str,
    scope: Optional[str],
    config: PreActionConfig,
    engine: Any,
    seen: Any = None,
    *,
    fts_mode: str = _DEFAULT_FTS_MODE,
    log_ctx: Any = None,
) -> list[dict[str, Any]]:
    """Run search via injected engine, filter excluded types, dedup, trim to hit_limit.

    `engine` is a duck-typed object with `search(query, scope, limit, mode="gist",
    fts_mode=..., log_ctx=None, stats=...)` returning a list of dicts with shape
    {path, title, scope, frontmatter, gist}. `seen` is an optional duck-typed dedup store (`is_seen(path)
    -> bool`, `record(paths)`); when supplied and `config.recall_dedup_enabled`,
    already-shown hits are dropped BEFORE the cap so fresh hits fill the N slots,
    then the emitted hits are recorded. Both injected so this fn stays unit-testable
    (no I/O here except the retrieval log below).

    The cap (`hit_limit`) is itself the relevance gate — top-N of fused RRF, never
    emit-only-STRONG (a multi-token tool-input often matches vector-only, so an
    `_in_both` drop-gate would empty recall in production; see
    [[decisions/2026-06-03-recall-behavior]]). `_in_both` is a label, not a filter.

    `log_ctx` (I-8) makes THIS function the write point for the two hook paths,
    and it is deliberately NOT forwarded into `engine.search`: search() returns
    the RAW pool (over_limit = 6 at hit_limit=3), while the agent sees the list
    after the type filter, the SeenStore and the cap. Logging inside search()
    would store six ranks nobody was shown, and "surfaced but not read" (§6.2)
    would be counted over notes the agent never got (§2.9).

    `fts_mode` is the REQUESTED mode; what reaches the log is the EFFECTIVE one,
    handed back through the `stats` out-param — run_recall cannot see the outcome
    of `all_then_any`, that is decided inside search(). `vec_enabled` comes from
    the same `stats` and from nowhere else: reading `engine._vec_enabled` would be
    a lie under a MagicMock (every getattr is truthy there), and a hardcoded False
    would be a lie in production (I-8).
    """
    if not query:
        return []
    over_limit = min(max(config.hit_limit * 2, 5), 50)
    stats: dict[str, Any] = {}
    t0 = time.perf_counter()
    raw = engine.search(query=query, scope=scope, limit=over_limit, mode="gist",
                        fts_mode=fts_mode, log_ctx=None, stats=stats)
    latency_ms = int((time.perf_counter() - t0) * 1000)
    excluded = set(config.excluded_note_types)
    filtered = [r for r in raw if _note_type(r) not in excluded]
    dedup_on = seen is not None and config.recall_dedup_enabled
    dedup_dropped = 0
    if dedup_on:
        try:
            kept = [r for r in filtered if not seen.is_seen(r.get("path", ""))]
            dedup_dropped = len(filtered) - len(kept)
            filtered = kept
        except Exception:
            pass  # fail-open: a dedup error must never drop or empty recall
    hits = filtered[:config.hit_limit]
    if dedup_on:
        try:
            seen.record(h.get("path", "") for h in hits)
        except Exception:
            pass  # fail-open
    if log_ctx is not None:
        try:
            from symbiosis_brain import retrieval_log

            retrieval_log.record(
                log_ctx,
                query=query,
                scope=scope,
                mode="gist",
                fts_mode=stats.get("fts_mode", fts_mode),
                hits=hits,
                latency_ms=latency_ms,
                vec_enabled=bool(stats.get("vec_enabled", False)),
                dedup_dropped=dedup_dropped,
            )
        except Exception:
            pass  # fail-open: telemetry never empties a recall
    return hits


def format_recall_block(query: str, hits: list[dict[str, Any]]) -> str:
    """Format hits as a [recall: N hits for "..."] block. Empty if no hits.

    ★ marks a STRONG hit: the note landed in the top-3 of BOTH search halves
    (`_strong`, I-22). It used to mark `_in_both` — "showed up in both lists" —
    which after the AND->OR fix would have fired on 27.5 % of shown hits and
    54.9 % of queries (measured on the real PreToolUse render profile: pool 6,
    cap 3, 142 live queries), i.e. stopped distinguishing anything. `_in_both`
    itself is untouched: it stays in the hit dict and in the journal.
    This is the ONLY surface that prints ★ — the prompt-path [memory:] block is
    rendered by `_shape_hits` (`__main__.py:223-237`) and has no stars at all.
    See the owner-side decision record on the strength mark (2026-08-26).
    """
    if not hits:
        return ""
    snippet = (query or "")[:60].rstrip()
    lines = [f'[recall: {len(hits)} hits for "{snippet}"]']
    for h in hits:
        path = h.get("path", "?")
        gist = h.get("gist") or "(no gist)"
        mark = "★ " if h.get("_strong") else ""
        lines.append(f"- {mark}{path} — {gist}")
    return "\n".join(lines)


# Matching window for a SUBAGENT prompt (C3). Deliberately WIDER than
# PreActionConfig.query_max_chars (500): recall only needs a topical
# fingerprint of the brief, while a route trigger is a literal phrase that
# usually sits in the middle of a long task description.
AGENT_ROUTE_MATCH_MAX_CHARS = 4000

# Routes eligible on a subagent prompt. class:"action" is excluded on purpose:
# those live in the compiled TSV read by the pure-bash matcher in
# brain-pre-action-trigger.sh, and a second copy here would double-inject.
_AGENT_ROUTE_CLASSES = ("augment", "supersede")


def agent_route_block(
    prompt: str,
    routes: list,
    *,
    scope: str | None,
    vault: Path | None,
    roster: set[str] | None,
    cap: int,
    session_id: str = "",
    seen_ttl_seconds: int = 86400,
) -> str:
    """Route hints for a subagent prompt (tool_name in {"Task", "Agent"}).

    Matches ``tool_input["prompt"]`` — truncated to AGENT_ROUTE_MATCH_MAX_CHARS —
    against ``Route.triggers`` (Python regexes). ``command_triggers`` are NOT
    used here: they are POSIX ERE for ``grep -E`` and Python's ``re`` silently
    misparses ``[[:space:]]`` (see action_rules.py docstring).

    Only class augment/supersede routes with non-empty triggers fire; ``cap``
    keeps the top-K by priority DESC (match_routes' own rule). ``when:`` gates
    are evaluated as usual — against the PARENT session's roster. PreToolUse on
    Task/Agent runs in the session that is ABOUT to spawn the subagent (the
    subagent has no session yet), and ``_roster_set`` reads
    ``brain-mcp-roster-<session_id>`` straight from that payload; the same roster
    already feeds serena_advisory in ``__main__``. So ``mcp:*`` gates here are
    normally OPEN. Only a missing roster file (roster is None) makes ``_when_ok``
    fail closed and silently drop the route.

    Dedup goes through tool_routing.dedup_augment, i.e. the SHARED
    ``brain-route-seen-<sid>`` store: an augment hint already shown on the
    user's prompt this session is not repeated on the subagent's. supersede is
    never deduped.

    Returns "" when nothing matched, ``routes`` is empty, or ANY error occurs
    (fail-open — this runs inside PreToolUse and must never block a tool call).
    Note: this is the one function in this module that touches disk, via the
    dedup store owned by tool_routing.
    """
    try:
        if not prompt or not routes:
            return ""
        from symbiosis_brain import tool_routing as tr

        candidates = [
            r for r in routes
            if getattr(r, "cls", "") in _AGENT_ROUTE_CLASSES and getattr(r, "triggers", None)
        ]
        if not candidates:
            return ""
        matched = tr.match_routes(
            prompt[:AGENT_ROUTE_MATCH_MAX_CHARS],
            candidates,
            roster=roster,
            scope=scope,
            vault=vault,
            cap=cap,
        )
        if not matched:
            return ""
        matched = tr.dedup_augment(matched, session_id, ttl_seconds=seen_ttl_seconds)
        hints = [
            h for h in ((r.get("hint") or "").strip() for r in tr.route_hints(matched)) if h
        ]
        if not hints:
            return ""
        lines = [f"[routes: {len(hints)} hints for subagent]"]
        lines.extend(f"- {h}" for h in hints)
        return "\n".join(lines)
    except Exception:
        return ""  # fail-open: a routing hint is never worth blocking a tool call


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
