from pathlib import Path

from symbiosis_brain.gist_limits import GIST_HARD_LIMIT
from symbiosis_brain.storage import Storage
from symbiosis_brain.resolver import (
    resolve_target,
    build_path_index,
    compute_linked_canonicals,
    is_external_ref,
)
# Reused, not re-derived: not_indexed only means anything if it scans the disk by
# exactly the rules VaultSync ingests by. (No import cycle — sync does not import lint.)
from symbiosis_brain.sync import MD_GLOB, SKIP_FILES, is_material_path
from symbiosis_brain.taxonomy import load_valid_scopes, load_folder_type_map

_TAXONOMY_PATH = "reference/scope-taxonomy.md"


class VaultLinter:
    """Audit vault connectivity: orphans, weak links, broken references, scope + type drift."""

    def __init__(self, storage: Storage, vault_path: Path):
        self._storage = storage
        self._vault_path = vault_path

    def _collect_not_indexed(self, db_paths: set[str]) -> list[dict]:
        """Markdown files on disk that have no row in the notes table.

        A note whose YAML does not parse never reaches the DB, and the linter reads
        only the DB — so it could not see such a file at all, and the symptom
        surfaced as an unexplained arithmetic gap instead of a filename (B5.2).
        Mirrors VaultSync's disk scan exactly: same glob, same SKIP_FILES, same
        "top-level dot-directory" rule. Never raises: an unreadable vault yields
        whatever was collected so far — brain_lint must not die on a scan."""
        out: list[dict] = []
        try:
            for md_file in self._vault_path.glob(MD_GLOB):
                if md_file.name in SKIP_FILES:
                    continue
                rel = md_file.relative_to(self._vault_path).as_posix()
                if rel.split("/")[0].startswith("."):
                    continue
                if is_material_path(rel):
                    continue
                if rel not in db_paths:
                    out.append({"path": rel})
        except OSError:
            # An unreadable directory must not take the whole report down; the
            # other categories are still worth printing. (lint.py has no logger
            # of its own — do not invent one here.)
            pass
        return sorted(out, key=lambda i: i["path"])

    def lint(self) -> dict:
        notes = self._storage.list_notes()
        not_indexed = self._collect_not_indexed({n["path"] for n in notes})
        valid_scopes = load_valid_scopes(self._vault_path)
        folder_type_map = load_folder_type_map(self._vault_path)
        # Build the resolution index ONCE; both broken-link detection AND orphan
        # detection re-resolve links LIVE (the persisted relations.broken flag goes
        # stale when a link target is renamed/deleted/made-ambiguous without
        # re-syncing the referrer — see mistakes/brain-sync-skips-stale-relations-
        # via-content-hash, 2026-06-04). `linked` is the set of canonicals with a
        # live-resolving inbound edge; count_orphans uses the same helper so the
        # write counter and brain_lint never disagree.
        path_index = build_path_index(self._storage)
        linked = compute_linked_canonicals(self._storage, index=path_index)

        orphans: list[dict] = []
        weak_links: list[dict] = []
        broken_links: list[dict] = []
        forward_refs: list[dict] = []
        scope_warnings: list[dict] = []
        type_drift: list[dict] = []
        gist_missing: list[dict] = []
        gist_too_long: list[dict] = []
        gist_equals_title: list[dict] = []
        scope_folder_mismatch: list[dict] = []
        audited = 0

        for note in notes:
            if note["path"] == _TAXONOMY_PATH:
                continue
            audited += 1

            canonical = note["path"].removesuffix(".md")

            outgoing = [
                r for r in self._storage.get_relations(canonical, direction="outgoing")
                if r["relation_type"] == "references"
            ]

            # Orphan (no live-resolving inbound) and weak_link (few outbound) are
            # independent axes: a note can appear in both buckets.
            if canonical not in linked:
                orphans.append({"path": note["path"], "title": note["title"]})

            if 0 < len(outgoing) < 2:
                weak_links.append({
                    "path": note["path"],
                    "title": note["title"],
                    "link_count": len(outgoing),
                })

            for rel in outgoing:
                # Broken-link detection re-resolves the target LIVE rather than
                # trusting the persisted relations.broken flag (which goes stale).
                raw_t = rel.get("raw_target")
                if raw_t:
                    # Mirror extract_wikilinks: unescape \| BEFORE splitting on the
                    # alias pipe, else an aliased [[path\|alias]] leaves a trailing
                    # backslash and resolve_target wrongly reports it broken.
                    target = raw_t.replace(r"\|", "|").split("|", 1)[0].strip()
                else:
                    # Legacy/hand-built rows without raw_target: derive from to_name,
                    # stripping the "broken:" marker sync uses for unresolved targets.
                    tn = rel["to_name"]
                    target = tn[len("broken:"):] if tn.startswith("broken:") else tn
                if not target:
                    continue
                # raw_target holds the original link text; fall back to to_name.
                reported_target = rel.get("raw_target") or rel["to_name"]
                # forward-refs and links into a foreign namespace point OUTSIDE the
                # vault: they are informational, not breakage (triage B3/B4). Storage
                # persists them broken=True by design — hence the live re-check here.
                if is_external_ref(target, valid_scopes):
                    forward_refs.append({
                        "source": note["path"],
                        "target": reported_target,
                    })
                    continue
                _canonical, is_broken = resolve_target(
                    target, self._storage, index=path_index
                )
                if is_broken:
                    broken_links.append({
                        "source": note["path"],
                        "target": reported_target,
                    })

            scope = note.get("scope") or ""
            if scope and scope not in valid_scopes:
                scope_warnings.append({
                    "path": note["path"],
                    "scope": scope,
                })

            fm = note.get("frontmatter") or {}
            path = note["path"]
            parts = path.split("/")
            if not fm.get("allow_type_mismatch"):
                # Two layouts are valid during the 2026-09 reorg transition:
                # scope-first <scope>/<type>/note.md (the canon) and legacy
                # top-level type folders. Flat <scope>/note.md and domain
                # subfolders carry no folder constraint; the project card
                # <scope>/<scope>.md and <scope>/archive/ entries are projects.
                expected = None
                if len(parts) >= 2 and parts[0] in valid_scopes:
                    if len(parts) == 2 and parts[1] == f"{parts[0]}.md":
                        expected = "project"
                    elif len(parts) >= 3 and parts[1] == "archive":
                        expected = "project"
                    elif len(parts) >= 3 and parts[1] in folder_type_map:
                        expected = folder_type_map[parts[1]]
                elif len(parts) >= 2 and parts[0] in folder_type_map:
                    expected = folder_type_map[parts[0]]
                if expected and note["note_type"] != expected:
                    type_drift.append({
                        "path": path,
                        "actual_type": note["note_type"],
                        "expected_type": expected,
                    })

            # Scope-first canon: the first path segment IS the scope — a note
            # filed under another scope's folder is misplaced even when its own
            # frontmatter scope is valid.
            if len(parts) >= 2 and parts[0] in valid_scopes and (note.get("scope") or "") != parts[0]:
                scope_folder_mismatch.append({
                    "path": path,
                    "folder_scope": parts[0],
                    "note_scope": note.get("scope") or "",
                })

            # Gist rules — skip CRITICAL_FACTS (root index, has no narrative gist)
            if note["path"] != "CRITICAL_FACTS.md":
                gist_value = (fm.get("gist") or "").strip() if isinstance(fm, dict) else ""
                if not gist_value:
                    gist_missing.append({
                        "path": note["path"],
                        "title": note["title"],
                    })
                else:
                    # Flag only past the HARD write-limit: a third of a living vault
                    # legitimately sits at 101-140 (512 notes measured 2026-09-02), so
                    # flagging the soft recommendation made this category pure noise.
                    if len(gist_value) > GIST_HARD_LIMIT:
                        gist_too_long.append({
                            "path": note["path"],
                            "title": note["title"],
                            "length": len(gist_value),
                        })
                    if gist_value.lower() == (note["title"] or "").strip().lower():
                        gist_equals_title.append({
                            "path": note["path"],
                            "title": note["title"],
                        })

        # The mirror of not_indexed: a DB row whose file vanished from disk —
        # exactly the state a crashed mid-call rename leaves behind. lint used
        # to check only the disk→DB direction, so the migration acceptance
        # criterion could not see it (skeptic finding, 2026-09-01).
        db_orphans = sorted(
            ({"path": n["path"]} for n in notes if not (self._vault_path / n["path"]).exists()),
            key=lambda i: i["path"],
        )

        return {
            "orphans": orphans,
            "weak_links": weak_links,
            "broken_links": broken_links,
            "not_indexed": not_indexed,
            "forward_refs": forward_refs,
            "scope_warnings": scope_warnings,
            "type_drift": type_drift,
            "gist_missing": gist_missing,
            "gist_too_long": gist_too_long,
            "gist_equals_title": gist_equals_title,
            "scope_folder_mismatch": scope_folder_mismatch,
            "db_orphans": db_orphans,
            "summary": {
                # total_notes == brain_status's note count. It used to be the
                # audited count, i.e. permanently one short (B5.2).
                "total_notes": len(notes),
                "audited_notes": audited,
                "orphan_count": len(orphans),
                "weak_link_count": len(weak_links),
                "broken_link_count": len(broken_links),
                "not_indexed_count": len(not_indexed),
                "forward_ref_count": len(forward_refs),
                "scope_warning_count": len(scope_warnings),
                "type_drift_count": len(type_drift),
                "gist_missing_count": len(gist_missing),
                "gist_too_long_count": len(gist_too_long),
                "gist_equals_title_count": len(gist_equals_title),
                "scope_folder_mismatch_count": len(scope_folder_mismatch),
                "db_orphans_count": len(db_orphans),
            },
        }
