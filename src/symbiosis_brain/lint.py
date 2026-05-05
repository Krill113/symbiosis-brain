from pathlib import Path

from symbiosis_brain.storage import Storage
from symbiosis_brain.taxonomy import load_valid_scopes, load_folder_type_map

_TAXONOMY_PATH = "reference/scope-taxonomy.md"


class VaultLinter:
    """Audit vault connectivity: orphans, weak links, broken references, scope + type drift."""

    def __init__(self, storage: Storage, vault_path: Path):
        self._storage = storage
        self._vault_path = vault_path

    def lint(self) -> dict:
        notes = self._storage.list_notes()
        valid_scopes = load_valid_scopes(self._vault_path)
        folder_type_map = load_folder_type_map(self._vault_path)

        orphans: list[dict] = []
        weak_links: list[dict] = []
        broken_links: list[dict] = []
        scope_warnings: list[dict] = []
        type_drift: list[dict] = []
        gist_missing: list[dict] = []
        gist_too_long: list[dict] = []
        gist_equals_title: list[dict] = []
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
            incoming_refs = [
                r for r in self._storage.get_relations(canonical, direction="incoming")
                if r["relation_type"] == "references" and not r.get("broken")
            ]

            # Orphan (no inbound) and weak_link (few outbound) are independent axes:
            # a note can appear in both buckets.
            if not incoming_refs:
                orphans.append({"path": note["path"], "title": note["title"]})

            if 0 < len(outgoing) < 2:
                weak_links.append({
                    "path": note["path"],
                    "title": note["title"],
                    "link_count": len(outgoing),
                })

            for rel in outgoing:
                # Broken-link detection uses ONLY the relations.broken flag.
                if rel.get("broken"):
                    # raw_target holds the original link text; fall back to to_name.
                    reported_target = rel.get("raw_target") or rel["to_name"]
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
            if not fm.get("allow_type_mismatch"):
                path = note["path"]
                folder = path.split("/", 1)[0] if "/" in path else ""
                expected = folder_type_map.get(folder)
                if expected and note["note_type"] != expected:
                    type_drift.append({
                        "path": path,
                        "actual_type": note["note_type"],
                        "expected_type": expected,
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
                    if len(gist_value) > 100:
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

        return {
            "orphans": orphans,
            "weak_links": weak_links,
            "broken_links": broken_links,
            "scope_warnings": scope_warnings,
            "type_drift": type_drift,
            "gist_missing": gist_missing,
            "gist_too_long": gist_too_long,
            "gist_equals_title": gist_equals_title,
            "summary": {
                "total_notes": audited,
                "orphan_count": len(orphans),
                "weak_link_count": len(weak_links),
                "broken_link_count": len(broken_links),
                "scope_warning_count": len(scope_warnings),
                "type_drift_count": len(type_drift),
                "gist_missing_count": len(gist_missing),
                "gist_too_long_count": len(gist_too_long),
                "gist_equals_title_count": len(gist_equals_title),
            },
        }
