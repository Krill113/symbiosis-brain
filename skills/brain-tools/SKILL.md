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

## Client compatibility

Names such as `brain_search` and `brain_sync` below are logical Brain operation
names. Use the actual exposed tool with the matching operation; the MCP prefix
differs by client. Preserve the fail-open roster, one-question, confirmation, and
merge-by-id ritual.

### Codex

Read the native tool inventory and effective skill inventory exposed by the
current session; do not run `claude mcp list`. Resolve packaged skills with
`symbiosis-brain resources --skills` when needed. Prefer explicit
`SYMBIOSIS_BRAIN_VAULT` and `SYMBIOSIS_BRAIN_TOOLS`.

### OpenCode

Read the native tool inventory. The package `skillsDir` is registered through
`skills.paths`; use that inventory rather than a Claude command. Prefer
`SYMBIOSIS_BRAIN_OPENCODE_VAULT`, `SYMBIOSIS_BRAIN_OPENCODE_TOOLS`, and
`SYMBIOSIS_BRAIN_OPENCODE_PYTHON`.

### Claude Code (default)

Keep the existing Claude behavior below unchanged, including its roster cache,
`claude mcp list` fallback, and built-in tool names.

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
- Native clients: if a cached roster is absent, continue from the actual tool
  inventory described in Step 1; do not substitute a Claude command.
- Claude Code default: roster cache absent → fall back to a live `claude mcp list`; if that fails
  too, work from the built-ins alone.
- Native clients: if the resolved native vault is unknown, mismatched, or not
  writable, tell the user once and stop this skill without writing.
- Claude Code default: `$SYMBIOSIS_BRAIN_VAULT` unset / unwritable → tell the user once and stop; do
  not crash.
- User declines to answer for an unknown tool → skip that tool, keep the rest.

## Procedure

### Step 1: List the tools available in THIS install

**Native clients:**

- **Codex:** start with the actual tool inventory exposed to this session;
  supplement MCP server names with `codex mcp list --json`, and use the native
  skill list exposed by the host.
- **OpenCode:** start with the actual tool inventory exposed to this session and
  the configured `skills.paths` inventory.

**Claude Code default sources:**

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

**Native clients — resolve the vault and packaged catalog:**

- **Codex:** use explicit `SYMBIOSIS_BRAIN_VAULT`; otherwise inspect
  `codex mcp get symbiosis-brain --json` for the registered `--vault` argument or
  `SYMBIOSIS_BRAIN_VAULT` in the registered server environment. That environment
  is registration data, not inherited parent environment. Require logical
  `brain_status` to report the same physical vault.
- **OpenCode:** map `SYMBIOSIS_BRAIN_OPENCODE_VAULT` to the resolved vault and
  require logical `brain_status` to report the same physical vault.
- Unknown paths, status failure, or mismatch stop this skill without a write; do
  not guess.
- Locate the read-only packaged catalog with
  `symbiosis-brain resources --routing` and read the path it prints. Do not infer
  a checkout path and do not perform a Python resource lookup.

**Claude Code default catalog path:**

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

For native clients, set `expected_tool` to the actual exposed tool identifier
from Step 1. `mcp__<server>__<tool>` is only the Claude Code example below; do
not manufacture that prefix for another client.

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
  joined with `&`. Native clients use the actual server name from Step 1.
  For the Claude Code default, use `<server>-present` (the server's display name
  as it appears in `claude mcp list`).
- `priority` — integer; higher wins when more routes than the cap match.
- `hint` — ≤120 chars; concrete ("use X for Y because Z"), not generic.
- `observable` — native clients use the actual observable tool identifier exposed
  by the session; if observability is unknown, use `false`. The Claude Code
  default set is `{Task, Edit, Write, MultiEdit, NotebookEdit, Bash}`; otherwise
  `false`.

Show the drafted route(s) to the user and confirm before writing.

### Step 5: Write / merge into the vault override

The override file is a **bare top-level JSON array** — match the package default
shape: `[ { "id": "...", "class": "...", "...": "..." } ]`. (The engine also
tolerates a `{"routes":[…]}` wrapper, but the package default ships a bare array,
so emit the bare array to stay consistent.)

**Native clients:** use
`<resolved-vault>/tool-routing.local.json`, and write only when the resolved
vault is known and writable.

**Claude Code default path:**

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

## Action rules (`class:"action"`)

A third route class, separate from `augment`/`supersede` above: it warns from
past mistakes at the MOMENT a matching Bash/PowerShell command is about to
run, not on prompt text. Compiled by `symbiosis-brain compile-action-rules`
into `<vault>/.index/action-rules.*` and matched by a pure-bash block in the
PreToolUse hook (see `action_rules.py`). Not authored through the Step 1–6
flow above — hand-write it into `tool-routing.local.json` alongside your
other routes:

```json
{"id": "git-reset-hard-after-fetch", "class": "action", "priority": 84,
 "command_triggers": {
   "bash": [{"re": "(^|[;&|]+)[[:space:]]*git[[:space:]]+reset[[:space:]]+--hard[[:space:]]+origin/"}],
   "powershell": [{"re": "(^|[;&|]+)[[:space:]]*git[[:space:]]+reset[[:space:]]+--hard[[:space:]]+origin/"}]
 },
 "hint": "Never reset --hard origin/* right after fetch: a failed fetch leaves a stale ref. [[mistakes/x]]",
 "test_match": {"bash": ["git reset --hard origin/main"], "powershell": ["git reset --hard origin/main"]},
 "test_nomatch": {"bash": ["git reset --soft HEAD~1"], "powershell": ["git reset HEAD README.md"]}}
```

- `command_triggers.bash` / `.powershell` — POSIX ERE patterns (`grep -E`
  syntax, e.g. `[[:space:]]`, NOT Python `re` — `[[:space:]]` silently
  misparses under Python's engine). At least one of the two tool sides.
- `test_match` / `test_nomatch` — **required**, per tool side. A tool side
  with no `test_match` vectors is dropped at compile time rather than
  shipped unvalidated — an over-broad unvalidated pattern would otherwise
  fire on everything and, because the hook exits on the first hit, silently
  suppress normal recall for every subsequent rule too.
- `id` — must match `[A-Za-z0-9._-]+` (compiled rules are rejected otherwise;
  it lands unescaped in the hook's JSON stdout and in `action-rule-hits.jsonl`).
- After editing `tool-routing.local.json`, run
  `symbiosis-brain compile-action-rules --vault <vault>` (or let
  `brain_sync`/setup do it) to (re)generate the compiled matcher files.
