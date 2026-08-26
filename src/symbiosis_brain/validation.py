"""Write-time validation gates for brain_write / brain_append / brain_patch.

Two classes of rule:
- hard-block: raise ValidationError, file is NOT written
- soft-warn:  return a Warning_ entry in the warnings list, file IS written

Hard-block list (structural breakage):
- missing_gist: frontmatter has no gist field
- malformed_frontmatter: frontmatter is not a dict (None, list, scalar, etc.)
- gist_over_hard_limit: gist >140 chars
- broken_outgoing_ref: any [[X]] in body resolves broken (excludes external refs:
  [[forward:X]] always, plus [[ns:X]] whose ns is not a taxonomy scope)

Soft-warn list (stylistic):
- gist_too_long: gist >100 chars (recommended ceiling)
- few_wiki_links: <2 outgoing wiki-links
- gist_missing: no gist AND require_gist=False (brain_append / brain_patch have no
  `gist` parameter — grandfathering, decision 2026-08-25 / triage B2)

Type↔folder mismatch is NOT enforced here — it's already enforced by lint.py
and the user can override via `allow_type_mismatch: true` in frontmatter.
We may surface it as a soft-warn in a future iteration.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from symbiosis_brain.markdown_parser import extract_wikilinks

if TYPE_CHECKING:
    from pathlib import Path

    from symbiosis_brain.storage import Storage


# Re-exported (not re-declared) so existing importers of
# `validation.GIST_SOFT_LIMIT` keep working — the numbers themselves live in
# gist_limits, the single source of truth (B-N6).
from symbiosis_brain.gist_limits import GIST_HARD_LIMIT, GIST_SOFT_LIMIT  # noqa: F401
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


def _gist_text(frontmatter: dict) -> str:
    """Frontmatter gist as text. YAML hands us a datetime.date for `gist: 2026-08-25`
    and an int for `gist: 42` — str() first, so .strip()/len() never blow up with
    AttributeError instead of a readable tool error (triage B-N1)."""
    return str(frontmatter.get("gist") or "")


def _valid_scopes_for(vault_path: Path | None) -> frozenset[str] | None:
    """Scope whitelist for is_external_ref, or None when it is unavailable.

    Never raises: a missing or malformed taxonomy must not block a write. None means
    "only forward: is external" — i.e. the pre-B3 behaviour.
    """
    if vault_path is None:
        return None
    try:
        from symbiosis_brain.taxonomy import load_valid_scopes_cached
        return load_valid_scopes_cached(vault_path)
    except Exception:
        return None


def _check_hard_blocks(
    path: str,
    body: str,
    frontmatter: dict,
    storage: Storage,
    *,
    valid_scopes: frozenset[str] | None,
    require_gist: bool,
    tool_name: str,
) -> list[Warning_]:
    """Raise ValidationError if any hard-block rule fires.

    Returns the soft warnings produced by a downgraded gate — today only the
    missing-gist gate on the brain_append / brain_patch path.
    """
    if not isinstance(frontmatter, dict):
        raise ValidationError(
            f"frontmatter must be a dict, got {type(frontmatter).__name__}"
        )

    warnings: list[Warning_] = []

    gist_value = _gist_text(frontmatter)
    if not gist_value.strip():
        if require_gist:
            raise ValidationError(
                f"gist field is required (1-line summary, ≤{GIST_SOFT_LIMIT} chars). "
                f"Add gist='...' to the {tool_name} call."
            )
        # brain_append / brain_patch have no `gist` parameter: blocking here left the
        # user with impossible advice and pushed edits outside MCP (triage B2).
        warnings.append(
            Warning_(
                rule="gist_missing",
                message="note has no gist — add one via brain_write",
            )
        )
    if len(gist_value) > GIST_HARD_LIMIT:
        raise ValidationError(
            f"gist {len(gist_value)} chars > hard limit {GIST_HARD_LIMIT}. "
            f"Shorten gist to ≤{GIST_HARD_LIMIT} chars (recommended ≤{GIST_SOFT_LIMIT})."
        )

    # Malformed forward-ref check: `[[forward:X|alias]]` is structurally invalid.
    # The alias attaches to a real target; until the target exists, an alias
    # is meaningless. Run BEFORE broken_outgoing_ref so the diagnostic is precise.
    for link in extract_wikilinks(body):
        if link["target"].startswith(FORWARD_REF_PREFIX) and link["alias"] is not None:
            raise ValidationError(
                f"malformed forward-ref [[{link['raw']}]]: forward-refs do "
                f"not accept '|alias'. Use [[forward:{link['target'][len(FORWARD_REF_PREFIX):]}]] "
                f"first; add the alias after the target is created."
            )

    # Resolve every outgoing wiki-link; collect broken ones (skipping external refs:
    # forward: always, plus namespaces outside the scope-taxonomy whitelist).
    from symbiosis_brain.resolver import is_external_ref, resolve_target
    broken: list[str] = []
    for link in extract_wikilinks(body):
        target = link["target"]
        if is_external_ref(target, valid_scopes):
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

    return warnings


def _check_soft_warns(
    body: str,
    frontmatter: dict,
) -> list[Warning_]:
    """Return list of soft warnings. Empty list means no warnings."""
    warnings: list[Warning_] = []

    gist = _gist_text(frontmatter)
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
    vault_path: Path | None = None,
    require_gist: bool = True,
    tool_name: str = "brain_write",
) -> list[Warning_]:
    """Run all validation rules. Raises ValidationError on any hard-block
    failure. Returns a list of Warning_ for soft-warn rules.

    require_gist=False (brain_append / brain_patch): a missing gist becomes a
    soft warning instead of a hard block — those tools have no `gist` parameter,
    so the block used to hand out impossible advice (triage B2).
    tool_name is quoted in error messages so the advice names the tool actually
    called.
    vault_path feeds the scope whitelist for external-ref detection (triage B3);
    None → only [[forward:X]] counts as external.

    Caller is responsible for:
    - parsing frontmatter from raw markdown (validate_note expects a dict)
    - rendering warnings into the tool response

    The `path` and `title` parameters are unused today but reserved for
    future rules (e.g. type↔folder consistency) that need them.
    """
    warnings = _check_hard_blocks(
        path=path,
        body=body,
        frontmatter=frontmatter,
        storage=storage,
        valid_scopes=_valid_scopes_for(vault_path),
        require_gist=require_gist,
        tool_name=tool_name,
    )
    return warnings + _check_soft_warns(body=body, frontmatter=frontmatter)
