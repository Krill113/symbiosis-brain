import re
from typing import TypedDict

from symbiosis_brain.markdown_parser import _mask_code_regions

# Matches any ATX heading h1-h6 (`#`..`######`). Single capture group = the
# heading text. SC1: append targets any level (not just h2).
SECTION_HEADING_RE = re.compile(r"^#{1,6}\s+(.+?)\s*$", re.MULTILINE)


def _heading_matches(body: str) -> list[re.Match]:
    """Return heading matches OUTSIDE fenced code blocks, in document order.

    Fence detection reuses markdown_parser._mask_code_regions (length-preserving),
    so a `#`-prefixed line inside a ``` / ~~~ block (e.g. a shell comment) is NOT
    mistaken for a section heading. Match offsets index into the ORIGINAL body;
    slice heading names from the original too (a heading line may contain inline
    code that the mask blanked)."""
    masked = _mask_code_regions(body)
    return list(SECTION_HEADING_RE.finditer(masked))


class SplitResult(TypedDict):
    preamble: str
    sections: dict[str, str]


def split_sections(body: str) -> SplitResult:
    """Split a markdown body into a preamble and an ordered dict of heading sections.

    A section is any ATX heading (`#`..`######`) that is NOT inside a fenced code
    block. Section content includes its heading line and everything up to (but not
    including) the next heading of any level or EOF. Preamble is everything before
    the first heading.

    Cross-level name collisions (`# Foo` and `## Foo`) collapse to one dict key
    (last-wins) in this view — `append_to_section` handles them safely (refuses
    rather than corrupts; see there).
    """
    matches = _heading_matches(body)
    if not matches:
        return {"preamble": body, "sections": {}}

    preamble = body[: matches[0].start()]
    sections: dict[str, str] = {}
    for i, m in enumerate(matches):
        name = body[m.start(1):m.end(1)].strip()
        start = m.start()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(body)
        sections[name] = body[start:end]
    return {"preamble": preamble, "sections": sections}


class SectionNotFoundError(LookupError):
    """Raised when a named section is not found in the body."""


class SectionAmbiguousError(LookupError):
    """Raised when the target section name matches more than one heading
    (e.g. a `# Foo` title and a `## Foo` subsection). Refusing to append is
    safer than guessing — appending to the wrong one is silent data loss."""


def append_to_section(
    body: str,
    section: str,
    content: str,
    *,
    create_if_missing: bool = False,
) -> str:
    """Append `content` to the end of section `<section>` in `body`, returning new body.

    `<section>` matches a heading of any level (h1-h6) by its text.

    Whitespace normalization: the section's trailing blank lines are collapsed to zero,
    then `content` is added with exactly one `\\n` between existing content and the
    new content, and exactly one trailing `\\n`. Section boundaries (heading line and
    spacing before the next heading) are preserved.

    Line endings are normalized to `\\n` for matching; the original line-ending style
    of `body` is restored on return. `content` is normalized the same way before insertion.

    Case-sensitive on `section`. If the section is missing:
      - `create_if_missing=False` (default): raise `SectionNotFoundError` with the
        list of available section names.
      - `create_if_missing=True`: append a new `## <section>` at the end of the body.
    """
    original_uses_crlf = "\r\n" in body
    normalized_body = body.replace("\r\n", "\n")
    normalized_content = content.replace("\r\n", "\n")

    # Work from the ordered list of heading matches (not the dict view), so a
    # rebuild never drops or reorders same-named sections.
    matches = _heading_matches(normalized_body)
    names = [normalized_body[m.start(1):m.end(1)].strip() for m in matches]
    target_idxs = [i for i, n in enumerate(names) if n == section]

    if not target_idxs:
        if create_if_missing:
            # Newly created sections are always h2 (the conventional default),
            # independent of the heading levels already present in the note.
            new_section = f"## {section}\n\n{normalized_content.rstrip()}\n"
            result = normalized_body
            if result and not result.endswith("\n"):
                result += "\n"
            if result and not result.endswith("\n\n"):
                result += "\n"
            result = result + new_section
            if original_uses_crlf:
                result = result.replace("\n", "\r\n")
            return result
        available = ", ".join(f"'{s}'" for s in dict.fromkeys(names)) or "(none)"
        raise SectionNotFoundError(
            f"Section '{section}' not found. Available sections: {available}."
        )

    if len(target_idxs) > 1:
        raise SectionAmbiguousError(
            f"Section '{section}' matches {len(target_idxs)} headings — "
            f"rename one or target a unique section name."
        )

    # Rebuild from the ordered segments, modifying only the target.
    ti = target_idxs[0]
    preamble = normalized_body[: matches[0].start()]
    segments: list[str] = []
    for i, m in enumerate(matches):
        start = m.start()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(normalized_body)
        segments.append(normalized_body[start:end])

    chunk = segments[ti]
    trailing_newlines = len(chunk) - len(chunk.rstrip("\n"))
    content_stripped = normalized_content.rstrip("\n")
    chunk_content = chunk.rstrip("\n")
    segments[ti] = f"{chunk_content}\n{content_stripped}\n" + "\n" * (trailing_newlines - 1)

    rebuilt = preamble + "".join(segments)
    result = rebuilt.rstrip("\n") + "\n"
    if original_uses_crlf:
        result = result.replace("\n", "\r\n")
    return result


class AnchorNotFoundError(LookupError):
    """Raised when the anchor substring is not found."""


class AnchorAmbiguousError(LookupError):
    """Raised when the anchor substring occurs more than once."""


def replace_anchor(body: str, anchor: str, replacement: str) -> str:
    """Replace the unique occurrence of `anchor` in `body` with `replacement`.

    Line endings are normalized to `\\n` for matching; the original line-ending style
    of `body` is restored on return. Raises `AnchorNotFoundError` if the anchor does
    not appear, `AnchorAmbiguousError` if it appears more than once.
    """
    original_uses_crlf = "\r\n" in body
    normalized_body = body.replace("\r\n", "\n")
    normalized_anchor = anchor.replace("\r\n", "\n")

    count = normalized_body.count(normalized_anchor)
    if count == 0:
        raise AnchorNotFoundError(f"Anchor not found: {anchor!r}")
    if count > 1:
        raise AnchorAmbiguousError(
            f"Anchor matches {count} locations. Extend the anchor to make it unique."
        )

    new_body = normalized_body.replace(normalized_anchor, replacement, 1)
    if original_uses_crlf:
        new_body = new_body.replace("\n", "\r\n")
    return new_body
