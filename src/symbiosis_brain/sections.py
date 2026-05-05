import re
from typing import TypedDict

SECTION_HEADING_RE = re.compile(r"^##\s+(.+?)\s*$", re.MULTILINE)


class SplitResult(TypedDict):
    preamble: str
    sections: dict[str, str]


def split_sections(body: str) -> SplitResult:
    """Split a markdown body into a preamble and an ordered dict of `## heading` sections.

    Section content includes its heading line and everything up to (but not including)
    the next `## ` heading or EOF. Preamble is everything before the first `## ` heading.

    Known limitation (v1): `## ` lines inside fenced code blocks are treated as headings.
    Accepted because our vault does not embed such content today.
    """
    matches = list(SECTION_HEADING_RE.finditer(body))
    if not matches:
        return {"preamble": body, "sections": {}}

    preamble = body[: matches[0].start()]
    sections: dict[str, str] = {}
    for i, m in enumerate(matches):
        name = m.group(1).strip()
        start = m.start()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(body)
        sections[name] = body[start:end]
    return {"preamble": preamble, "sections": sections}


class SectionNotFoundError(LookupError):
    """Raised when a named section is not found in the body."""


def append_to_section(
    body: str,
    section: str,
    content: str,
    *,
    create_if_missing: bool = False,
) -> str:
    """Append `content` to the end of `## <section>` in `body`, returning new body.

    Whitespace normalization: the section's trailing blank lines are collapsed to zero,
    then `content` is added with exactly one `\\n` between existing content and the
    new content, and exactly one trailing `\\n`. Section boundaries (heading line and
    spacing before the next `## `) are preserved.

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

    split = split_sections(normalized_body)
    sections = split["sections"]

    if section not in sections:
        if create_if_missing:
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
        available = ", ".join(f"'{s}'" for s in sections) or "(none)"
        raise SectionNotFoundError(
            f"Section '## {section}' not found. Available sections: {available}."
        )

    # Rebuild the body, modifying only the target section.
    new_sections: list[str] = []
    for name, chunk in sections.items():
        if name == section:
            trailing_newlines = len(chunk) - len(chunk.rstrip("\n"))
            content_stripped = normalized_content.rstrip("\n")
            chunk_content = chunk.rstrip("\n")
            new_chunk = f"{chunk_content}\n{content_stripped}\n" + "\n" * (trailing_newlines - 1)
            new_sections.append(new_chunk)
        else:
            new_sections.append(chunk)

    rebuilt = split["preamble"] + "".join(new_sections)
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
