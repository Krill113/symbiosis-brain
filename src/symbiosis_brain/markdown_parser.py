import re
from typing import Any

import frontmatter

WIKILINK_RE = re.compile(r"\[\[([^\]]+)\]\]")
HEADING_RE = re.compile(r"^#\s+(.+)$", re.MULTILINE)


def parse_note(content: str) -> dict[str, Any]:
    """Parse markdown with optional YAML frontmatter into structured dict."""
    post = frontmatter.loads(content)
    meta = dict(post.metadata)
    body = post.content

    title = meta.pop("title", None)
    if not title:
        match = HEADING_RE.search(body)
        title = match.group(1).strip() if match else "Untitled"

    return {
        "title": title,
        "body": body,
        "type": meta.pop("type", "wiki"),
        "scope": meta.pop("scope", "global"),
        "tags": meta.pop("tags", []),
        "valid_from": meta.pop("valid_from", None),
        "valid_to": meta.pop("valid_to", None),
        "created_at": meta.pop("created_at", None),
        "extra": meta,
    }


def extract_wikilinks(text: str) -> list[dict]:
    """Extract wiki-links from text.

    Returns list of dicts with keys:
        raw    — original text between [[ ]], preserving escapes
        target — left side of |, unescaped, stripped; canonical lookup key
        alias  — right side of |, stripped, or None if no pipe

    Deduplicates by `raw`, preserves order of first occurrence.
    Empty/whitespace-only links are skipped.
    """
    seen_raw: set[str] = set()
    result: list[dict] = []
    for match in WIKILINK_RE.finditer(text):
        raw = match.group(1)
        if raw in seen_raw:
            continue
        seen_raw.add(raw)

        unescaped = raw.replace(r"\|", "|")
        parts = unescaped.split("|", 1)
        target = parts[0].strip()
        alias = parts[1].strip() if len(parts) == 2 else None

        if not target:
            continue

        result.append({"raw": raw, "target": target, "alias": alias})
    return result


def render_note(title: str, body: str, note_type: str = "wiki", scope: str = "global",
                tags: list[str] | None = None, extra_frontmatter: dict | None = None) -> str:
    """Render a note dict back to markdown with YAML frontmatter."""
    meta: dict[str, Any] = {
        "title": title,
        "type": note_type,
        "scope": scope,
    }
    if tags:
        meta["tags"] = tags
    if extra_frontmatter:
        meta.update(extra_frontmatter)

    post = frontmatter.Post(body, **meta)
    return frontmatter.dumps(post) + "\n"
