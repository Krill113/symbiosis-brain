---
name: brain-tools
description: >
  Onboard YOUR tools into Symbiosis Brain tool-routing. Use when the user types
  /brain-tools, or asks to set up / onboard / configure / register their tools,
  MCP servers, or routing — e.g. "настрой мои инструменты", "set up tool routing",
  "добавь мои тулы в роутинг", "configure routing for my MCP servers". Lists the
  tools available in THIS install, auto-routes the ones it already knows, asks one
  short question per unknown tool, and writes a per-install override file so each
  user tailors routing to their own toolbox without editing package code.
---

# Brain Tools — Routing Onboarding

On-demand discovery skill (spec §7, C6). It surveys the tools available in *this*
install, keeps the ones the shipped catalog already covers, and helps the user
draft routes for anything new — then writes them to a per-install override that
the routing engine merges on top of the package defaults.

## Universality (why this is data, not code)

The Symbiosis Brain package ships a universal default catalog
(`tool-routing.json`: web / registry / powershell / playwright / serena /
catalog / debug). Another user with a different toolbox configures THEIR tools
**without touching package code** — purely by data, via a per-install override
file `tool-routing.local.json` in the vault (git-ignored, never indexed by
`brain_search`). This skill is the friendly front-end that produces that file.

## When to use

Run only on explicit request — `/brain-tools`, or when the user asks to set up,
onboard, configure, or register their tools / MCP servers / routing. Do NOT
trigger it on every session; routing already works from the defaults out of the
box. This skill is for tailoring routing to a specific toolbox.

## Fail-open contract

Every step degrades to a no-op rather than blocking:
- Roster cache absent → fall back to a live `claude mcp list`; if that fails
  too, work from the built-ins alone.
- `$SYMBIOSIS_BRAIN_VAULT` unset / unwritable → tell the user once and stop; do
  not crash.
- User declines to answer for an unknown tool → skip that tool, keep the rest.

## Procedure

### Step 1: List the tools available in THIS install

Build the candidate set from three best-effort sources, in order:

1. **Roster cache (cheapest).** SessionStart primes a cache of `claude mcp list`
   output. Read it best-effort (the file may be absent on a cold first turn —
   that is expected, just move on):

   ```bash
   cat "${TMPDIR:-${TEMP:-/tmp}}/brain-mcp-roster-${CLAUDE_SESSION_ID}" 2>/dev/null
   ```

2. **Live fallback.** If the cache is empty or missing, query MCP servers live:

   ```bash
   claude mcp list
   ```

   Each line names a server (e.g. `serena`, `playwright`, `duckduckgo`); its
   tools surface as `mcp__<server>__*`.

3. **Built-ins.** Always include the always-available Claude Code tools
   (WebSearch, WebFetch, Bash, Edit, Write, Read, Task, etc.) — they need no
   server and are part of every install.

### Step 2: Split into KNOWN vs UNKNOWN

Read the shipped default catalog (fail-open — `2>/dev/null` so a missing file is
silent):

```bash
cat "${SYMBIOSIS_BRAIN_TOOLS}/src/symbiosis_brain/data/tool-routing.json" 2>/dev/null
```

For each discovered tool / server:
- **KNOWN** — already covered by a default route (its `when`-gate names this
  server/platform/skill, e.g. `duckduckgo` → `web-research-dual-engine`,
  `serena` → `serena-symbol-work`, `playwright` → `playwright-escalation`).
  These auto-route the moment the gate is satisfied — nothing to do, just report
  them as already covered.
- **UNKNOWN** — a tool with no matching default route (a custom MCP server, a
  niche built-in workflow the user cares about). These are the only ones that
  need input.

### Step 3: One question per UNKNOWN tool

For each unknown tool, ask exactly ONE short question and stop for the answer —
never batch a wall of questions. Keep it to the purpose and the trigger:

> Тул `<name>` я не знаю. Когда мне стоит про него вспомнить — на каких словах
> или намерении в запросе? (одной фразой; «пропусти» — если не нужен)

If the user says «пропусти» / «skip» / defers — drop that tool and continue.

### Step 4: Draft a route from the answer

Turn the answer into one route object (data, not prose). Map intent words to a
case-insensitive regex; pick a `when`-gate that keeps the route silent when the
tool is absent so it never bothers users who don't have it.

Per-route schema (see Shared contracts / spec §3.1):

```json
{"id": "my-custom-tool", "class": "augment",
 "triggers": [{"re": "intent-keywords", "flags": "i"}],
 "hint": "≤120 chars: what to use and why", "priority": 60,
 "when": "<server>-present", "expected_tool": "mcp__<server>__<tool>",
 "observable": false, "chain": [], "trial": false}
```

Field guidance:
- `class` — `augment` for net-new surface (most custom tools); `supersede` only
  when the route should replace a periodic tools-reminder for that intent.
- `triggers[].re` — JSON-escaped regex; `flags:"i"` for case-insensitive. Make
  it require a specific token so it fires on intent, not on every mention.
- `when` — gate token so the route stays silent without the tool. Grammar:
  `null` | `<name>-present` (MCP server, matched against the roster) |
  `skill:<name>-present` | `catalog-present` | `scope:<x>` | `platform:windows`,
  joined with `&`. For an MCP tool, use `<server>-present` (the server's display
  name as it appears in `claude mcp list`).
- `priority` — integer; higher wins when more routes than the cap match.
- `hint` — ≤120 chars; concrete ("use X for Y because Z"), not generic.
- `observable` — `true` only if `expected_tool` is one of
  `{Task, Edit, Write, MultiEdit, NotebookEdit, Bash}`; otherwise `false`.

Show the drafted route(s) to the user and confirm before writing.

### Step 5: Write / merge into the vault override

The override file is a **bare top-level JSON array** — match the package default
shape: `[ { "id": "...", "class": "...", "...": "..." } ]`. (The engine also
tolerates a `{"routes":[…]}` wrapper, but the package default ships a bare array,
so emit the bare array to stay consistent.)

Path — under the vault, never a hard-coded location:

```
$SYMBIOSIS_BRAIN_VAULT/tool-routing.local.json
```

This file is git-ignored (the installer adds `tool-routing.local.json` to the
vault `.gitignore`) and is never indexed by `brain_search` (sync is md-only), so
private routing stays private — never commit it.

Merge semantics — **by `id`**:
- New `id` → append the drafted object to the array.
- Existing `id` + fields → override those fields on the existing object.
- `{"id": "<id>", "disabled": true}` → turn a default route OFF for this install.

If the file does not exist yet, create it with a bare array containing just the
new route(s). If it exists, read it, merge by `id`, and write the full array
back. The skill writes the file itself — it is out-of-the-box; do not ask the
user to hand-edit JSON.

### Step 6: Confirm

One line, then return control — no further dialogue:

> ✓ Роутинг настроен. Новые маршруты в `tool-routing.local.json` (vault,
> git-ignored). Они подхватятся на следующем запросе — рестарт не нужен.

A freshly written local route fires on the next prompt with no MCP restart
(routing loads the override on every `search-gist` call).

## Idempotency

Safe to re-run. Known tools are reported, not re-added. Unknown tools already
present in `tool-routing.local.json` (same `id`) are updated in place, not
duplicated. Declining a tool leaves the file unchanged for that `id`.
