"""Codex scope discovery delegated to the canonical resolver helpers."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Sequence

from symbiosis_brain.adapters.codex.config import TrustedProject
from symbiosis_brain.adapters.codex.secure_io import secure_read_text
from symbiosis_brain.scope_resolver import normalize_scope, parse_marker


@dataclass(frozen=True)
class ScopeResolution:
    """Informational scope result consumed by the Codex runtime.

    The compatibility flags remain for the copied adapter/tests while the
    runtime migration lands. They describe workspace availability only and are
    not an adapter-owned authorization policy.
    """

    scope: str | None
    source: str
    marker_path: Path | None
    depth: int | None
    warning: str | None
    trusted: bool = True
    trusted_root: Path | None = None
    allow_global: bool = True
    memory_authorized: bool = True


def safe_vault_path(vault: Path, *parts: str) -> Path | None:
    """Return a physical vault-contained path, or ``None`` on any escape."""
    try:
        root = Path(vault).resolve(strict=True)
    except (OSError, RuntimeError):
        return None
    if not root.is_dir():
        return None

    candidate = root
    for raw in parts:
        if not isinstance(raw, str) or not raw or "\x00" in raw:
            return None
        part = Path(raw)
        posix = PurePosixPath(raw)
        windows = PureWindowsPath(raw)
        if (
            part.is_absolute()
            or part.drive
            or posix.is_absolute()
            or windows.is_absolute()
            or windows.drive
            or ".." in posix.parts
            or ".." in windows.parts
        ):
            return None
        candidate = candidate / part
    try:
        physical = candidate.resolve(strict=False)
        physical.relative_to(root)
    except (OSError, RuntimeError, ValueError):
        return None
    return physical


def _unavailable() -> ScopeResolution:
    return ScopeResolution(
        scope=None,
        source="none",
        marker_path=None,
        depth=None,
        warning="workspace unavailable",
        trusted=False,
        allow_global=True,
        memory_authorized=False,
    )


def _taxonomy_marks_broad(vault: Path | None, scope: str) -> bool:
    """Match the canonical hook's ancestor-only broad-scope classification.

    Missing or unreadable taxonomy fails open: taxonomy may narrow inherited
    context, but must not make a known marker disappear on a fresh install.
    """
    if vault is None:
        return False
    try:
        text = secure_read_text(vault, "reference/scope-taxonomy.md")
    except (OSError, UnicodeError, ValueError):
        return False

    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.startswith("|") or "---" in stripped:
            continue
        cells = [cell.strip() for cell in stripped.strip("|").split("|")]
        if len(cells) < 2 or cells[0].strip("`") != scope:
            continue
        normalized = re.sub(r"[*_]", "", cells[1]).strip().strip("`").casefold()
        kind = re.split(r"[\s(]", normalized, maxsplit=1)[0]
        return kind in {"workspace-root", "umbrella", "зонтик"}
    return False


def resolve_scope(
    cwd: Path,
    vault: Path | None = None,
    trusted_projects: Sequence[TrustedProject] = (),
    *,
    max_depth: int = 10,
) -> ScopeResolution:
    """Resolve the nearest canonical marker, else the project basename.

    ``trusted_projects`` is accepted only for API compatibility with the copied
    Codex adapter. Taxonomy is consulted solely for the existing cross-client
    rule that workspace-root/umbrella markers are not inherited by descendants.
    """
    del trusted_projects
    if type(max_depth) is not int or max_depth < 0:
        raise ValueError("max_depth must be a non-negative integer")
    try:
        physical_cwd = Path(cwd).resolve(strict=True)
    except (OSError, RuntimeError):
        return _unavailable()
    if not physical_cwd.is_dir():
        return _unavailable()

    current = physical_cwd
    for depth in range(max_depth + 1):
        marker_path = current / "CLAUDE.md"
        if os.path.lexists(marker_path):
            if not marker_path.is_file():
                return ScopeResolution(
                    scope=normalize_scope(current.name) or None,
                    source="basename",
                    marker_path=marker_path.resolve(strict=False),
                    depth=depth,
                    warning="scope marker unavailable",
                )
            marker = parse_marker(marker_path)
            if marker is None:
                scope = normalize_scope(current.name) or None
                return ScopeResolution(
                    scope=scope,
                    source="basename" if scope else "none",
                    marker_path=marker_path.resolve(strict=False),
                    depth=depth,
                    warning=None if scope else "scope unresolved",
                )
            source = "marker_v1" if marker.version == 1 else "marker_future"
            if depth > 0 and _taxonomy_marks_broad(vault, marker.scope):
                return ScopeResolution(
                    scope=None,
                    source=source,
                    marker_path=marker_path.resolve(strict=False),
                    depth=depth,
                    warning="broad ancestor scope rejected",
                )
            return ScopeResolution(
                scope=marker.scope,
                source=source,
                marker_path=marker_path.resolve(strict=False),
                depth=depth,
                warning=None,
            )
        parent = current.parent
        if parent == current:
            break
        current = parent

    scope = normalize_scope(physical_cwd.name) or None
    return ScopeResolution(
        scope=scope,
        source="basename" if scope else "none",
        marker_path=None,
        depth=0 if scope else None,
        warning=None if scope else "scope unresolved",
    )
