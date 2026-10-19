---
name: brain-init
description: >
  Load Symbiosis Brain context at session start. MUST use at the beginning of
  every new conversation or when switching to a different project. Loads user
  profile, project context, umbrella ecosystem-context, and resolves scope via
  CLAUDE.md marker.
---

# Brain Init — Session Context Loading (post-C1)

## Client compatibility

Names such as `brain_status` and `brain_read` below are logical Brain operation
names. Use the actual exposed tool with the matching operation; the MCP prefix
differs by client. The ritual and its ordering stay unchanged.

### Codex

Use native skill invocation for `brain-welcome` and `brain-project-init` when it
is available. Otherwise resolve the installed canonical skill root with
`symbiosis-brain resources --skills` (a host may also expose it under
`$CODEX_HOME/skills`), read its canonical `SKILL.md`, and execute the original
ritual. Never invent a `Skill` tool call. For scope commands, prefer the installed
`brain-cli`; if unavailable, use the current interpreter as
`"<absolute current interpreter>" -m symbiosis_brain.scope_cli`. Do not use `uv run --directory`
for a wheel-only install. Prefer explicit `SYMBIOSIS_BRAIN_VAULT` and
`SYMBIOSIS_BRAIN_TOOLS`.

### OpenCode

The package `skillsDir` is registered through `skills.paths`; use native skill
invocation for `brain-welcome` and `brain-project-init`, or read their canonical
`SKILL.md` from that resource and execute the original ritual. Resolve paths from
`SYMBIOSIS_BRAIN_OPENCODE_VAULT`, `SYMBIOSIS_BRAIN_OPENCODE_TOOLS`, and
`SYMBIOSIS_BRAIN_OPENCODE_PYTHON`; run the latter with
`-m symbiosis_brain.scope_cli`. Do not use `uv run --directory`.

### Claude Code (default)

Keep the existing Claude behavior below unchanged, including its installed skills,
MCP tools, environment, and shell fallbacks.

## When to use

- Beginning of every new conversation.
- Switching to a different project context.
- After a long break in the same session.

## Procedure

### Step 0: First-time onboarding check

**Native clients — resolve the vault before probing its marker:**

- **Codex:** use explicit `SYMBIOSIS_BRAIN_VAULT` when present. Otherwise inspect
  `codex mcp get symbiosis-brain --json` and resolve the registered `--vault`
  argument or `SYMBIOSIS_BRAIN_VAULT` from the registered server environment.
  MCP registration environment is configuration data, not inherited parent
  environment. Call logical `brain_status` and require its Vault to be the same
  physical vault.
- **OpenCode:** map `SYMBIOSIS_BRAIN_OPENCODE_VAULT` to the
  `SYMBIOSIS_BRAIN_VAULT` placeholder used below, then call logical
  `brain_status` and require the same physical vault.
- If status is unavailable but there is a known resolved vault, that is not a
  vault mismatch: read `<resolved-vault>/MEMORY.md` if readable, do not run
  welcome or any write, do not execute the marker probe below, skip Steps 3-5,
  and continue the session without MCP.
- If status reports that the physical vault differs, stop Brain initialization
  and report the mismatch.
- If the resolved path is unknown, report that fallback memory is unavailable,
  do not guess a path, and continue the session without Brain memory.

**Claude Code default:** use the existing environment-backed probe below.

Before any other initialization, check whether this vault has been onboarded.

```bash
test -f "$SYMBIOSIS_BRAIN_VAULT/.sb-initialized" && echo present || echo absent
```

- If `present` → continue to Step 1 (normal bootstrap, welcome was done previously).
- If `absent` → invoke skill `brain-welcome`. After it returns, the marker exists; continue to Step 1.

This step runs at most once per vault (welcome-skill writes the marker on success).
If welcome-skill fails before writing the marker, the next session retries.

### Step 1: Verify MCP server

**Native clients:** the status-unavailable branch from Step 0 reads only
`<resolved-vault>/MEMORY.md` when that resolved path is known and readable. With
no resolved path, report the unavailable fallback and continue the session
without memory. Do not run welcome or writes; skip Steps 3-5.

**Claude Code default:** keep the original fallback below.

Call `brain_status`. If it returns an error:
- Inform the user: "Symbiosis Brain MCP unavailable, falling back to MEMORY.md".
- Read MEMORY.md for basic context.
- Skip Steps 3-5 — those require MCP.
- Continue without MCP tools for this session.

### Step 2: Load critical facts (L0)

Call `brain_read("CRITICAL_FACTS.md")`. ~120 tokens. Never skip.

### Step 3: Detect & validate scope

Use `brain-cli scope-resolve` to compute the canonical scope. Path to the tools repo
comes from `$SYMBIOSIS_BRAIN_TOOLS` (exported by SessionStart hook).

**Native clients — scope resolve:**

- **Codex:** run `brain-cli scope-resolve "$PWD"`. If the installed command is
  unavailable, run `"<absolute current interpreter>" -m
  symbiosis_brain.scope_cli scope-resolve "$PWD"`.
- **OpenCode:** run `"$SYMBIOSIS_BRAIN_OPENCODE_PYTHON" -m
  symbiosis_brain.scope_cli scope-resolve "$PWD"`.
- Do not use the Claude `uv --directory` examples for a wheel-only native install.

**Claude Code default — Windows** — PowerShell first (the bash subshell launched by Claude Code may lack `uv` on PATH):

```powershell
uv run --directory "$env:SYMBIOSIS_BRAIN_TOOLS" brain-cli scope-resolve "$PWD"
```

**Claude Code default — macOS/Linux** — bash:

```bash
uv run --directory "$SYMBIOSIS_BRAIN_TOOLS" brain-cli scope-resolve "$PWD"
```

**Claude Code default fallback:** If the primary call returns `uv: command not found` — retry through the other shell (PowerShell↔bash). If `$SYMBIOSIS_BRAIN_TOOLS` is unset, check `settings.json` env block for the absolute path, or use the module entry-point as a last resort:

```bash
uv run --directory "$SYMBIOSIS_BRAIN_TOOLS" python -m symbiosis_brain.scope_cli scope-resolve "$PWD"
```

The CLI returns one of:

| `source`         | meaning                                    | next action                                           |
|------------------|--------------------------------------------|-------------------------------------------------------|
| `marker_v1`      | `<project>/CLAUDE.md` has v1 marker        | use `scope` + `umbrella` from output                  |
| `marker_future`  | marker version ≥ 2 (newer client wrote it) | run `brain-project-init` with `mode=migration`        |
| `hook`           | no marker found, fell back to basename     | check vault — see Step 3.1                            |

If `source=marker_v1` AND `marker_status=draft`: scope is provisional. Read `<scope>/<scope>.md (legacy fallback: projects/<scope>.md)` — if `## Зачем` body is `TODO`, prompt the user to flesh it out (one short message, single attempt; don't pester).

#### Step 3.1: Vault check when source=hook

Call `brain_list(scope=<scope>)`.
- If non-empty → vault recognises this scope. Continue to Step 4.
- If empty → run skill `brain-project-init` (it will onboard the new scope, then return). After it returns, re-read CLAUDE.md marker via `brain-cli parse-marker` and use the freshly-written values.

### Step 4: Load project context (L1)

Call `brain_read("<scope>/<scope>.md (legacy fallback: projects/<scope>.md)")` directly — deterministic single-file read, no ranking. The project card is the single-source-of-truth for the scope (Roadmap, Current Status, Recent decisions, Active initiatives, Handoff sections). **Keep the loaded content in working memory for Steps 4.5 and 4.6 — do not re-read.**

Budget: typically 1-3K tokens. If the card is unusually large (>15K bytes / ~4K tokens) — note it and skip the umbrella search in Step 4.5 to stay within budget.

### Step 4.5: Umbrella-aware bootstrap (only if umbrella ≠ null)

If the marker (or the project-card frontmatter loaded in Step 4) names an `umbrella`:

1. `brain_read("wiki/<umbrella>-ecosystem-map.md")` — silently skip if absent.
2. From the project-card frontmatter (already loaded in Step 4) — note `dependents` and `dependencies` arrays. **Do not re-read the project card.**
3. `brain_search(scope=<umbrella>, limit=3)` — recent cross-cutting decisions/patterns.

Total budget for this step: ~1.5K tokens. Mention dependents in the briefing if the project has `role=shared-library`.

### Step 4.6: Surface latest handoff (if exists)

Scan the project card **already loaded in Step 4** for the latest `## Handoff YYYY-MM-DD` section. **No additional read** — work with what's in working memory.

- Date ≤ 14 days old → surface as **first item** in Step 6 briefing: `Handoff <date>: <next-step>` (plus WIP one-liner if non-empty).
- Date > 14 days old → silent skip; treat as stale, the next `brain-save` self-scan will remind.
- No section found → silent skip; same — next session's `brain-save` will trigger the contract.

Format reference: [[wiki/handoff-pattern]] (4 bullets: shipped / WIP / parked / next-step).

### Step 5: Recovery scenarios

**Native clients:** when a branch below says Edit, use the actual native
file-edit capability. Do not issue a fake `Edit` tool call. Claude Code keeps the
original Edit behavior below.

The following pathological states are handled silently in priority order:

- **Marker missing, `<scope>/<scope>.md (legacy fallback: projects/<scope>.md)` exists** → propose: "Маркер CLAUDE.md потерялся, восстановить из vault?". On yes, write marker via Edit (append at EOF). One question max. **Marker format** (single line, comma-separated `key=value` pairs):
  ```
  <!-- symbiosis-brain v1: scope=<scope>[, umbrella=<umbrella>][, status=<status>] -->
  ```
  Examples:
  - `<!-- symbiosis-brain v1: scope=symbiosis-brain -->` (no umbrella)
  - `<!-- symbiosis-brain v1: scope=acme-net, umbrella=acme -->` (with umbrella)
  - `<!-- symbiosis-brain v1: scope=newscope, status=draft -->` (provisional)
- **Marker present, `<scope>/<scope>.md (legacy fallback: projects/<scope>.md)` missing** → run `brain-project-init` with `mode=recovery` and pre-filled scope/umbrella from the marker. Skill should NOT re-ask "зачем" — read it from history if found, otherwise fall back to draft mode.
- **Marker version ≥ 2 (future client)** → run `brain-project-init` with `mode=migration`. Tell the user: "Маркер написан более новой версией Symbiosis Brain. Запускаю миграцию".
- **Marker scope ≠ frontmatter scope (drift)** → vault wins. Use frontmatter scope. Tell the user once: "Маркер CLAUDE.md устарел (`<old>` → `<new>`), исправить?". Apply on yes.

### Step 6: Brief summary

Output 2-3 lines:
- Who the user is (CRITICAL_FACTS).
- Which project we're in (resolved scope, umbrella if any).
- Top 1-2 things from search results (don't dump everything).

Do NOT dump all loaded content.
