import hashlib
from pathlib import Path

from symbiosis_brain.markdown_parser import extract_wikilinks, parse_note
from symbiosis_brain.storage import Storage

VAULT_DIRS = ["projects", "wiki", "research", "user", "decisions", "patterns", "mistakes", "feedback", "reference"]
MD_GLOB = "**/*.md"
SKIP_FILES = {"CLAUDE.md", "README.md", "log.md"}


class VaultSync:
    def __init__(self, vault_path: Path, storage: Storage):
        self.vault_path = vault_path
        self.storage = storage

    def sync_all(self) -> dict[str, int]:
        stats = {"added": 0, "updated": 0, "removed": 0, "skipped": 0}

        # Force reindex on first sync after schema migration
        if self.storage.needs_full_reindex():
            self.storage._conn.execute("DELETE FROM relations")
            self.storage._conn.execute("DELETE FROM entities")
            self.storage._conn.execute("UPDATE notes SET content_hash=NULL")
            self.storage._conn.commit()
            self.storage.mark_reindex_done()

        disk_files: dict[str, Path] = {}
        for md_file in self.vault_path.glob(MD_GLOB):
            rel = md_file.relative_to(self.vault_path).as_posix()
            if md_file.name in SKIP_FILES:
                continue
            parts = rel.split("/")
            if parts[0].startswith("."):
                continue
            disk_files[rel] = md_file

        db_notes = {n["path"]: n for n in self.storage.list_notes()}

        # Pass 1: ingest notes (upsert). Collect changed notes so we can resolve
        # their wiki-links in Pass 2, after every note is present in the DB.
        # This ensures resolve_target() sees the full vault, regardless of glob order.
        changed_notes: list[tuple[str, str, str]] = []  # (path, title, body)

        for rel_path, file_path in disk_files.items():
            content = file_path.read_text(encoding="utf-8")
            content_hash = hashlib.sha256(content.encode()).hexdigest()[:16]

            existing = db_notes.get(rel_path)
            if existing and existing.get("content_hash") == content_hash:
                stats["skipped"] += 1
                continue

            parsed = parse_note(content)
            self.storage.upsert_note(
                path=rel_path,
                title=parsed["title"],
                content=parsed["body"],
                note_type=parsed["type"],
                scope=parsed["scope"],
                tags=parsed["tags"],
                frontmatter=parsed["extra"],
                valid_from=parsed["valid_from"],
                valid_to=parsed["valid_to"],
            )
            self.storage._conn.execute(
                "UPDATE notes SET content_hash=? WHERE path=?", (content_hash, rel_path)
            )
            self.storage._conn.commit()

            changed_notes.append((rel_path, parsed["title"], parsed["body"]))

            if existing:
                stats["updated"] += 1
            else:
                stats["added"] += 1

        for db_path in db_notes:
            if db_path not in disk_files:
                self.storage._conn.execute(
                    "DELETE FROM relations WHERE source_note=?", (db_path,)
                )
                self.storage.delete_note(db_path)
                stats["removed"] += 1

        # Pass 2: resolve wiki-links now that the notes table is fully populated.
        for rel_path, title, body in changed_notes:
            self._sync_wikilinks(rel_path, title, body)

        return stats

    def _sync_wikilinks(self, note_path: str, note_title: str, body: str):
        from symbiosis_brain.resolver import resolve_target

        self.storage._conn.execute(
            "DELETE FROM relations WHERE source_note=?", (note_path,)
        )
        source_canonical = note_path[:-3] if note_path.endswith(".md") else note_path
        links = extract_wikilinks(body)
        for link in links:
            target = link["target"]
            canonical, broken = resolve_target(target, self.storage)
            if broken:
                to_name = f"broken:{target[:200]}"
            else:
                to_name = canonical
            self.storage.upsert_entity(name=to_name)
            self.storage.upsert_relation(
                from_name=source_canonical,
                to_name=to_name,
                relation_type="references",
                source_note=note_path,
                label=link["alias"],
                raw_target=link["raw"],
                broken=broken,
            )
