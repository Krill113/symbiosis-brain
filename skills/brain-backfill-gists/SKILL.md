---
name: brain-backfill-gists
description: >
  Backfill missing `gist:` field for existing vault notes. Run once after upgrading
  to gist-layer (or after importing many notes). Spawns parallel Explore subagents
  by scope. Idempotent — re-run safely after partial completion.
---

# Brain Backfill Gists — One-time vault migration

## Client compatibility

Names such as `brain_lint`, `brain_read`, `brain_patch`, and `brain_sync` below
are logical Brain operation names. Use the actual exposed tool with the matching
operation; the MCP prefix differs by client. Keep the same grouping, gist
templates, per-note patching, verification, and idempotency ritual.

### Codex

Use the parallel-agent capability exposed by the current session with an explicit
model and reasoning effort. If it is unavailable, process the same bounded
buckets sequentially; do not issue a fake `Agent` call. Canonical skill files can
be resolved with `symbiosis-brain resources --skills`.

### OpenCode

The package `skillsDir` is registered through `skills.paths`. Use the native
parallel-agent interface when available; otherwise process the same bounded
buckets sequentially. Prefer `SYMBIOSIS_BRAIN_OPENCODE_VAULT`,
`SYMBIOSIS_BRAIN_OPENCODE_TOOLS`, and `SYMBIOSIS_BRAIN_OPENCODE_PYTHON`.

### Claude Code (default)

Keep the existing Claude behavior below unchanged, including the parallel
`Agent(subagent_type=Explore)` shape and MCP calls.

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

**Native clients:** Codex uses its actual parallel-agent interface with an
explicit model and reasoning effort; OpenCode uses its native parallel-agent
interface. If that capability is unavailable, process the same bounded buckets
sequentially. Do not issue a fake `Agent` call.

**Dispatch — Claude Code default:**

For each scope bucket — dispatch one `Agent` (subagent_type=Explore) in parallel.

**All clients — per-bucket procedure:**

Each dispatched worker or sequential bucket:

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
3. **Set the gist with the logical `brain_patch` operation**, one call per note.
   Use the actual exposed tool name. Claude Code spelling example:
   `mcp__symbiosis-brain__brain_patch(path="<note-path>", gist="<generated>")`
   - Frontmatter fields are named arguments — `gist`, `tags`, `title`, `valid_to` and the rest. No anchor, and the body is not touched.
   - **Do not hand-escape YAML.** The server serialises the value, so a gist containing `:` or a backslash needs nothing from you. Hand-escaping was required by the old `Edit` recipe and is exactly how one unquoted colon once broke `brain_sync` for the whole vault.
   - `anchor` still cannot reach frontmatter, and that is deliberate rather than a gap — `gist:` occurs in bodies too, and substring edits break on quoting and folded values while leaving a file that still parses. See the reasoning in `server.py` beside the tool schema. Do not try to reach the YAML block with an anchor.
4. No batch sync needed: `brain_patch` reindexes the note it wrote. Call the
   logical `brain_sync` operation only if you also changed files outside the
   tool. Claude Code spelling example: `mcp__symbiosis-brain__brain_sync`.
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
