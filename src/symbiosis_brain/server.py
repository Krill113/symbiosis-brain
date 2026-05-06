import argparse
import asyncio
import json
import logging
from datetime import datetime
from pathlib import Path

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

from symbiosis_brain.graph import GraphTraverser
from symbiosis_brain.search import SearchEngine
from symbiosis_brain.storage import Storage
from symbiosis_brain.sync import VAULT_DIRS, VaultSync
from symbiosis_brain.temporal import TemporalManager
from symbiosis_brain.markdown_parser import parse_note, render_note
from symbiosis_brain.lint import VaultLinter
from symbiosis_brain.atomic_write import atomic_write_text
from symbiosis_brain.write_lock import note_write_lock

import frontmatter

logger = logging.getLogger("symbiosis-brain")

app = Server("symbiosis-brain")

_storage: Storage | None = None
_search: SearchEngine | None = None
_sync: VaultSync | None = None
_graph: GraphTraverser | None = None
_temporal: TemporalManager | None = None
_vault_path: Path | None = None
_linter: VaultLinter | None = None
_ready: asyncio.Event | None = None


def _init(vault_path: Path):
    global _storage, _search, _sync, _graph, _temporal, _vault_path, _linter
    _vault_path = vault_path
    db_path = vault_path / ".index" / "brain.db"
    _storage = Storage(db_path)
    _search = SearchEngine(_storage)
    _sync = VaultSync(vault_path, _storage)
    _graph = GraphTraverser(_storage)
    _temporal = TemporalManager(_storage)
    _linter = VaultLinter(_storage, vault_path=vault_path)

    sync_result = _sync.sync_all()
    logger.info("Vault synced: added=%d updated=%d removed=%d skipped=%d",
                len(sync_result.added), len(sync_result.updated),
                len(sync_result.removed), sync_result.skipped)

    current_model = _search._model_name
    stored_model = _storage.get_schema_version("embedding_model")

    # Bootstrap: stored_model unset → infer from current state.
    # On legacy DBs upgraded into this code path, notes_vec was already
    # populated by the old `index_all()`-on-every-startup behaviour. If the
    # index is consistent with notes (no count drift), it's valid under the
    # current model — just register the model name, don't re-embed.
    # This avoids the 60s parallel-init collision otherwise triggered the
    # first time a freshly-upgraded vault meets multiple cold-starting
    # processes (write-lock contention exceeds busy_timeout).
    if stored_model is None:
        if _search.is_index_dirty():
            logger.info("Bootstrap: notes_vec drift detected, building full index")
            _search.index_all()
        _storage.set_schema_version("embedding_model", current_model)
        logger.info("Embedding model registered: %s", current_model)
        return

    # Real model change (rare, only on explicit upgrade to a different model).
    if stored_model != current_model:
        logger.warning("Embedding model changed (%s -> %s); rebuilding vector index",
                       stored_model, current_model)
        _search.index_all()
        _storage.set_schema_version("embedding_model", current_model)
        logger.info("Embeddings indexed (full re-build, model change)")
        return

    # Targeted incremental indexing — only the actual diff. Note: this MUST
    # run before the count-drift safety net below, otherwise the safety net
    # would fire after every sync that added notes (notes table briefly has
    # more rows than notes_vec until index_note runs).
    for path in sync_result.removed:
        _search.delete_vec(path)
    for path in sync_result.added + sync_result.updated:
        note = _storage.get_note(path)
        if note is None:
            continue
        _search.index_note(path, f"{note['title']}\n{note['content']}")

    # Final safety net: if drift remains after targeted updates, something's
    # structurally wrong (manual deletion, partial-write recovery) — rebuild.
    if _search.is_index_dirty():
        logger.warning("notes_vec inconsistent after targeted updates; rebuilding")
        _search.index_all()
        logger.info("Embeddings indexed (full re-build, count drift safety net)")
        return

    logger.info("Embeddings indexed (targeted: +%d ~%d -%d)",
                len(sync_result.added), len(sync_result.updated),
                len(sync_result.removed))


def _append_log(vault_path: Path, action: str, path: str, title: str):
    """Append operation to vault log. Non-blocking."""
    try:
        log_file = vault_path / "log.md"
        if not log_file.exists():
            log_file.write_text(
                "# Vault Operation Log\n\n"
                "| Date | Action | Path | Title |\n"
                "|------|--------|------|-------|\n",
                encoding="utf-8",
            )
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
        with open(log_file, "a", encoding="utf-8") as f:
            f.write(f"| {timestamp} | {action} | {path} | {title} |\n")
    except Exception:
        logger.warning("Failed to append to vault log", exc_info=True)


def _write_note_body_unlocked(rel_path: str, new_text: str, op: str, title: str) -> None:
    """FS write + DB sync + vec index WITHOUT acquiring the per-note lock.

    Call only when the caller already holds `note_write_lock` for `rel_path`
    (e.g. from inside a `with note_write_lock(...)` block). Using this from
    an unlocked context creates a race — prefer `_write_note_body` instead.
    """
    file_path = (_vault_path / rel_path).resolve()
    atomic_write_text(file_path, new_text)
    _append_log(_vault_path, op, rel_path, title)
    _sync.sync_one(rel_path)
    parsed = parse_note(new_text)
    _search.index_note(rel_path, f"{parsed['title']}\n{parsed['body']}")


def _write_note_body(rel_path: str, new_text: str, op: str, title: str) -> None:
    """Persist `new_text` to `rel_path` inside the vault, then log/sync/index.

    Per-note file lock serializes concurrent writes to the same path across
    processes. The lock covers FS write + DB sync + vec index so the three
    stay consistent under contention. Different notes do not block each other.

    `op` is one of "write", "append", "patch" (used in log only).
    """
    with note_write_lock(_vault_path, rel_path):
        _write_note_body_unlocked(rel_path, new_text, op, title)


@app.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(name="brain_search", description="Search knowledge by meaning (hybrid: semantic + keyword). Returns most relevant notes.", inputSchema={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Natural language query"},
                "scope": {"type": "string", "description": "Project scope (e.g. 'beta', 'widgetcompare'). Includes global. Omit for all."},
                "limit": {"type": "integer", "default": 5},
                "mode": {
                    "type": "string",
                    "enum": ["preview", "gist"],
                    "default": "preview",
                    "description": "preview = full content snippet (legacy); gist = 1-line gist field (or first-paragraph fallback). Use gist for compact recall.",
                },
            },
            "required": ["query"],
        }),
        Tool(name="brain_read", description="Read a specific note by path (e.g. 'projects/beta.md')", inputSchema={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Relative path in vault"},
            },
            "required": ["path"],
        }),
        Tool(name="brain_write", description="Write or update a knowledge note. Creates markdown file in vault.", inputSchema={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Relative path (e.g. 'wiki/dapper-patterns.md')"},
                "title": {"type": "string"},
                "body": {"type": "string", "description": "Markdown body. Use [[wiki links]] for connections."},
                "note_type": {"type": "string", "enum": ["project", "wiki", "research", "decision", "user", "pattern", "mistake", "feedback"], "default": "wiki"},
                "scope": {"type": "string", "default": "global"},
                "tags": {"type": "array", "items": {"type": "string"}},
                "gist": {"type": "string", "description": "1-line summary (≤80 chars), used by mid-conversation recall. Optional but warned-if-missing."},
                "valid_from": {"type": "string", "description": "ISO date when this became true"},
                "valid_to": {"type": "string", "description": "ISO date when this stopped being true (for superseded notes)"},
            },
            "required": ["path", "title", "body"],
        }),
        Tool(name="brain_append", description="Append content to an existing '## section' of a note without rewriting the whole file. Use for incremental updates.", inputSchema={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Relative path in vault"},
                "section": {"type": "string", "description": "Exact '## heading' name without the '## ' prefix. Case-sensitive."},
                "content": {"type": "string", "description": "Markdown fragment to append at end of the section."},
                "create_if_missing": {"type": "boolean", "default": False, "description": "If true and section does not exist, create it at the end of the file."},
            },
            "required": ["path", "section", "content"],
        }),
        Tool(name="brain_patch", description="Replace a unique anchor substring in a note's body. For pinpoint edits (flip status, fix a line). Anchor must be unique in the body.", inputSchema={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Relative path in vault"},
                "anchor": {"type": "string", "description": "Exact substring to replace. Must be unique in the body. May span multiple lines."},
                "replacement": {"type": "string", "description": "New content. Empty string deletes the anchor."},
            },
            "required": ["path", "anchor", "replacement"],
        }),
        Tool(name="brain_context", description="Traverse knowledge graph from an entity. Returns connected facts at N hops depth. By default stops expansion at well-known hubs and high-in-degree nodes to prevent fan-out — pass include_hubs=true to disable.", inputSchema={
            "type": "object",
            "properties": {
                "entity": {"type": "string", "description": "Entity name to start from"},
                "depth": {"type": "integer", "default": 2, "description": "Max hops (1-3)"},
                "hub_threshold": {"type": "integer", "default": 20, "description": "Nodes with incoming-edge count ≥ this become terminals (not expanded). Set high (e.g. 999) to disable."},
                "include_hubs": {"type": "boolean", "default": False, "description": "If true, traverse through hubs anyway — mirrors pre-W2 behaviour."},
            },
            "required": ["entity"],
        }),
        Tool(name="brain_list", description="List notes in vault, optionally filtered by scope or type", inputSchema={
            "type": "object",
            "properties": {
                "scope": {"type": "string"},
                "note_type": {"type": "string"},
                "strict": {
                    "type": "boolean",
                    "default": False,
                    "description": "If true and scope is set, return ONLY notes in that scope (exclude the usual fallback to 'global'). Default false matches brain_list's usual behaviour of including global notes alongside scope-specific ones.",
                },
            },
        }),
        Tool(name="brain_status", description="Show vault status: note count, entity count, index health", inputSchema={
            "type": "object", "properties": {},
        }),
        Tool(name="brain_sync", description="Re-sync vault files to database (after manual edits)", inputSchema={
            "type": "object", "properties": {},
        }),
        Tool(name="brain_lint", description="Audit vault: orphans, weak links, broken references, scope warnings, type drift", inputSchema={
            "type": "object", "properties": {},
        }),
    ]


@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    if _ready is not None and not _ready.is_set():
        await _ready.wait()
    if _storage is None:
        return [TextContent(type="text", text="Error: server not initialized. Call _init() first.")]

    if name == "brain_search":
        mode = arguments.get("mode", "preview")
        results = _search.search(
            query=arguments["query"],
            scope=arguments.get("scope"),
            limit=arguments.get("limit", 5),
            mode=mode,
        )
        output_parts = []
        if mode == "gist":
            # Compact line-per-hit format
            scope_label = arguments.get("scope") or "all"
            if not results:
                return [TextContent(type="text", text="")]
            header = f"[memory: {len(results)} hits, scope={scope_label}]"
            lines = [header]
            for note in results:
                gist = note.get("gist", "")
                lines.append(f"- {note['path']} — {gist}")
            return [TextContent(type="text", text="\n".join(lines))]
        # preview mode — legacy formatting
        for note in results:
            warning = _temporal.staleness_warning(note)
            header = f"### {note['title']} ({note['path']})"
            if warning:
                header += f"\n> {warning}"
            snippet = note["content"][:500]
            if len(note["content"]) > 500:
                snippet += "..."
            output_parts.append(f"{header}\n\n{snippet}")
        if not output_parts:
            return [TextContent(type="text", text="No results found.")]
        return [TextContent(type="text", text="\n\n---\n\n".join(output_parts))]

    elif name == "brain_read":
        note = _storage.get_note(arguments["path"])
        if not note:
            return [TextContent(type="text", text=f"Note not found: {arguments['path']}")]
        warning = _temporal.staleness_warning(note)
        text = f"# {note['title']}\n"
        text += f"type: {note['note_type']} | scope: {note['scope']} | tags: {', '.join(note['tags'])}\n"
        if warning:
            text += f"> {warning}\n"
        text += f"\n{note['content']}"
        return [TextContent(type="text", text=text)]

    elif name == "brain_write":
        extra_fm = {}
        if arguments.get("valid_from"):
            extra_fm["valid_from"] = arguments["valid_from"]
        if arguments.get("valid_to"):
            extra_fm["valid_to"] = arguments["valid_to"]
        if arguments.get("gist"):
            extra_fm["gist"] = arguments["gist"]
        md_content = render_note(
            title=arguments["title"],
            body=arguments["body"],
            note_type=arguments.get("note_type", "wiki"),
            scope=arguments.get("scope", "global"),
            tags=arguments.get("tags"),
            extra_frontmatter=extra_fm or None,
        )
        file_path = (_vault_path / arguments["path"]).resolve()
        if not file_path.is_relative_to(_vault_path.resolve()):
            return [TextContent(type="text", text="Error: path must be within vault")]
        is_new = not file_path.exists()
        file_path.parent.mkdir(parents=True, exist_ok=True)
        _write_note_body(arguments["path"], md_content, "write", arguments["title"])
        msg = f"Saved: {arguments['path']}"
        if not arguments.get("gist"):
            msg += ("\n\n⚠️ Note saved without `gist:` field. "
                    "Add a 1-line gist (≤80 chars) describing why this note exists. "
                    "Used by mid-conversation recall.")
        if is_new:
            note_count = _storage.count_notes()
            if note_count > 0 and note_count % 25 == 0:
                msg += (f"\n\n(Vault has {note_count} notes."
                        " Consider reviewing for duplicates.)")
        return [TextContent(type="text", text=msg)]

    elif name == "brain_append":
        from symbiosis_brain.sections import append_to_section, SectionNotFoundError

        rel_path = arguments["path"]
        file_path = (_vault_path / rel_path).resolve()
        if not file_path.is_relative_to(_vault_path.resolve()):
            return [TextContent(type="text", text="Error: path must be within vault")]
        if not file_path.exists():
            return [TextContent(type="text", text=f"Error: note not found: {rel_path}")]

        # Read-modify-write must be atomic: hold the per-note lock for the
        # entire cycle so a concurrent append cannot read stale content and
        # clobber the first writer's changes.
        with note_write_lock(_vault_path, rel_path):
            raw = file_path.read_text(encoding="utf-8")
            post = frontmatter.loads(raw)
            try:
                new_body = append_to_section(
                    post.content,
                    arguments["section"],
                    arguments["content"],
                    create_if_missing=arguments.get("create_if_missing", False),
                )
            except SectionNotFoundError as e:
                return [TextContent(type="text", text=f"Error: {e}")]

            post.content = new_body
            new_text = frontmatter.dumps(post) + "\n"
            _write_note_body_unlocked(rel_path, new_text, "append", post.metadata.get("title", ""))
        return [TextContent(type="text", text=f"Appended to '## {arguments['section']}' in {rel_path}")]

    elif name == "brain_patch":
        from symbiosis_brain.sections import (
            replace_anchor,
            AnchorNotFoundError,
            AnchorAmbiguousError,
        )

        rel_path = arguments["path"]
        file_path = (_vault_path / rel_path).resolve()
        if not file_path.is_relative_to(_vault_path.resolve()):
            return [TextContent(type="text", text="Error: path must be within vault")]
        if not file_path.exists():
            return [TextContent(type="text", text=f"Error: note not found: {rel_path}")]

        raw = file_path.read_text(encoding="utf-8")
        post = frontmatter.loads(raw)
        try:
            new_body = replace_anchor(
                post.content,
                arguments["anchor"],
                arguments["replacement"],
            )
        except AnchorNotFoundError as e:
            return [TextContent(type="text", text=f"Error: {e}")]
        except AnchorAmbiguousError as e:
            return [TextContent(type="text", text=f"Error: {e}")]

        post.content = new_body
        new_text = frontmatter.dumps(post) + "\n"
        _write_note_body(rel_path, new_text, "patch", post.metadata.get("title", ""))
        return [TextContent(type="text", text=f"Patched {rel_path}")]

    elif name == "brain_context":
        result = _graph.traverse(
            start=arguments["entity"],
            max_depth=min(arguments.get("depth", 2), 3),
            hub_threshold=arguments.get("hub_threshold", 20),
            include_hubs=arguments.get("include_hubs", False),
        )
        if not result["neighbors"]:
            return [TextContent(type="text", text=f"No connections found for '{arguments['entity']}'")]
        lines = [f"## Graph context for: {result['start']}\n"]
        hub_names: list[str] = []
        for n in result["neighbors"]:
            marker = " [HUB]" if n.get("is_hub") else ""
            name = n["name"]
            if name.startswith("broken:"):
                display = f"(broken) {name.removeprefix('broken:')}"
            else:
                display = name
            lines.append(f"- {'  ' * (n['depth'] - 1)}[d={n['depth']}] {display}{marker}")
            if n.get("is_hub"):
                hub_names.append(name)
        lines.append(f"\n### Edges ({len(result['edges'])})")
        for e in result["edges"]:
            if e.get("broken"):
                display = e.get("label") or e["to"].removeprefix("broken:")
                target = f"(broken) {display}"
            elif e.get("label"):
                target = f"{e['to']} ({e['label']})"
            else:
                target = e["to"]
            lines.append(f"- {e['from']} --{e['type']}--> {target}")
        if hub_names and not arguments.get("include_hubs", False):
            lines.append(
                f"\n_Filtered {len(hub_names)} hub(s) to prevent fan-out: "
                f"{', '.join(hub_names)}. Pass include_hubs=true to expand through them._"
            )
        return [TextContent(type="text", text="\n".join(lines))]

    elif name == "brain_list":
        notes = _storage.list_notes(
            scope=arguments.get("scope"),
            note_type=arguments.get("note_type"),
            strict=arguments.get("strict", False),
        )
        if not notes:
            return [TextContent(type="text", text="No notes found.")]
        lines = [f"- **{n['title']}** ({n['path']}) [{n['note_type']}, {n['scope']}]" for n in notes]
        return [TextContent(type="text", text="\n".join(lines))]

    elif name == "brain_status":
        notes = _storage.list_notes()
        entities = _storage.list_entities()
        text = f"Vault: {_vault_path}\n"
        text += f"Notes: {len(notes)}\n"
        text += f"Entities: {len(entities)}\n"
        text += f"Vector index: {'enabled' if _search._vec_enabled else 'disabled (FTS only)'}\n"
        text += f"Ready: {'yes' if _ready is None or _ready.is_set() else 'initializing...'}\n"
        by_type: dict[str, int] = {}
        for n in notes:
            by_type[n["note_type"]] = by_type.get(n["note_type"], 0) + 1
        for t, count in sorted(by_type.items()):
            text += f"  {t}: {count}\n"
        return [TextContent(type="text", text=text)]

    elif name == "brain_sync":
        sync_result = _sync.sync_all()
        _search.index_all()
        summary = {
            "added": len(sync_result.added),
            "updated": len(sync_result.updated),
            "removed": len(sync_result.removed),
            "skipped": sync_result.skipped,
        }
        return [TextContent(type="text", text=f"Sync complete: {json.dumps(summary)}")]

    elif name == "brain_lint":
        report = _linter.lint()
        s = report["summary"]
        lines = [
            f"# Vault Lint Report",
            f"",
            f"Total notes: {s['total_notes']}  |  "
            f"Orphans: {s['orphan_count']}  |  "
            f"Weak links: {s['weak_link_count']}  |  "
            f"Broken links: {s['broken_link_count']}  |  "
            f"Scope warnings: {s['scope_warning_count']}  |  "
            f"Type drift: {s['type_drift_count']}  |  "
            f"Gist missing: {s.get('gist_missing_count', 0)}  |  "
            f"Gist too long: {s.get('gist_too_long_count', 0)}  |  "
            f"Gist equals title: {s.get('gist_equals_title_count', 0)}",
        ]
        if report["orphans"]:
            lines.append(f"\n## Orphans (0 wiki-links) — {s['orphan_count']}")
            for i in report["orphans"]:
                lines.append(f"- `{i['path']}` — {i['title']}")
        if report["weak_links"]:
            lines.append(f"\n## Weak Links (<2 wiki-links) — {s['weak_link_count']}")
            for i in report["weak_links"]:
                lines.append(f"- `{i['path']}` — {i['title']} ({i['link_count']} link)")
        if report["broken_links"]:
            lines.append(f"\n## Broken Links — {s['broken_link_count']}")
            for i in report["broken_links"]:
                lines.append(f"- `{i['source']}` → [[{i['target']}]] (no matching note)")
        if report["scope_warnings"]:
            lines.append(f"\n## Scope Warnings (scope not in whitelist) — {s['scope_warning_count']}")
            for i in report["scope_warnings"]:
                lines.append(f"- `{i['path']}` — scope=`{i['scope']}` (see [[reference/scope-taxonomy]])")
        if report.get("type_drift"):
            lines.append(f"\n## Type Drift (folder ≠ note_type) — {s['type_drift_count']}")
            for i in report["type_drift"]:
                lines.append(
                    f"- `{i['path']}` — type=`{i['actual_type']}`, "
                    f"expected=`{i['expected_type']}` (set `allow_type_mismatch: true` to silence)"
                )
        if report.get("gist_missing"):
            lines.append(f"\n## Gist Missing — {s['gist_missing_count']}")
            for i in report["gist_missing"]:
                lines.append(f"- `{i['path']}` — {i['title']}")
        if report.get("gist_too_long"):
            lines.append(f"\n## Gist Too Long (>100 chars) — {s['gist_too_long_count']}")
            for i in report["gist_too_long"]:
                lines.append(f"- `{i['path']}` — {i['title']} ({i['length']} chars)")
        if report.get("gist_equals_title"):
            lines.append(f"\n## Gist Equals Title — {s['gist_equals_title_count']}")
            for i in report["gist_equals_title"]:
                lines.append(f"- `{i['path']}` — {i['title']}")
        if (not report["orphans"] and not report["weak_links"]
                and not report["broken_links"] and not report["scope_warnings"]
                and not report.get("type_drift")
                and not report.get("gist_missing")
                and not report.get("gist_too_long")
                and not report.get("gist_equals_title")):
            lines.append("\nAll notes are well-connected.")
        return [TextContent(type="text", text="\n".join(lines))]

    return [TextContent(type="text", text=f"Unknown tool: {name}")]


def main():
    parser = argparse.ArgumentParser(description="Symbiosis Brain MCP Server")
    parser.add_argument("--vault", type=str, required=True, help="Path to vault directory")
    args = parser.parse_args()

    vault_path = Path(args.vault).expanduser().resolve()
    if not vault_path.exists():
        vault_path.mkdir(parents=True)
        for d in VAULT_DIRS:
            (vault_path / d).mkdir()

    asyncio.run(_run_server(vault_path))


async def _run_server(vault_path: Path):
    global _ready
    _ready = asyncio.Event()

    async def _background_init():
        try:
            await asyncio.to_thread(_init, vault_path)
            logger.info("Symbiosis Brain init complete, tools ready")
        except Exception:
            logger.exception("Symbiosis Brain background init failed")
        finally:
            _ready.set()

    asyncio.create_task(_background_init())

    async with stdio_server() as (read_stream, write_stream):
        await app.run(read_stream, write_stream, app.create_initialization_options())


if __name__ == "__main__":
    main()
