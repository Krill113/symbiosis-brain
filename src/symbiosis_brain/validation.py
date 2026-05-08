"""Write-time validation gates for brain_write / brain_append / brain_patch.

Two classes of rule:
- hard-block: raise ValidationError, file is NOT written
- soft-warn:  return a Warning_ entry in the warnings list, file IS written

Hard-block list (structural breakage):
- missing_gist: frontmatter has no gist field
- malformed_frontmatter: frontmatter is not a dict (None, list, scalar, etc.)
- broken_outgoing_ref: any [[X]] in body resolves broken (excludes [[forward:X]])

Soft-warn list (stylistic):
- gist_too_long: gist >100 chars
- few_wiki_links: <2 outgoing wiki-links

Type↔folder mismatch is NOT enforced here — it's already enforced by lint.py
and the user can override via `allow_type_mismatch: true` in frontmatter.
We may surface it as a soft-warn in a future iteration.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from symbiosis_brain.markdown_parser import extract_wikilinks

if TYPE_CHECKING:
    from symbiosis_brain.storage import Storage


GIST_SOFT_LIMIT = 100  # chars; lint reports >100 as `gist_too_long`
MIN_WIKILINKS = 2
FORWARD_REF_PREFIX = "forward:"


class ValidationError(Exception):
    """Raised when a hard-block rule fires. Caller (server.py) maps this to an
    error response that does NOT touch the filesystem."""


@dataclass(frozen=True)
class Warning_:
    """A soft warning produced during validation. Attached to the success
    response as a human-readable line, but does not block the write."""
    rule: str
    message: str


def _check_hard_blocks(
    path: str,
    body: str,
    frontmatter: dict,
    storage: Storage,
) -> None:
    """Raise ValidationError if any hard-block rule fires. Returns None on success."""
    if not isinstance(frontmatter, dict):
        raise ValidationError(
            f"frontmatter must be a dict, got {type(frontmatter).__name__}"
        )

    gist_value = frontmatter.get("gist") or ""
    if not gist_value.strip():
        raise ValidationError(
            "gist field is required (1-line summary, ≤100 chars). "
            "Add gist='...' to the brain_write call."
        )

    # Resolve every outgoing wiki-link; collect broken ones (skipping forward-refs).
    from symbiosis_brain.resolver import resolve_target
    broken: list[str] = []
    for link in extract_wikilinks(body):
        target = link["target"]
        if target.startswith(FORWARD_REF_PREFIX):
            continue
        _canonical, is_broken = resolve_target(target, storage)
        if is_broken:
            broken.append(link["raw"])
    if broken:
        sample = ", ".join(f"[[{b}]]" for b in broken[:3])
        more = f" (+{len(broken) - 3} more)" if len(broken) > 3 else ""
        raise ValidationError(
            f"{len(broken)} broken outgoing wiki-link(s): {sample}{more}. "
            f"Either create the target first, or use [[forward:X]] for "
            f"explicit forward-refs."
        )


def _check_soft_warns(
    body: str,
    frontmatter: dict,
) -> list[Warning_]:
    """Return list of soft warnings. Empty list means no warnings."""
    warnings: list[Warning_] = []

    gist = frontmatter.get("gist") or ""
    if len(gist) > GIST_SOFT_LIMIT:
        warnings.append(
            Warning_(
                rule="gist_too_long",
                message=f"gist {len(gist)} chars (rec ≤{GIST_SOFT_LIMIT})",
            )
        )

    link_count = len(extract_wikilinks(body))
    if link_count < MIN_WIKILINKS:
        warnings.append(
            Warning_(
                rule="few_wiki_links",
                message=f"{link_count} wiki-link(s) — notes with <{MIN_WIKILINKS} "
                        f"links risk becoming orphaned",
            )
        )

    return warnings


def new_links_introduced(old_body: str, new_body: str) -> bool:
    """True iff new_body contains any [[wiki-link]] target that old_body did not."""
    old_targets = {l["target"] for l in extract_wikilinks(old_body)}
    new_targets = {l["target"] for l in extract_wikilinks(new_body)}
    return bool(new_targets - old_targets)


def validate_note(
    *,
    path: str,
    title: str,
    body: str,
    frontmatter: dict,
    storage: Storage,
) -> list[Warning_]:
    """Run all validation rules. Raises ValidationError on any hard-block
    failure. Returns a list of Warning_ for soft-warn rules.

    Caller is responsible for:
    - parsing frontmatter from raw markdown (validate_note expects a dict)
    - rendering warnings into the tool response

    The `path` and `title` parameters are unused today but reserved for
    future rules (e.g. type↔folder consistency) that need them.
    """
    _check_hard_blocks(path=path, body=body, frontmatter=frontmatter, storage=storage)
    return _check_soft_warns(body=body, frontmatter=frontmatter)
