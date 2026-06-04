import re
from typing import Any

import frontmatter

WIKILINK_RE = re.compile(r"\[\[([^\]]+)\]\]")
HEADING_RE = re.compile(r"^#\s+(.+)$", re.MULTILINE)

_FENCE_CHARS = "`~"


def _fence_marker(stripped: str) -> tuple[str, int] | None:
    """If `stripped` (a leading-whitespace-stripped line) opens/closes a code
    fence, return (fence_char, run_length); else None. A fence is a run of >=3
    of the same char in {`, ~} at the start of the line."""
    if not stripped:
        return None
    ch = stripped[0]
    if ch not in _FENCE_CHARS:
        return None
    n = len(stripped) - len(stripped.lstrip(ch))
    return (ch, n) if n >= 3 else None


def _mask_inline_code(s: str) -> str:
    """Replace inline-code spans in a single line with spaces (length-preserving).

    A backtick run of length N opens a span that closes at the next run of
    EXACTLY N backticks (CommonMark). An unterminated run is literal (left as-is).
    """
    chars = list(s)
    n = len(s)
    i = 0
    while i < n:
        if s[i] != "`":
            i += 1
            continue
        j = i
        while j < n and s[j] == "`":
            j += 1
        run = j - i
        # Search for a closing run of exactly `run` backticks.
        k = j
        closed_at = -1
        while k < n:
            if s[k] == "`":
                m = k
                while m < n and s[m] == "`":
                    m += 1
                if m - k == run:
                    closed_at = m
                    break
                k = m
            else:
                k += 1
        if closed_at == -1:
            # Unterminated: backticks are literal. Skip past the opening run.
            i = j
            continue
        for x in range(i, closed_at):
            chars[x] = " "
        i = closed_at
    return "".join(chars)


def _mask_code_regions(text: str) -> str:
    """Return `text` with every char inside a code region replaced by a space,
    preserving length, line structure, and byte offsets so match positions still
    index into the original text.

    Masked regions: fenced code blocks (``` / ~~~, >=3 chars, info-string allowed
    on the opening fence) and inline-code spans (`...`). Used to keep wiki-link
    extraction from seeing [[...]] tokens that are only code examples.
    """
    out: list[str] = []
    in_fence = False
    fence_char = ""
    fence_len = 0
    segments = text.split("\n")
    for seg in segments:
        cr = ""
        content = seg
        if content.endswith("\r"):
            cr = "\r"
            content = content[:-1]
        stripped = content.lstrip()
        marker = _fence_marker(stripped)
        if in_fence:
            # A closing fence: same char, run >= opening, nothing but whitespace after.
            if (
                marker
                and marker[0] == fence_char
                and marker[1] >= fence_len
                and stripped[marker[1]:].strip() == ""
            ):
                in_fence = False
            out.append(" " * len(content) + cr)
            continue
        if marker:
            in_fence = True
            fence_char, fence_len = marker
            out.append(" " * len(content) + cr)
            continue
        out.append(_mask_inline_code(content) + cr)
    return "\n".join(out)


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

    Wiki-links inside inline-code spans (`...`) and fenced code blocks (``` / ~~~)
    are skipped, so documenting [[...]] syntax in a code example neither creates
    nor validates a link.
    """
    seen_raw: set[str] = set()
    result: list[dict] = []
    # Mask code regions before scanning. The mask is length-preserving, so match
    # offsets still index into `text` — slice `raw` from the original, not the mask.
    scan_text = _mask_code_regions(text)
    for match in WIKILINK_RE.finditer(scan_text):
        raw = text[match.start(1):match.end(1)]
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
