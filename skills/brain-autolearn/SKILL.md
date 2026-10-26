---
name: brain-autolearn
description: >
  Use when brain-save Step 0 runs at session end (right after the project
  retrospective), when the user asks to look for repeated actions or repeated
  mistakes (/brain-autolearn), or when you catch yourself doing the same thing for
  the third time. Turns repetition into an action rule, a script, a skill, or a
  proposal — with the owner kept in the loop.
---

# Brain Autolearn — Turn Repetition Into Leverage

## Client compatibility

Names such as `brain_search` below are logical Brain operation names. Use the
actual exposed tool with the matching operation; the MCP prefix differs by
client. Keep the repetition sweep, rule of three, artifact tiers, registration,
inbox, and reporting ritual intact.

### Codex

Use the actual tool inventory exposed by the current session. Use `$CODEX_HOME`
only for skill files and client configuration when the host supports them;
otherwise resolve canonical skills with `symbiosis-brain resources --skills`,
read the canonical `SKILL.md`, and execute the original ritual. Prefer explicit
`SYMBIOSIS_BRAIN_VAULT` and `SYMBIOSIS_BRAIN_TOOLS`; the native-client command
branch in the recipes overrides Claude-only `uv` and hook examples.

### OpenCode

The package `skillsDir` is registered through `skills.paths`; use that canonical
source rather than creating a client-specific copy. Prefer
`SYMBIOSIS_BRAIN_OPENCODE_VAULT`, `SYMBIOSIS_BRAIN_OPENCODE_TOOLS`, and
`SYMBIOSIS_BRAIN_OPENCODE_PYTHON`; follow the native-client command branch in the
recipes.

### Claude Code (default)

Keep the existing Claude behavior below unchanged, including its skill locations,
MCP tools, routing files, and hook examples.

## Overview

A mistake written to the vault is not a lesson learned; a snippet retyped four times
is not a tool. This skill closes that gap: it scans the session for **repetition of
two kinds** — repeated mistakes and repeated actions — and converts each into an
artifact that fires by itself next time.

Two principles decide everything below:

- **Rule of three.** One occurrence is noise. Two is a candidate — count it. Three
  (within a session, or across sessions via the inbox) is a signal — act.
  Exception: a cheap deterministic script pays for itself at two — build it at ×2.
  Rules, skills, and anything the owner must review wait for three.
- **An artifact nobody recalls is a second vault of forgotten things.** Every artifact
  this skill creates gets a trigger in tool-routing so it surfaces when needed.
- **Fix the tool before teaching the agent.** When the owner builds the tool that
  caused the friction, a guard inside that tool (an early refusal, an honest status,
  a launcher that picks the right runtime) works even when recall does not. Memory
  is for judgment; it is not a safety contract.
- **Ripe means done, not noted.** A finding at ×3 is acted on in this pass or turns
  into one tracked proposal — it does not sit in the inbox marked "ripe".

The rule of three filters *automation* payoff. It is not a threshold for danger: one
occurrence of an in-house tool losing or duplicating data is enough to record it as a
defect (Step 6b).

Typical yield: 0–2 artifacts per session. A clean session must end in seconds with
"nothing to learn" — a heavy pass will be skipped, and a skipped pass learns nothing.

**Capture early, decide late.** The main pass runs at session end, when context is
short. So when an expensive or dangerous failure happens mid-session (minutes lost to a
timeout, a correction from the owner, a near-miss on data), write its evidence and the
working workaround as one inbox or ledger line right away — a few seconds — and leave
the counting and deciding to the end-of-session pass.

## Step 1: Repetition sweep

Ask each question against THIS session. Note only concrete moments.

1. Did I make the same mistake twice — or once here and it is already a `mistake`
   note in the vault? (verify with a targeted `brain_search`)
2. Did the user correct me the same way twice? Corrections are first-class signals,
   not just memory material.
3. Did I write the same snippet or command sequence three times, with variations?
4. Do 3+ tool calls always travel together? That is a bundle waiting to be named.
5. What took 3+ calls that should take one?
6. Did I re-derive something a note, skill, or script already holds? Then the fix is
   a **trigger**, not a new artifact. If several notes cover one theme (a cluster —
   e.g. five `workflow-*` gotchas), register ONE trigger pointing at a checklist/MOC
   note, not one trigger per note: N hints for one moment is noise, one checklist is
   recall.

## Step 2: Do NOT capture

These become self-imposed constraints that bite later when the environment changes:

- Environment-dependent failures (missing binary, credential, PATH quirk of one box).
- Negative claims about a tool («X is broken») — they harden into refusals months
  after the tool was fixed. Record the *workaround condition* instead, if anything.
- Transient errors that resolved before the session ended.
- One-off task narratives.
- Unresolved failures written up as a «reliable workflow».
- Anything that happened once — except a data-loss or safety defect of an in-house
  tool, which goes to the tool ledger at once (Step 6b).
- Several retries of one stuck operation counted as several occurrences. Count
  independent episodes or sessions, not attempts.

## Step 3: Classify and size

| finding | artifact | tier |
|---|---|---|
| repeated mistake, command-shaped | action rule — `references/action-rule-recipe.md` | 1 |
| repeated mistake, judgment-shaped | patch the existing `feedback`/`pattern` note (new note only if none fits) | 1 |
| repeated action, no judgment inside | script — `references/automation-recipe.md` | 1 |
| repeated action needing judgment | skill draft — `references/automation-recipe.md` | 2 |
| bigger than a script or a skill (it is really an application) | proposal to the owner | 3 |
| friction caused by an in-house tool | tool-ledger entry (Step 6b); at ×3 or on a data/safety defect — a proposed task for that tool | 3 |

- **Tier 1 — do it, then report.** Small, reversible, obviously useful. The owner
  hears `made X for Y, lives at Z, trigger T` — enough to stay informed and to
  correct you.
- **Tier 2 — draft, show, install on OK.** Skills go through the TDD cycle
  (baseline run without the skill, then the same prompt with it) before anything is
  installed.
- **Tier 3 — discuss first.** Write a short proposal (problem, repetition evidence,
  rough shape, why it is more than a script), hand it to the owner, and stop. The
  build, if approved, goes brainstorming → plan → verified build.

## Step 4: Prefer patching over creating

Before any new artifact, in this order:

1. Patch an artifact already used this session (a skill you invoked, a script you ran).
2. Patch the existing artifact for this **class** of task (search: `brain_search`,
   skills list, your tooling directory).
3. Add a reference file or script to that existing artifact.
4. Only then create — and name it at class level, never «fix-X-today».

## Step 5: Make it, register it

Follow the matching recipe. Prefer, in this order: a deterministic guard in an
in-house tool or launcher (proposed under Tier 3 unless the current task already
allows that change) → an action rule or route → a note.

Whatever the tier, the artifact is not done until:

- it has a **route** in `tool-routing.local.json` (prompt triggers and/or command
  triggers) so it surfaces in the moment it applies;
- a `pattern` note (or the patched note) links to it with `[[...]]`;
- the trigger fires on the *call that failed* — for an MCP tool, check that the
  client's hooks can see MCP calls at all before promising a command trigger;
- what surfaces is a short conditional action («modal window + UI-tree timeout →
  screenshot and click by coordinates»), not a pointer to a backlog.

## Step 6: Count what is not ripe yet

A finding seen once or twice goes to `global/projects/autolearn-inbox.md` (create on first
use) as one line:

`- <last-seen YYYY-MM-DD> ×N [status] <what> — evidence: <moments> — next: <artifact>`

Same finding already there → increment N, refresh last-seen, append the evidence.

Statuses: `candidate` (×1–2) → `ready` (×3: the artifact and where it lives are named)
→ `done` (link to the artifact and its route; move the line out of the inbox) — or
`proposed` (handed to the owner, link to the one proposal or task) / `blocked`
(reason). «Proposed» is not «done».

**Ready in this pass:** take at most ONE `ready` item, and only if it is cheap,
reversible and allowed by the current task (never a write the session was told not to
make). Anything else stays `ready` or becomes `proposed`, with the reason.

**Expiry:** a `candidate` not seen for 45 days moves to an `## Archive` section of the
same note — the knowledge stays, the queue shrinks; a new episode brings it back. Never
expire a data-loss defect, an unfinished `ready`, or a limit of the current version of a
tool.

## Step 6b: In-house tools ledger

Only when this session used a tool the owner (or their team) builds: an MCP server,
a CLI, a script set, a plugin of their own. Third-party tools are out of scope here;
if `brain-self-critique` is installed, it covers Symbiosis Brain itself — link to it, do not file
it twice.

1. **Registry.** `global/projects/tool-registry.md` (create on first use) lists
   in-house tools, one line each: `name — what it is — where the source lives — how to
   recognise its use (MCP prefix, script path, command) — [[card or backlog note]]`.
   A tool of the owner's that was used but is missing → add its line, or ask the owner
   in one short question if it is unclear whether they build it.
2. **Ledger.** `global/projects/tool-ledger.md` (create on first use), one section per
   tool. Per tool used this session:
   - **Use** — one line: what it did for this session, version if known.
   - **Friction** — only what is new, changed, or costly; one entry each:
     `- <YYYY-MM-DD> <tool@version> · <operation> · <expected → actual> · cost
     <time / retries> · workaround <…> · cause unknown|confirmed · status
     open|proposed|fixed — evidence <where>`.
   - **Idea** — when the work showed that a change to the tool would remove the
     friction: what to change and how to tell it worked.
   Recurring friction → bump the existing entry, not a new one. Project notes link to
   the ledger entry; the ledger owns the status.
3. **Escalate.** A friction seen ×3 across independent sessions, or once if it can
   lose or duplicate data, becomes one proposed task for that tool (Tier 3). Do not
   change the tool inside this pass unless the session's task is that tool.
4. **Surface next time.** When a ledger entry has a working workaround, give it a
   route keyed to that tool's name or operation (Step 5), so the next caller sees the
   conditional action before repeating the failure.

## Step 7: Report

One line per artifact made or patched, one line per proposal handed over, one line
for inbox counters that moved, one line per in-house tool touched (use, new friction,
proposed task). Nothing found — say so and stop.

## Common mistakes

- Automating a one-off: three real occurrences, not a hunch that it «will recur».
- Creating new when a patch would do — libraries of near-duplicates are how skills
  stop being read.
- Shipping an artifact without a trigger.
- Leaving a finding marked "ripe" for weeks — act, or record it as `proposed`.
- Writing a long ledger essay for every tool used — routine use is one line; detail
  is for new or costly friction.
- Teaching the agent to dodge a defect the owner could remove in the tool itself.
- Capturing «tool X is broken» — see Step 2.
- Letting this pass grow heavy. If it cannot finish in seconds on a clean session,
  cut it, not the session.
- Mixing in Symbiosis Brain feedback — noisy recall, tool friction, missed recall
  are about the memory system itself, not about repetition. Keep them out of this
  pass: `brain-save` Step 0 runs a separate self-critique pass after this one when
  the `brain-self-critique` skill is installed.
