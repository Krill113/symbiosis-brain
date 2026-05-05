# Symbiosis Brain — MCP Memory Server

Persistent knowledge base for Claude Code with semantic search, wiki-link graph, and bi-temporal tracking.

## Quick Start

```bash
uv tool install symbiosis-brain
symbiosis-brain setup claude-code
```

That's it. Restart Claude Code — `brain-welcome` will introduce itself
and help you finish the setup interactively.

## What `setup claude-code` does

The CLI handles the deterministic part of installation:

1. Asks where to put your vault (default: `~/symbiosis-brain-vault`).
2. Creates the vault directory with the standard folder layout (`projects/`, `wiki/`, …).
3. Registers the MCP server with Claude Code via `claude mcp add -s user`.
4. Copies skills (`brain-init`, `brain-recall`, `brain-save`, `brain-project-init`, `brain-welcome`) to `~/.claude/skills/`.
5. Copies hooks (Python-based, cross-platform) to `~/.claude/hooks/`.
6. Mutates `~/.claude/settings.json` with our hooks/statusLine/permissions blocks (deep-merge, atomic, with `.bak` backup; preserves your existing statusLine in an env-var).
7. Appends a Symbiosis Brain block to `~/.claude/CLAUDE.md` (idempotent via marker).

The interactive part — meeting you, creating `CRITICAL_FACTS.md`, optionally
helping you install Obsidian — happens in `brain-welcome` on the next Claude
Code session.

## Maintenance commands

```bash
symbiosis-brain doctor                       # health check
symbiosis-brain setup claude-code --repair   # fix only what's broken
symbiosis-brain uninstall                    # restore settings.json from .bak,
                                             # remove our skills/hooks (vault preserved)
symbiosis-brain migrate-hooks                # legacy bash → python (one-time)
```

## Updating

```bash
uv tool upgrade symbiosis-brain
```

`uv` handles the upgrade; no separate update command.
