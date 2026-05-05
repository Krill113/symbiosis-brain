---
name: brain-backfill-gists
description: >
  Backfill missing `gist:` field for existing vault notes. Run once after upgrading
  to gist-layer (or after importing many notes). Spawns parallel Explore subagents
  by scope. Idempotent — re-run safely after partial completion.
---

# Brain Backfill Gists — One-time vault migration

## When to use

- After implementing gist-layer (Step 4.5 in `brain-save` skill).
- After importing many external notes that don't have `gist:` field.
- When `brain_lint` reports many `gist_missing` entries.

## Procedure

### Step 1: Find notes that need backfill

Call `brain_lint`. From the result, extract `gist_missing` list.

If the list is empty — no work needed, exit.

### Step 2: Group by scope

Bucket the list by `scope` (each note's scope is in its frontmatter — read with `brain_read` if needed).

Typical buckets in our vault: `alpha-seti`, `alpha-details`, `alpha-pdf`, `alpha-local`, `alpha-faq`, `widgetcompare`, `beta`, `symbiosis-brain`, `alpha`, `global`.

### Step 3: Dispatch parallel Explore subagents

For each scope bucket — dispatch one `Agent` (subagent_type=Explore) in parallel. Each subagent:

1. Receives the list of paths in its scope.
2. For each path:
   - Read the note with `brain_read`.
   - Look at title + body. Determine note type (decision/pattern/mistake/etc.) from frontmatter.
   - Generate a 1-line gist (≤80 chars) using the type-template from `brain-save` skill Step 4.5:
     - `project` — what is it, what's distinctive
     - `decision` — what was decided + reason
     - `pattern` — when to apply + what it gives
     - `mistake` — what went wrong + why remember
     - `research` — what was investigated + main finding
     - `feedback` — what user noticed + what to change
     - `user` — principle/profile in one phrase
     - `wiki` — what reader learns
     - `reference` — where to look + why
   - Don't duplicate title. Don't write "what's inside" — write "what this note enables".
3. **Insert `gist:` line into frontmatter via direct file `Edit`** (NOT `brain_patch` — it operates on body only, server strips frontmatter before matching, so the closing `---` anchor is invisible to it):
   - Vault path: `<SYMBIOSIS_BRAIN_VAULT>/<note-path>` (use the env var if set, else hardcoded vault path)
   - Read the file via `Read` tool to confirm exact frontmatter format
   - Use `Edit` tool: anchor on the LAST frontmatter field (e.g. `tags: [foo]\n---` or `valid_from: '2026-04-25'\n---`) — replace with `<same-field>\ngist: <generated>\n---`
   - YAML escape: if gist contains `:`, single-quote the value (`gist: 'Spawn CAD: bg → DataModel'`); if it contains backslash, use single quotes too (avoid YAML `\X` invalid escape)
4. After all files done, call `mcp__symbiosis-brain__brain_sync` ONCE to refresh the index. If it returns `database is locked`, the lock will clear after MCP restart — files on disk are correct.
5. Report back which paths were backfilled.

### Step 4: Wait for all subagents to complete

When all return — call `brain_lint` again to verify `gist_missing_count` decreased.

### Step 5: Report to user

Output 1-2 lines: "Backfilled N notes across M scopes. K notes still flagged (gist_too_long after auto-generation)."

### Idempotency

Re-running this skill is safe — Step 1 only picks up notes that still lack `gist:`. Already-backfilled notes are skipped.

### Notes

- **Do not block** the user's main session for too long. If subagents take more than ~10 minutes — report progress and offer to continue later.
- **Quality matters more than completion.** If a generated gist is unclear, leave the note flagged and let the user (or `brain-save` next time) write a better one.
