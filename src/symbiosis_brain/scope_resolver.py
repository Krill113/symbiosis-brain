"""Scope detection helpers — normalize, parse marker, detect."""
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

_SEPARATOR_RE = re.compile(r"[._\s]+")
_CAMEL_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")
_NON_ALNUM_DASH = re.compile(r"[^a-z0-9-]")
_MULTI_DASH = re.compile(r"-{2,}")

_MARKER_RE = re.compile(
    r"<!--\s*symbiosis-brain\s+v(?P<version>\d+)\s*:\s*"
    r"(?P<body>[^>]*?)\s*-->"
)


@dataclass(frozen=True)
class Marker:
    version: int
    scope: str
    umbrella: Optional[str] = None
    status: Optional[str] = None


def _parse_marker_body(body: str) -> dict:
    """Parse `key=value, key=value` pairs from a marker body. Whitespace-tolerant."""
    out: dict = {}
    for part in body.split(","):
        if "=" not in part:
            continue
        k, _, v = part.partition("=")
        out[k.strip()] = v.strip()
    return out


def parse_marker(claude_md_path) -> Optional[Marker]:
    """Read <project>/CLAUDE.md and extract a `<!-- symbiosis-brain v1: ... -->` marker.

    Returns None if: file missing, no marker, marker corrupt (no scope=).
    If multiple markers are present, returns the last one (recovery after migration).
    """
    p = Path(claude_md_path)
    if not p.is_file():
        return None
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    matches = list(_MARKER_RE.finditer(text))
    if not matches:
        return None
    last = matches[-1]
    fields = _parse_marker_body(last.group("body"))
    scope = fields.get("scope")
    if not scope:
        return None
    return Marker(
        version=int(last.group("version")),
        scope=scope,
        umbrella=fields.get("umbrella"),
        status=fields.get("status"),
    )


def normalize_scope(raw: str) -> str:
    """Normalize folder name to kebab-case scope identifier.

    Steps:
      1. Insert dashes between camelCase boundaries (FooBar → Foo-Bar).
      2. Lowercase.
      3. Replace dots/underscores/whitespace with dashes.
      4. Strip non-[a-z0-9-] characters.
      5. Collapse multiple dashes, strip leading/trailing dashes.
    """
    if not raw:
        return ""
    step1 = _CAMEL_RE.sub("-", raw)
    step2 = step1.lower()
    step3 = _SEPARATOR_RE.sub("-", step2)
    step4 = _NON_ALNUM_DASH.sub("", step3)
    step5 = _MULTI_DASH.sub("-", step4).strip("-")
    return step5
