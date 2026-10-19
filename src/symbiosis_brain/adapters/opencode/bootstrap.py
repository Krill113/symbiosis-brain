"""Read-only, scope-bounded project bootstrap for the OpenCode adapter."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path

import frontmatter

from symbiosis_brain.markdown_parser import extract_wikilinks, parse_note
from symbiosis_brain.scope_resolver import normalize_scope, parse_marker

from .protocol import ContextBlock

_SCOPE_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_HANDOFF_RE = re.compile(r"^(#{1,6})\s+Handoff\s+(\d{4}-\d{2}-\d{2})\s*$", re.I | re.M)
_SECTION_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$", re.M)


@dataclass(frozen=True)
class ScopeContext:
    scope: str | None
    umbrella: str | None
    status: str | None = None
    global_only: bool = False
    diagnostics: tuple[str, ...] = field(default_factory=tuple)


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(parent.resolve(strict=False))
        return True
    except (OSError, ValueError):
        return False


def _taxonomy_marks_broad(vault: Path, scope: str) -> bool:
    """Read only the taxonomy entry for this marker; unknown taxonomy fails open."""
    try:
        root = Path(vault).resolve(strict=True)
        taxonomy = (root / "reference" / "scope-taxonomy.md").resolve(strict=True)
        if not root.is_dir() or not _is_within(taxonomy, root) or not taxonomy.is_file():
            return False
        text = taxonomy.read_text(encoding="utf-8", errors="replace")
    except (OSError, RuntimeError):
        return False

    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.startswith("|") or "---" in stripped:
            continue
        cells = [cell.strip() for cell in stripped.strip("|").split("|")]
        if len(cells) < 2 or cells[0].strip("`") != scope:
            continue
        category = re.sub(r"[*_]", "", cells[1]).strip().strip("`").casefold()
        kind = re.split(r"[\s(]", category, maxsplit=1)[0]
        return kind in {"workspace-root", "umbrella", "зонтик"}
    return False


def resolve_project(cwd: Path, vault: Path, boundary: Path | None) -> ScopeContext:
    """Resolve only within a caller-supplied project/worktree boundary.

    Without a known boundary the adapter deliberately returns global-only and
    never inspects ancestor markers.
    """
    cwd = Path(cwd)
    if boundary is None:
        return ScopeContext(None, None, global_only=True, diagnostics=("project-boundary-unknown",))
    boundary = Path(boundary)
    if not _is_within(cwd, boundary):
        return ScopeContext(None, None, global_only=True, diagnostics=("cwd-outside-project-boundary",))
    cursor = cwd.resolve(strict=False)
    stop = boundary.resolve(strict=False)
    depth = 0
    while True:
        marker = parse_marker(cursor / "CLAUDE.md")
        if marker is not None:
            if marker.version != 1:
                return ScopeContext(None, None, global_only=True, diagnostics=("unsupported-scope-marker-version",))
            scope = marker.scope
            umbrella = marker.umbrella
            if not _SCOPE_RE.fullmatch(scope) or (umbrella is not None and not _SCOPE_RE.fullmatch(umbrella)):
                return ScopeContext(None, None, global_only=True, diagnostics=("invalid-scope-marker",))
            if depth > 0 and _taxonomy_marks_broad(Path(vault), scope):
                return ScopeContext(None, None, global_only=True, diagnostics=("broad-ancestor-scope-rejected",))
            return ScopeContext(scope, umbrella, marker.status)
        if cursor == stop or cursor.parent == cursor:
            break
        cursor = cursor.parent
        depth += 1
    fallback = normalize_scope(Path(cwd).name)
    if not fallback or not _SCOPE_RE.fullmatch(fallback):
        return ScopeContext(None, None, global_only=True, diagnostics=("invalid-project-folder",))
    return ScopeContext(fallback, None)


def is_superseded(note: dict, *, now: datetime | None = None) -> bool:
    """Adapter-local temporal predicate, parity-tested against the Core API."""
    closed_at = note.get("valid_to")
    if not closed_at:
        return False
    try:
        dt = datetime.fromisoformat(str(closed_at))
    except (ValueError, TypeError):
        return False
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return dt <= current


def _safe_read(path: Path, root: Path, diagnostics: list[str]) -> str | None:
    try:
        resolved = path.resolve(strict=True)
        if not _is_within(resolved, root.resolve(strict=True)) or not resolved.is_file():
            diagnostics.append("bootstrap-path-outside-vault")
            return None
        return resolved.read_text(encoding="utf-8", errors="replace")
    except (OSError, RuntimeError):
        diagnostics.append("bootstrap-file-unavailable")
        return None


def _valid_content(
    content: str,
    now: datetime,
    *,
    expected_scope: str | None = None,
    require_global: bool = False,
    diagnostics: list[str] | None = None,
) -> tuple[str, dict] | None:
    try:
        note = parse_note(content)
        metadata = frontmatter.loads(content).metadata
    except Exception:
        if diagnostics is not None:
            diagnostics.append("bootstrap-note-invalid")
        return None
    explicit_scope = metadata.get("scope") if "scope" in metadata else None
    if "scope" in metadata and (
        not isinstance(explicit_scope, str)
        or (expected_scope is not None and explicit_scope != expected_scope)
        or (require_global and explicit_scope != "global")
    ):
        if diagnostics is not None:
            diagnostics.append("bootstrap-note-scope-mismatch")
        return None
    if is_superseded(note, now=now):
        return None
    valid_from = note.get("valid_from")
    if valid_from:
        try:
            starts_at = datetime.fromisoformat(str(valid_from))
        except (ValueError, TypeError):
            starts_at = None
        if starts_at is not None:
            if starts_at.tzinfo is None:
                starts_at = starts_at.replace(tzinfo=timezone.utc)
            current = now if now.tzinfo is not None else now.replace(tzinfo=timezone.utc)
            if starts_at > current:
                return None
    return note.get("body", content), note


def _section_end(body: str, heading: re.Match[str], headings: list[re.Match[str]]) -> int:
    level = len(heading.group(1))
    for candidate in headings:
        if candidate.start() > heading.start() and len(candidate.group(1)) <= level:
            return candidate.start()
    return len(body)


def _has_substantive_text(text: str) -> bool:
    without_headings = re.sub(r"(?m)^\s*#{1,6}\s+.*$", "", text)
    return any(char.isalnum() for char in without_headings)


def _extract_intent(body: str) -> str:
    matches = list(_SECTION_RE.finditer(body))
    wanted = (
        "purpose", "why", "зачем", "design intent", "intent", "mission", "goal", "цель", "замысел",
        "current status", "текущее состояние", "актуальный статус", "active initiatives", "инициативы", "roadmap",
    )
    picked: list[str] = []
    for i, match in enumerate(matches):
        title = match.group(2).strip().lower()
        if title not in wanted:
            continue
        end = _section_end(body, match, matches)
        part = body[match.start():end].strip()
        if part:
            picked.append(part)
    if picked:
        return "\n\n".join(picked)
    # Do not let stale handoff sections leak through the fallback when a card
    # uses a localized or nonstandard heading for its purpose.
    handoff = _HANDOFF_RE.search(body)
    return body[:handoff.start()].strip() if handoff else body.strip()


def _latest_handoff(body: str, now: datetime) -> tuple[date, str] | None:
    headings = list(_SECTION_RE.finditer(body))
    matches = list(_HANDOFF_RE.finditer(body))
    candidates: list[tuple[date, str]] = []
    current_date = now.date()
    for match in matches:
        try:
            handoff_date = date.fromisoformat(match.group(2))
        except ValueError:
            continue
        if handoff_date > current_date:
            continue
        end = _section_end(body, match, headings)
        text = body[match.start():end].strip()
        # A dated heading with only nested headings or whitespace is not a
        # useful candidate; let an older substantive handoff win instead.
        body_text = body[match.end():end].strip()
        if _has_substantive_text(body_text):
            candidates.append((handoff_date, text))
    return max(candidates, key=lambda item: item[0]) if candidates else None


def _handoff_section_priority(title: str) -> int:
    normalized = re.sub(r"[^a-z0-9а-я]+", " ", title.casefold()).strip()
    if any(token in normalized for token in (
        "next step", "next", "wip", "in progress", "unresolved", "следующий шаг", "дальше", "в работе", "незаверш",
    )):
        return 0
    if any(token in normalized for token in ("shipped", "done", "completed", "готово", "сделано")):
        return 2
    return 1


def _compact_handoff(text: str, char_limit: int) -> str:
    """Preserve actionable handoff sections before long shipped/history text."""
    if len(text) <= char_limit:
        return text
    first_line, separator, _remainder = text.partition("\n")
    if not separator:
        return text[:char_limit]
    title = first_line.strip()
    headings = list(_SECTION_RE.finditer(text))
    nested = [heading for heading in headings if heading.start() > 0]
    if not nested:
        return (title + "\n" + text[len(first_line) + 1:][:max(0, char_limit - len(title) - 1)]).strip()

    sections: list[tuple[int, int, str]] = []
    for heading in nested:
        end = _section_end(text, heading, headings)
        if not _has_substantive_text(text[heading.end():end]):
            continue
        sections.append((
            _handoff_section_priority(heading.group(2)),
            heading.start(),
            text[heading.start():end].strip(),
        ))

    first_nested = min(heading.start() for heading in nested)
    intro = text[len(first_line) + 1:first_nested].strip()
    ordered: list[tuple[int, int, str]] = []
    if _has_substantive_text(intro):
        ordered.append((1, len(first_line), intro))
    ordered.extend(sections)
    ordered.sort(key=lambda item: (item[0], item[1]))

    output = title
    remaining = max(0, char_limit - len(title) - 1)
    for _priority, _position, section in ordered:
        if remaining <= 0:
            break
        if len(section) <= remaining:
            output += "\n" + section
            remaining -= len(section) + 1
            continue
        section_head, section_sep, section_body = section.partition("\n")
        if section_sep and remaining > len(section_head) + 1:
            fragment = section_head + "\n" + section_body[:remaining - len(section_head) - 1].rstrip()
        else:
            fragment = section_body[:remaining].strip() if section_sep else section[:remaining].strip()
        if fragment:
            output += "\n" + fragment
        break
    return output[:char_limit].rstrip()


def _add_block(
    blocks: list[ContextBlock], text: str | None, category: str, priority: int,
    remaining: int, block_id: str, max_chars: int | None = None,
) -> int:
    if not text or remaining <= 0:
        return remaining
    text = text.strip()
    if not text:
        return remaining
    available = min(remaining, max_chars) if max_chars is not None else remaining
    if len(text) > available:
        text = text[:available].rstrip()
        first_line, separator, body = text.partition("\n")
        if separator and not body.strip():
            # If the cap lands on a heading, prefer substantive content over
            # returning a header that claims to be useful context by itself.
            text = body[:available].strip()
    if text:
        blocks.append(ContextBlock(block_id, text, priority, category))
        return remaining - len(text)
    return remaining


def build_bootstrap(vault: Path, context: ScopeContext, *, char_limit: int, now: datetime) -> list[ContextBlock]:
    """Build a bounded snapshot without creating or changing any vault files."""
    root = Path(vault)
    diagnostics = list(context.diagnostics)
    if char_limit <= 0:
        return []
    if not root.is_dir():
        return [ContextBlock("bootstrap-diagnostic", "Project context is unavailable."[:char_limit], 0, "bootstrap")]
    blocks: list[ContextBlock] = []
    remaining = char_limit
    critical = _safe_read(root / "CRITICAL_FACTS.md", root, diagnostics)
    if critical is None:
        critical = _safe_read(root / "wiki" / "CRITICAL_FACTS.md", root, diagnostics)
    parsed = _valid_content(critical, now, require_global=True, diagnostics=diagnostics) if critical is not None else None
    critical_budget = None if context.scope is None or context.global_only else max(256, int(char_limit * 0.25))
    remaining = _add_block(
        blocks, parsed[0] if parsed else None, "bootstrap", 100, remaining,
        "critical-facts", max_chars=critical_budget,
    )

    if context.scope is None or context.global_only:
        for diagnostic in diagnostics:
            remaining = _add_block(blocks, f"Diagnostic: {diagnostic}", "bootstrap", 0, remaining, f"diagnostic-{len(blocks)}")
        return blocks

    scope = context.scope
    project_path = root / scope / f"{scope}.md"
    legacy_path = root / "projects" / f"{scope}.md"
    project_file = project_path if project_path.is_file() else legacy_path
    project_raw = _safe_read(project_file, root, diagnostics)
    project_parsed = _valid_content(project_raw, now, expected_scope=scope, diagnostics=diagnostics) if project_raw is not None else None
    project_body = project_parsed[0] if project_parsed else None
    if project_body:
        intent = _extract_intent(project_body)
        remaining = _add_block(
            blocks, intent, "bootstrap", 90, remaining, "project-intent",
            max_chars=max(256, int(char_limit * 0.4)),
        )

    handoff_candidates: list[tuple[date, str]] = []
    if project_body:
        card_handoff = _latest_handoff(project_body, now)
        if card_handoff is not None:
            handoff_candidates.append(card_handoff)
    handoff_dir = root / scope / "handoffs"
    if handoff_dir.is_dir():
        file_candidates: list[tuple[date, Path]] = []
        for path in handoff_dir.glob("*.md"):
            match = re.match(r"^(\d{4}-\d{2}-\d{2})", path.stem)
            try:
                candidate_date = date.fromisoformat(match.group(1)) if match else date.min
            except ValueError:
                candidate_date = date.min
            if candidate_date <= now.date():
                file_candidates.append((candidate_date, path))
        for file_date, path in sorted(file_candidates, key=lambda item: item[0], reverse=True):
            raw = _safe_read(path, root, diagnostics)
            if raw is None:
                continue
            parsed_handoff = _valid_content(raw, now, expected_scope=scope, diagnostics=diagnostics)
            if parsed_handoff is None:
                continue
            body, _note = parsed_handoff
            dated_section = _latest_handoff(body, now)
            if dated_section is not None:
                candidate_date, text = dated_section
            else:
                if _HANDOFF_RE.search(body):
                    # A file may have a current-looking name but contain only
                    # future-dated handoff sections; do not leak their text.
                    continue
                candidate_date, text = file_date, body
            if candidate_date <= now.date() and _has_substantive_text(text):
                handoff_candidates.append((candidate_date, text))
    if handoff_candidates:
        _handoff_date, handoff_text = max(handoff_candidates, key=lambda item: item[0])
        handoff_budget = min(remaining, max(256, int(char_limit * 0.25)))
        handoff_text = _compact_handoff(handoff_text, handoff_budget)
        remaining = _add_block(
            blocks, handoff_text, "bootstrap", 80, remaining, "latest-handoff",
        )

    # Read only explicit same-scope design-principles paths and at most two
    # unambiguous same-scope links from the project card; never traverse the graph.
    principle_candidates = [
        root / scope / "design-principles.md",
        root / scope / "decisions" / "design-principles.md",
        root / scope / "patterns" / "design-principles.md",
    ]
    if project_body:
        for link in extract_wikilinks(project_body):
            target = link["target"].replace("\\", "/")
            if target.startswith(f"{scope}/") and target.lower().endswith("design-principles"):
                rel = Path(target)
                if not rel.suffix:
                    rel = rel.with_suffix(".md")
                principle_candidates.append(root / rel)
    seen: set[Path] = set()
    principles_added = 0
    for candidate in principle_candidates:
        try:
            canonical = candidate.resolve(strict=False)
        except OSError:
            continue
        if canonical in seen or not _is_within(canonical, (root / scope)):
            continue
        seen.add(canonical)
        raw = _safe_read(candidate, root, diagnostics)
        parsed_principle = _valid_content(raw, now, expected_scope=scope, diagnostics=diagnostics) if raw is not None else None
        if parsed_principle is None:
            continue
        remaining = _add_block(blocks, parsed_principle[0], "bootstrap", 75 - principles_added, remaining, f"design-principles-{principles_added + 1}")
        principles_added += 1
        if principles_added == 2 or remaining <= 0:
            break

    decisions_dir = root / scope / "decisions"
    decisions: list[tuple[float, Path, str]] = []
    if decisions_dir.is_dir():
        for path in sorted(decisions_dir.glob("*.md"), key=lambda p: p.name, reverse=True):
            if path.stem.lower() == "design-principles":
                continue
            raw = _safe_read(path, root, diagnostics)
            valid = _valid_content(raw, now, expected_scope=scope, diagnostics=diagnostics) if raw is not None else None
            if valid:
                decisions.append((path.stat().st_mtime, path, valid[0]))
    for index, (_, path, body) in enumerate(sorted(decisions, reverse=True)[:2]):
        remaining = _add_block(blocks, body, "bootstrap", 65 - index, remaining, f"decision-{path.stem}")

    for diagnostic in diagnostics:
        if remaining < 20:
            break
        remaining = _add_block(blocks, f"Diagnostic: {diagnostic}", "bootstrap", 0, remaining, f"diagnostic-{len(blocks)}")
    return blocks
