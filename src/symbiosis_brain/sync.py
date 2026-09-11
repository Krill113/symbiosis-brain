import hashlib
import os
from dataclasses import dataclass, field
from pathlib import Path

# PyYAML is a hard dependency of python-frontmatter, which is what actually
# raises here — parse_note -> frontmatter.loads -> yaml.
import yaml

from symbiosis_brain.markdown_parser import extract_wikilinks, parse_note
from symbiosis_brain.storage import Storage


@dataclass
class SyncResult:
    added: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    skipped: int = 0
    failed: list[tuple[str, str]] = field(default_factory=list)
    """(rel_path, error) for notes sync could not read or parse. One bad note is
    now skipped and NAMED instead of aborting the whole vault (B5.1)."""
    moved: list[tuple[str, str]] = field(default_factory=list)
    """(old_path, new_path) for notes that left one path and arrived at another
    with the same bytes. A move is reported HERE and nowhere else — not in
    `removed`, not in `added` — so every consumer that mirrors this diff into
    another store (the vector index: server._apply_targeted_index and the
    hook-side sync paths) must handle it as a re-key, or the vector silently
    stays under the old path and the note drops out of vector search."""

    def __getitem__(self, key: str) -> int:
        """Backward-compat: stats["added"] returns count, mirroring the
        legacy dict-shaped return. Use the dataclass attributes (e.g.
        result.added) for the path lists."""
        if key in ("added", "updated", "removed", "failed", "moved"):
            return len(getattr(self, key))
        if key == "skipped":
            return self.skipped
        raise KeyError(key)

VAULT_DIRS = [
    "projects", "wiki", "research", "user", "decisions", "patterns", "mistakes", "feedback", "reference", "archive",
    # Scope-first canon (2026-09 reorg): the global scope tree. Legacy top-level
    # dirs above stay during the transition — both layouts are valid.
    "global/wiki", "global/research", "global/user", "global/decisions", "global/patterns",
    "global/mistakes", "global/feedback", "global/reference", "global/projects", "global/archive",
]
MD_GLOB = "**/*.md"  # md-only by design: .json (incl. tool-routing.local.json) stays out of the index
# MEMORY.md is the auto-memory bootstrap/fallback file, not a vault note — it has
# no gist and never will, so indexing it parked a permanent gist_missing entry in
# every lint report (B-N5).
SKIP_FILES = {"CLAUDE.md", "README.md", "log.md", "MEMORY.md"}

# <scope>/files/** holds raw materials (reports, scripts, assets) — never notes.
# One rule shared by the indexer scan and brain_lint; only DIRECTORY segments are
# checked, so a note merely named files.md stays indexed.
MATERIAL_DIRS = {"files"}


def is_material_path(rel_path: str) -> bool:
    return any(part in MATERIAL_DIRS for part in rel_path.split("/")[:-1])


def _content_hash(content: str) -> str:
    """16 hex chars of sha256 over the DECODED text. One definition for the
    move pairing (Pass 0), the skip check (Pass 1) and sync_one, so a move is
    recognised by exactly the value the skip check compares against — hashing
    raw bytes instead would disagree on CRLF files, which read_text folds."""
    return hashlib.sha256(content.encode()).hexdigest()[:16]


def _walk_vault(vault: Path) -> dict[str, Path]:
    """Every note file under the vault, as {vault-relative posix path: Path}.

    Selects exactly what `vault.glob(MD_GLOB)` plus the filters that followed
    it used to select — tests/test_sync_walk.py compares the two name for name
    on the running platform and Python — at a fraction of the cost: 73 -> 10 ms
    on a 1724-note vault (2026-09-11). pathlib built and matched a Path for
    every entry of every directory, .git's 4542 included, only to drop them
    afterwards; here an excluded directory is never entered.

    Rules, each mirroring glob or the old filters:
    - `*.md` is matched the way glob matches it by default: case-insensitive on
      Windows, case-sensitive elsewhere (os.path.normcase);
    - a DIRECTORY whose name matches is an entry too, because glob yields it
      (reading it then fails and is reported, exactly as before);
    - anything whose first path segment starts with "." is skipped — top-level
      dot dirs and dot files; a dot dir deeper down IS walked, as before;
    - SKIP_FILES are skipped by name at any depth; a directory named in
      MATERIAL_DIRS is never entered, at any depth (is_material_path);
    - a symlinked directory is not descended into — "**" never followed one
      (3.12: `is_dir() and not is_symlink()`; 3.13+: recurse_symlinks=False);
    - order is glob's: a directory's own entries, then its subdirectories.
    """
    found: dict[str, Path] = {}

    def visit(directory: str, prefix: str) -> None:
        try:
            with os.scandir(directory) as it:
                entries = list(it)
        except OSError:
            return  # glob skips an unreadable directory silently, too
        below: list[tuple[str, str]] = []
        for entry in entries:
            name = entry.name
            if not prefix and name.startswith("."):
                continue
            rel = prefix + name
            if os.path.normcase(name).endswith(".md") and name not in SKIP_FILES:
                found[rel] = vault / rel
            try:
                descend = entry.is_dir() and not entry.is_symlink()
            except OSError:
                descend = False
            if descend and name not in MATERIAL_DIRS:
                below.append((entry.path, rel + "/"))
        for sub_directory, sub_prefix in below:
            visit(sub_directory, sub_prefix)

    visit(str(vault), "")
    return found


class VaultSync:
    def __init__(self, vault_path: Path, storage: Storage):
        self.vault_path = vault_path
        self.storage = storage

    def _body_of(self, path: str) -> str:
        """One stored body, read on demand: Pass 0's empty-body guard is the
        only reader of a body in sync_all, and only for a vanished row."""
        row = self.storage._conn.execute(
            "SELECT content FROM notes WHERE path=?", (path,)).fetchone()
        return (row[0] if row is not None else "") or ""

    def sync_all(self) -> SyncResult:
        # A migration that changed how links resolve asks for one full rebuild:
        # purge the graph and re-ingest EVERY note so Pass 2 rebuilds it with
        # the current resolver. The old way to force the re-ingest was to NULL
        # every content_hash — which also made upsert_note restamp updated_at
        # for the whole vault, because the hash it compares against was gone.
        # Forcing is now a flag on this run: hashes stay, stamps stay. The
        # done-marker is written at the END of the run — a crash halfway used
        # to be resumable only because the NULLed hashes made the next run
        # re-ingest the rest; now the next run simply sees the marker missing.
        #
        # The forced run holds the cross-process reindex lock, and re-checks the
        # marker once it has it. Without that, two cold starts in one window
        # (five sessions in 2.5 minutes was measured) both see "no marker", both
        # purge, and the second purge can land AFTER the first has rebuilt the
        # graph and stamped the marker — if the second then dies, the graph is
        # empty and nothing will ever rebuild it. With the lock the second
        # waits, finds the marker, and takes the ordinary path.
        if self.storage.needs_full_reindex():
            # Lazy: sync must not drag the vector stack into every hook path.
            from symbiosis_brain.search import _reindex_lock
            with _reindex_lock(self.storage.db_path):
                if self.storage.needs_full_reindex():
                    return self._sync_all(force_all=True)
        return self._sync_all(force_all=False)

    def _sync_all(self, *, force_all: bool) -> SyncResult:
        result = SyncResult()
        if force_all:
            self.storage._conn.execute("DELETE FROM relations")
            self.storage._conn.execute("DELETE FROM entities")
            self.storage._conn.commit()

        disk_files = _walk_vault(self.vault_path)

        # Only what the passes below read: each row's path and content hash.
        # The whole row (list_notes: every body, two JSON columns per note and
        # an ORDER BY) was 71 of 298 ms of a no-change run, and a body is read
        # by exactly one check — Pass 0's empty-body guard, for a vanished row —
        # which fetches the one it needs (_body_of).
        db_notes = {
            r["path"]: {"path": r["path"], "content_hash": r["content_hash"]}
            for r in self.storage._conn.execute("SELECT path, content_hash FROM notes")
        }

        # Pass 0: a note that vanished from one path and appeared at another
        # with the same bytes is a MOVE, not a removal plus a birth. Pair
        # strictly 1:1 by content_hash — the same hash Pass 1 compares — and
        # re-key the row so it keeps its stamps. Anything ambiguous (two
        # identical notes both moved) is left to the plain delete + insert
        # below: re-born with fresh stamps, which is at least true. No pairing
        # by name, no partial matches, no journal: mass moves are rare and
        # this is insurance, not optimization. Runs BEFORE Pass 1 because Pass 1
        # inserts the new path first and the removal loop drops the old one
        # last — by then the stamps are gone.
        gone_by_hash: dict[str, list[str]] = {}
        for db_path, row in db_notes.items():
            if db_path in disk_files or not row.get("content_hash"):
                continue
            # A pairing is a claim of identity, and equal bytes are evidence only
            # when there are bytes: two empty-bodied stubs (a template deleted
            # here, another created there) share a hash and share nothing else.
            # Such a row is never paired — its successor is born fresh, which is
            # at least true. One condition, on the vanished side: the newcomer
            # has the same bytes by definition.
            if not self._body_of(db_path).strip():
                continue
            gone_by_hash.setdefault(row["content_hash"], []).append(db_path)
        preread: dict[str, str] = {}  # new files' text, read once here, reused in Pass 1
        new_by_hash: dict[str, list[str]] = {}
        if gone_by_hash:
            for rel_path, file_path in disk_files.items():
                if rel_path in db_notes:
                    continue
                try:
                    preread[rel_path] = file_path.read_text(encoding="utf-8")
                except (OSError, UnicodeDecodeError):
                    continue  # Pass 1 reports it under `failed`
                new_by_hash.setdefault(_content_hash(preread[rel_path]), []).append(rel_path)
        moved_to: set[str] = set()
        for h, olds in gone_by_hash.items():
            news = new_by_hash.get(h, [])
            if len(olds) != 1 or len(news) != 1:
                continue
            old, new = olds[0], news[0]
            self.storage.move_note(old, new)
            # Outgoing edges are rebuilt in Pass 2 under the new path — a
            # scope-prefix link can resolve differently under another scope.
            self.storage.delete_relations_by_source(old)
            row = db_notes.pop(old)
            row["path"] = new
            db_notes[new] = row
            moved_to.add(new)
            result.moved.append((old, new))

        # Pass 1: ingest notes (upsert). Collect changed notes so we can resolve
        # their wiki-links in Pass 2, after every note is present in the DB.
        # This ensures resolve_target() sees the full vault, regardless of glob order.
        changed_notes: list[tuple[str, str, str]] = []  # (path, title, body)

        for rel_path, file_path in disk_files.items():
            # One unreadable or unparsable note must cost exactly that note. Before
            # this guard a single unquoted colon in a gist raised out of the loop
            # and left the ENTIRE vault unsynced, with a traceback that never named
            # the file (B5.1). The caller reports `failed` with paths; brain_lint's
            # not_indexed category then shows the same file by name.
            try:
                content = preread.pop(rel_path, None)
                if content is None:
                    content = file_path.read_text(encoding="utf-8")
                content_hash = _content_hash(content)

                existing = db_notes.get(rel_path)
                unchanged = existing is not None and existing.get("content_hash") == content_hash
                # A moved note is unchanged by construction but its edges were
                # dropped in Pass 0 — it must reach Pass 2. Same for a forced
                # reindex: every note goes through, its stamps survive because
                # upsert_note sees the same hash on both sides.
                if unchanged and not force_all and rel_path not in moved_to:
                    result.skipped += 1
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
                    content_hash=content_hash,
                )
            except (yaml.YAMLError, ValueError, OSError, UnicodeDecodeError, TypeError) as e:
                # TypeError: a frontmatter value JSON cannot hold (bytes from
                # !!binary, a set from !!set). parse_note turns dates into
                # strings; anything else costs this one note, named — before
                # 2026-09-11 it escaped the loop and aborted the whole vault
                # with a message that named no file. server.py's write path
                # already listed it.
                result.failed.append((rel_path, f"{type(e).__name__}: {e}"))
                # We could not ingest it, so the stored hash no longer vouches
                # for the row being complete: in a forced run its edges were
                # already purged, after a move they were dropped in Pass 0. With
                # the hash intact and the bytes unchanged, the next run would
                # skip it and the graph would stay short of this note for good.
                # NULLing the hash costs one restamp when it finally ingests.
                self.storage._conn.execute(
                    "UPDATE notes SET content_hash=NULL WHERE path=?", (rel_path,)
                )
                self.storage._conn.commit()
                continue

            changed_notes.append((rel_path, parsed["title"], parsed["body"]))

            if rel_path in moved_to:
                pass  # reported in result.moved — neither an add nor an update
            elif existing:
                result.updated.append(rel_path)
            else:
                result.added.append(rel_path)

        for db_path in db_notes:
            if db_path not in disk_files:
                self.storage._conn.execute(
                    "DELETE FROM relations WHERE source_note=?", (db_path,)
                )
                self.storage.delete_note(db_path)
                result.removed.append(db_path)

        # Pass 2: resolve wiki-links now that the notes table is fully populated.
        for rel_path, title, body in changed_notes:
            self._sync_wikilinks(rel_path, title, body)

        if force_all:
            self.storage.mark_reindex_done()

        return result

    def sync_one(self, rel_path: str) -> None:
        """Upsert a single note from disk + re-resolve its outgoing wikilinks.

        O(1) in vault size — does NOT scan disk for other notes. Caller must
        have already written the file via atomic_write_text. External edits
        to OTHER notes are NOT detected by this call — use sync_all (run on
        session start) or the brain_sync MCP tool for that.
        """
        file_path = self.vault_path / rel_path
        content = file_path.read_text(encoding="utf-8")
        parsed = parse_note(content)
        # The hash rides in the upsert itself: updated_at is kept when it equals
        # the stored one (same bytes, e.g. a rename), restamped otherwise.
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
            content_hash=_content_hash(content),
        )
        self._sync_wikilinks(rel_path, parsed["title"], parsed["body"])
        self._reresolve_broken_inbound(rel_path)

    def _reresolve_broken_inbound(self, just_written_path: str) -> None:
        """After a note is added or modified, re-resolve broken relations
        whose raw_target *plausibly* points to this path. The plausibility
        filter (canonical-equals or basename-equals after anchor + scope-prefix
        stripping) caps actual resolver calls to near zero on the hot path —
        without it this is O(broken × notes) per write.
        """
        from symbiosis_brain.resolver import (
            resolve_target,
            _strip_anchor,
            _strip_scope_prefix,
            _strip_md,
        )
        import sqlite3 as _sqlite3

        new_canonical = _strip_md(just_written_path).lower()
        new_basename = new_canonical.rsplit("/", 1)[-1]

        for rel in self.storage.find_broken_relations():
            raw = rel["raw_target"] or ""
            target = raw.split("|", 1)[0].strip()
            if not target:
                continue
            cleaned = _strip_md(_strip_scope_prefix(_strip_anchor(target))).lower()
            if cleaned != new_canonical and cleaned != new_basename:
                continue
            canonical, broken = resolve_target(target, self.storage)
            if broken:
                continue
            self.storage.upsert_entity(name=canonical)
            try:
                self.storage.update_relation_resolution(
                    rel["id"], new_to_name=canonical, broken=False
                )
            except _sqlite3.IntegrityError:
                # Duplicate edge already exists in canonical form — drop the
                # broken duplicate instead of failing. Storage uses
                # isolation_level=None (autocommit, see Storage.__init__),
                # so the failed UPDATE was implicitly rolled back; no explicit
                # ROLLBACK needed here before the DELETE.
                self.storage._conn.execute(
                    "DELETE FROM relations WHERE id=?", (rel["id"],)
                )
                self.storage._conn.commit()

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
