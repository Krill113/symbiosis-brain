"""Bounded Codex startup and compaction context."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path, PurePosixPath
from typing import Any, Mapping, Sequence

from symbiosis_brain.adapters.codex.config import CodexConfig
from symbiosis_brain.adapters.codex.secure_io import secure_read_text
from symbiosis_brain.adapters.codex.scope import ScopeResolution, safe_vault_path
from symbiosis_brain.markdown_parser import parse_note
from symbiosis_brain.rotation import parse_handoff_sections

_RELATED_TYPES = frozenset({"decision", "mistake", "feedback", "pattern"})
_DATE_RE = re.compile(r"(?<!\d)(\d{4}-\d{2}-\d{2})(?!\d)")
_H2_RE = re.compile(r"^##[ \t]+(.+?)[ \t]*$", re.MULTILINE)
_SKIP_NOTE_FILES = frozenset({"CLAUDE.md", "README.md", "log.md", "MEMORY.md"})
_MATERIAL_DIRS = frozenset({"files"})


@dataclass(frozen=True)
class HandoffResult:
    path: str
    date: date
    text: str
    stale: bool
    age_days: int


@dataclass(frozen=True)
class BootstrapResult:
    context: str
    scope: str | None
    handoff: HandoffResult | None
    related_count: int
    warning: str | None


def estimate_tokens(text: str) -> int:
    return (len(text) + 2) // 3


def _truncate_at_boundary(text: str, token_budget: int) -> str:
    if token_budget <= 0:
        return ""
    clean = text.strip()
    if estimate_tokens(clean) <= token_budget:
        return clean
    char_limit = max(0, token_budget * 3 - 2)
    head = clean[:char_limit]
    newline = head.rfind("\n")
    sentence = -1
    for match in re.finditer(r"[.!?](?=\s|\Z)", clean):
        if match.end() > char_limit:
            break
        sentence = match.end()
    boundary = max(newline, sentence)
    if boundary > 0:
        return head[:boundary].rstrip()
    return ""


def _h2_sections(text: str) -> list[tuple[str, str]]:
    matches = list(_H2_RE.finditer(text))
    return [
        (
            match.group(1).strip(),
            text[match.start() : matches[index + 1].start()]
            if index + 1 < len(matches)
            else text[match.start() :],
        )
        for index, match in enumerate(matches)
    ]


def extract_project_context(card_text: str, *, token_budget: int) -> str:
    """Keep a small card whole; reduce a large card to active sections."""
    if token_budget <= 0:
        return ""
    if estimate_tokens(card_text.strip()) <= token_budget:
        return card_text.strip()

    selected: list[str] = []
    handoffs = parse_handoff_sections(card_text)
    latest_handoff = max(handoffs, key=lambda section: section.date, default=None)
    patterns = (
        re.compile(r"^(?:current[ -]?status|status)\b", re.IGNORECASE),
        re.compile(r"^(?:next[ -]?(?:up|step)|current next step)\b", re.IGNORECASE),
        re.compile(r"^active initiatives?\b", re.IGNORECASE),
        re.compile(r"^recent decisions?\b", re.IGNORECASE),
        re.compile(r"^(?:project )?dependencies\b", re.IGNORECASE),
    )
    seen_categories: set[int] = set()
    for heading, body in _h2_sections(card_text):
        if heading.lower().startswith("handoff "):
            continue
        for index, pattern in enumerate(patterns):
            if index not in seen_categories and pattern.search(heading):
                selected.append(body.strip())
                seen_categories.add(index)
                break
    if latest_handoff is not None:
        selected.append(latest_handoff.body.strip())

    selected = [part for part in selected if part]
    if not selected:
        return _truncate_at_boundary(card_text, token_budget)
    per_section = max(1, token_budget // len(selected) - 2)
    reduced = "\n\n".join(
        _truncate_at_boundary(part, per_section) for part in selected
    )
    return _truncate_at_boundary(reduced, token_budget)


def _as_date(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if not isinstance(value, str) or not value.strip():
        return None
    raw = value.strip()
    try:
        return date.fromisoformat(raw)
    except ValueError:
        try:
            return datetime.fromisoformat(raw.replace("Z", "+00:00")).date()
        except ValueError:
            return None


def _is_closed(note: Mapping[str, Any], today: date) -> bool:
    raw = note.get("valid_to")
    if not raw:
        return False
    closed = _as_date(raw)
    return closed is not None and closed <= today


def _frontmatter(note: Mapping[str, Any]) -> Mapping[str, Any]:
    value = note.get("frontmatter")
    return value if isinstance(value, Mapping) else {}


def _is_superseded(note: Mapping[str, Any], today: date) -> bool:
    if _is_closed(note, today):
        return True
    superseded_by = _frontmatter(note).get("superseded_by")
    return isinstance(superseded_by, str) and bool(superseded_by.strip())


def _is_handoff_note(note: Mapping[str, Any]) -> bool:
    tags = note.get("tags")
    if isinstance(tags, Sequence) and not isinstance(tags, (str, bytes)):
        if any(isinstance(tag, str) and tag.lower() == "handoff" for tag in tags):
            return True
    title = note.get("title")
    if isinstance(title, str) and "handoff" in title.lower():
        return True
    path = note.get("path")
    if (
        isinstance(path, str)
        and "handoff" in PurePosixPath(path).stem.lower()
    ):
        return True
    content = note.get("content")
    return isinstance(content, str) and (
        bool(parse_handoff_sections(content))
        or "session handoff" in content.lower()
    )


def _fallback_handoff_date(note: Mapping[str, Any], *, today: date) -> date | None:
    dates: set[date] = set()
    raw_valid_from = note.get("valid_from")
    if raw_valid_from not in (None, ""):
        valid_from = _as_date(raw_valid_from)
        if valid_from is None:
            return None
        dates.add(valid_from)

    title = note.get("title")
    if isinstance(title, str) and "handoff" in title.lower():
        matches = _DATE_RE.findall(title)
        if len(matches) > 1:
            return None
        if matches:
            parsed = _as_date(matches[0])
            if parsed is None:
                return None
            dates.add(parsed)

    path = note.get("path")
    if isinstance(path, str) and "handoff" in PurePosixPath(path).stem.lower():
        matches = _DATE_RE.findall(PurePosixPath(path).name)
        if len(matches) > 1:
            return None
        if matches:
            parsed = _as_date(matches[0])
            if parsed is None:
                return None
            dates.add(parsed)

    if len(dates) != 1:
        return None
    handoff_date = next(iter(dates))
    return handoff_date if handoff_date <= today else None


def _strip_future_handoff_sections(text: str, *, today: date) -> str:
    future = [
        section
        for section in parse_handoff_sections(text)
        if section.date > today
    ]
    if not future:
        return text
    chunks: list[str] = []
    start = 0
    for section in future:
        chunks.append(text[start:section.start])
        start = section.end
    chunks.append(text[start:])
    return "".join(chunks).rstrip()


def _handoff_result(
    path: str,
    handoff_date: date,
    text: str,
    *,
    stale_days: int,
    today: date,
) -> HandoffResult:
    age_days = (today - handoff_date).days
    return HandoffResult(
        path=path,
        date=handoff_date,
        text=text.strip(),
        stale=age_days > stale_days,
        age_days=age_days,
    )


def select_handoff(
    card_text: str,
    notes: Sequence[Mapping[str, Any]],
    *,
    stale_days: int,
    today: date,
) -> HandoffResult | None:
    """Choose the newest open handoff with deterministic tie-breaking."""
    candidates: list[tuple[date, bool, str, str]] = []
    for section in parse_handoff_sections(card_text):
        if section.date <= today:
            candidates.append((section.date, True, "project-card", section.body))

    for note in notes:
        if _is_superseded(note, today) or not _is_handoff_note(note):
            continue
        path = note.get("path")
        content = note.get("content")
        if not isinstance(path, str) or not isinstance(content, str):
            continue
        sections = parse_handoff_sections(content)
        if sections:
            eligible = [section for section in sections if section.date <= today]
            if not eligible:
                continue
            selected = max(eligible, key=lambda section: section.date)
            handoff_date = selected.date
            selected_text = selected.body
        else:
            handoff_date = _fallback_handoff_date(note, today=today)
            if handoff_date is None:
                continue
            selected_text = content
        is_current = "/archive/" not in f"/{path.replace('\\', '/')}"
        candidates.append((handoff_date, is_current, path, selected_text))

    if not candidates:
        return None
    handoff_date, _is_current, path, text = max(
        candidates,
        key=lambda item: (item[0], item[1], item[2]),
    )
    return _handoff_result(
        path,
        handoff_date,
        text,
        stale_days=stale_days,
        today=today,
    )


def _read_optional(root: Path, relative: str) -> str:
    try:
        return secure_read_text(root, relative)
    except (OSError, UnicodeError, ValueError):
        return ""


def _project_card(vault: Path, scope: str) -> tuple[str | None, str]:
    for relative in (f"{scope}/{scope}.md", f"projects/{scope}.md"):
        text = _read_optional(vault, relative)
        if text:
            return relative, text
    return None, ""


def _scope_note_paths(vault: Path, scope: str) -> list[str]:
    scope_root = safe_vault_path(vault, scope)
    if scope_root is None or not scope_root.is_dir():
        return []
    found: list[str] = []

    def visit(directory: Path, prefix: str) -> None:
        try:
            with os.scandir(directory) as entries:
                snapshot = sorted(entries, key=lambda entry: os.path.normcase(entry.name))
        except OSError:
            return
        for entry in snapshot:
            name = entry.name
            if name.startswith("."):
                continue
            try:
                linked = entry.is_symlink()
                is_junction = bool(
                    getattr(entry, "is_junction", lambda: False)()
                )
                if linked or is_junction:
                    continue
                descend = (
                    entry.is_dir(follow_symlinks=False)
                )
            except OSError:
                descend = False
            relative = f"{scope}/{prefix}{name}"
            if (
                os.path.normcase(name).endswith(".md")
                and name not in _SKIP_NOTE_FILES
            ):
                found.append(PurePosixPath(relative).as_posix())
            if descend and name not in _MATERIAL_DIRS:
                visit(Path(entry.path), f"{prefix}{name}/")

    visit(scope_root, "")
    return found


def _read_scope_notes(vault: Path, scope: str) -> list[Mapping[str, Any]]:
    notes: list[Mapping[str, Any]] = []
    for relative in _scope_note_paths(vault, scope):
        text = _read_optional(vault, relative)
        if not text:
            continue
        try:
            parsed = parse_note(text)
        except Exception:
            continue
        if parsed.get("scope") != scope:
            continue
        notes.append(
            {
                "path": relative,
                "title": parsed.get("title"),
                "content": parsed.get("body", ""),
                "scope": parsed.get("scope"),
                "note_type": parsed.get("type"),
                "tags": parsed.get("tags", []),
                "frontmatter": parsed.get("extra", {}),
                "valid_from": parsed.get("valid_from"),
                "valid_to": parsed.get("valid_to"),
            }
        )
    return sorted(
        notes,
        key=lambda note: (
            _as_date(note.get("valid_from")) or date.min,
            str(note.get("path", "")),
        ),
        reverse=True,
    )


def _note_gist(note: Mapping[str, Any]) -> str:
    gist = _frontmatter(note).get("gist")
    if isinstance(gist, str) and gist.strip():
        return gist.strip()
    content = note.get("content")
    if isinstance(content, str):
        for paragraph in re.split(r"\n\s*\n", content):
            clean = " ".join(
                line.strip()
                for line in paragraph.splitlines()
                if line.strip() and not line.lstrip().startswith("#")
            )
            if clean:
                return _truncate_at_boundary(clean, 50)
    title = note.get("title")
    return title.strip() if isinstance(title, str) else ""


def _eligible_related(
    notes: Sequence[Mapping[str, Any]],
    *,
    scope: str,
    today: date,
    excluded_paths: set[str],
) -> list[Mapping[str, Any]]:
    return [
        note
        for note in notes
        if note.get("scope") == scope
        and note.get("note_type") in _RELATED_TYPES
        and isinstance(note.get("path"), str)
        and note["path"] not in excluded_paths
        and not _is_superseded(note, today)
        and (
            _as_date(note.get("valid_from")) is None
            or _as_date(note.get("valid_from")) <= today
        )
        and not _is_handoff_note(note)
    ]


def _select_related(
    notes: Sequence[Mapping[str, Any]],
    *,
    scope: str,
    today: date,
    excluded_paths: set[str],
) -> list[Mapping[str, Any]]:
    return _eligible_related(
        notes,
        scope=scope,
        today=today,
        excluded_paths=excluded_paths,
    )[:2]


def _first_next_step(*texts: str) -> str:
    for text in texts:
        for heading, body in _h2_sections(text):
            if re.search(r"^next[ -]?(?:up|step)\b", heading, re.IGNORECASE):
                for line in body.splitlines()[1:]:
                    clean = line.strip().lstrip("-* ").strip()
                    if clean:
                        return _truncate_at_boundary(clean, 80)
    return ""


def _render_bootstrap(
    *,
    source: str,
    scope: str | None,
    critical: str,
    project: str,
    handoff: HandoffResult | None,
    related: Sequence[Mapping[str, Any]],
) -> str:
    compact = source == "compact"
    lines = ["[symbiosis-brain bootstrap]", f"scope: {scope or 'unresolved'}"]
    if handoff is not None:
        lines.extend(
            [
                f"handoff: {handoff.date.isoformat()}",
                f"handoff_state: {'stale' if handoff.stale else 'current'}",
            ]
        )
        if handoff.stale:
            lines.append(f"handoff_age_days: {handoff.age_days}")
    next_step = _first_next_step(handoff.text if handoff else "", project)
    if next_step:
        lines.append(f"next_step: {next_step}")

    if critical and (not compact or scope is None):
        bounded_critical = _truncate_at_boundary(critical, 180)
        if bounded_critical:
            lines.extend(["", "critical:", bounded_critical])
    if project:
        bounded_project = extract_project_context(
            project,
            token_budget=500 if compact else 1000,
        )
        if bounded_project:
            lines.extend(["", "project:", bounded_project])
    if handoff is not None:
        bounded_handoff = _truncate_at_boundary(
            handoff.text,
            650 if compact else 900,
        )
        if bounded_handoff:
            lines.extend(["", "handoff:", bounded_handoff])
    if not compact and related:
        rendered_related: list[str] = []
        for note in related[:2]:
            title = note.get("title")
            prefix = title.strip() if isinstance(title, str) and title.strip() else "Note"
            bounded_gist = _truncate_at_boundary(_note_gist(note), 120)
            if bounded_gist:
                rendered_related.append(f"- {prefix}: {bounded_gist}")
        if rendered_related:
            lines.extend(["", "related:", *rendered_related])
    if compact:
        guidance = (
            "Verify stale handoff; use brain_read."
            if handoff is not None and handoff.stale
            else "Use brain_read for details."
        )
    else:
        guidance = (
            "Use brain_search/brain_read for details; verify stale handoffs."
            if handoff is not None and handoff.stale
            else "Use brain_search/brain_read for details."
        )
    lines.extend(["", guidance])
    return _truncate_at_boundary("\n".join(lines), 1500 if compact else 3200)


def build_bootstrap(
    vault: Path,
    resolution: ScopeResolution,
    config: CodexConfig,
    *,
    source: str,
    today: date | None = None,
) -> BootstrapResult:
    """Build a deterministic and token-bounded canonical context envelope."""
    current_day = today or date.today()
    if not resolution.trusted:
        return BootstrapResult(
            context="",
            scope=None,
            handoff=None,
            related_count=0,
            warning=resolution.warning or "workspace unavailable",
        )
    critical = _read_optional(vault, "CRITICAL_FACTS.md")
    if resolution.scope is not None:
        logical_scope = Path(vault) / resolution.scope
        if os.path.lexists(logical_scope) and safe_vault_path(
            vault,
            resolution.scope,
        ) is None:
            return BootstrapResult(
                context=_render_bootstrap(
                    source=source,
                    scope=resolution.scope,
                    critical=critical,
                    project="",
                    handoff=None,
                    related=[],
                ),
                scope=resolution.scope,
                handoff=None,
                related_count=0,
                warning="unsafe vault path",
            )

    if resolution.scope is None:
        context = _render_bootstrap(
            source=source,
            scope=None,
            critical=critical,
            project="",
            handoff=None,
            related=[],
        )
        return BootstrapResult(
            context=context if critical else "",
            scope=None,
            handoff=None,
            related_count=0,
            warning=resolution.warning,
        )

    scope = resolution.scope
    card_path, card_text = _project_card(vault, scope)
    card_text = _strip_future_handoff_sections(card_text, today=current_day)
    notes = _read_scope_notes(vault, scope)
    warning = resolution.warning
    handoff = select_handoff(
        card_text,
        notes,
        stale_days=config.handoff_stale_days,
        today=current_day,
    )
    excluded_paths = {
        path
        for path in (
            card_path,
            handoff.path if handoff is not None else None,
        )
        if isinstance(path, str)
    }
    related: list[Mapping[str, Any]] = []
    if source != "compact":
        related = _select_related(
            notes,
            scope=scope,
            today=current_day,
            excluded_paths=excluded_paths,
        )

    if not card_text:
        warning = warning or "project card missing"
    context = _render_bootstrap(
        source=source,
        scope=scope,
        critical=critical,
        project=card_text,
        handoff=handoff,
        related=related,
    )
    return BootstrapResult(
        context=context,
        scope=scope,
        handoff=handoff,
        related_count=len(related),
        warning=warning,
    )
