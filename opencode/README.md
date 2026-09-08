# OpenCode recall plugin for Symbiosis Brain

Source of truth for the OpenCode plugin that makes Symbiosis Brain work in
OpenCode. Currently **sources only** — no installer integration, no wheel
shipping, no release wiring. The plugin was validated end-to-end in the
`l-llm-test` scratch folder before it moved here.

## What it does

Two layers of recall, equivalent to what the Claude Code hooks provide:

- **Layer 1 — per-turn memory injection.** Catches each chat message and, on
  the next `experimental.chat.system.transform`, injects a compact block like
  `[memory: N hits for "<query>"]` with the top hits from the vault, plus
  `[routes: N hints]` tool-routing hints once the MCP roster is known.
- **Layer 2 — per-tool pre-action recall.** Hooks `tool.execute.before` for
  edit/write (and optionally bash) and throws a block-once error so the model
  sees relevant memory before it touches a file. Per-file-once, capped by
  `SYMBIOSIS_BRAIN_OC_RECALL_MAX_BLOCKS`, modes `block|silent|off`.

The heavy lifting is done by the existing CLI: `uv run --directory
<tools> python -m symbiosis_brain search-gist` and `pre-action-recall`.

## Layout

```
opencode/
  plugins/brain-recall.ts   # the plugin (named export BrainRecallPlugin)
  package.json              # pins @opencode-ai/plugin (dev types, 1.18.29)
```

To use it, copy `plugins/brain-recall.ts` into your project's
`.opencode/plugins/` (OpenCode auto-loads plugins from there), or wire it up
globally. The `@opencode-ai/plugin` package is only needed for TypeScript
types at authoring time.

## Environment

| Variable | Meaning |
|---|---|
| `SYMBIOSIS_BRAIN_VAULT` | Path to the vault (falls back to the `symbiosis-brain` MCP server env) |
| `SYMBIOSIS_BRAIN_TOOLS` | Path to the symbiosis-brain repo/package (falls back to the MCP server env) |
| `SYMBIOSIS_BRAIN_SCOPE` | Optional scope filter for recall |
| `SYMBIOSIS_BRAIN_PRE_ACTION_DISABLED=1` | Kill switch for the whole plugin |
| `SYMBIOSIS_BRAIN_RECALL_SKIP_SHORT_CHARS` | Min message length to trigger recall (default 15) |
| `SYMBIOSIS_BRAIN_OC_RECALL_TIMEOUT_MS` | Subprocess timeout (default 30000) |
| `SYMBIOSIS_BRAIN_OC_RECALL_MODE` | `block` (default) / `silent` / `off` |
| `SYMBIOSIS_BRAIN_OC_RECALL_MAX_BLOCKS` | Layer-2 block cap per session (default 5) |
| `SYMBIOSIS_BRAIN_OC_RECALL_BASH=1` | Also intercept bash commands in layer 2 (off by default) |
| `SYMBIOSIS_BRAIN_OC_ROSTER_OFF=1` | Disable the MCP roster temp-file bridge |

## Status

- [x] Working end-to-end (layer 1 + layer 2 + roster), validated 2026-09-08
      in the `l-llm-test` scratch folder on opencode v1.18.29.
- [ ] Installer integration (`symbiosis-brain setup opencode`), wheel
      shipping, release — deliberately NOT done yet.
- [ ] Relevance threshold for weak off-topic hits.
- [ ] Known gotchas and design context live in the private vault
      (`symbiosis-brain/research/2026-09-08-opencode-recall-plugin-gotchas`).

Public repo note: this folder ships no local paths, no vault content, and no
internal design docs — see the top-level CLAUDE.md privacy rules.
