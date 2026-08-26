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

Typical yield: 0–2 artifacts per session. A clean session must end in seconds with
"nothing to learn" — a heavy pass will be skipped, and a skipped pass learns nothing.

## Step 1: Repetition sweep

Ask each question against THIS session. Note only concrete moments.

1. Did I make the same mistake twice — or once here and it is already a `mistake`
   note in the vault? (verify with a targeted `brain_search`)
2. Did the user correct me the same way twice? Corrections are first-class signals,
   not just memory material.
3. Did I write the same snippet or command sequence three times, with variations?
4. Do 3+ tool calls always travel together? That is a bundle («связка») waiting to
   be named.
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
- Anything that happened once.

## Step 3: Classify and size

| finding | artifact | tier |
|---|---|---|
| repeated mistake, command-shaped | action rule — `references/action-rule-recipe.md` | 1 |
| repeated mistake, judgment-shaped | patch the existing `feedback`/`pattern` note (new note only if none fits) | 1 |
| repeated action, no judgment inside | script — `references/automation-recipe.md` | 1 |
| repeated action needing judgment | skill draft — `references/automation-recipe.md` | 2 |
| bigger than a script or a skill («тянет на приложение») | proposal to the owner | 3 |

- **Tier 1 — do it, then report.** Small, reversible, obviously useful. The owner
  hears «сделано X для Y, лежит Z, триггер T» — enough to stay informed and to
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

Follow the matching recipe. Whatever the tier, the artifact is not done until:

- it has a **route** in `tool-routing.local.json` (prompt triggers and/or command
  triggers) so it surfaces in the moment it applies;
- a `pattern` note (or the patched note) links to it with `[[...]]`.

## Step 6: Count what is not ripe yet

A finding seen once or twice goes to `projects/autolearn-inbox.md` (create on first
use) as one line: `- YYYY-MM-DD ×N <what> — evidence: <moment>`. Same finding already
there → increment N and refresh the date. At ×3 it is ripe: act on it next pass.

## Step 7: Report

One line per artifact made or patched, one line per proposal handed over, one line
for inbox counters that moved. Nothing found — say so and stop.

## Common mistakes

- Automating a one-off: three real occurrences, not a hunch that it «will recur».
- Creating new when a patch would do — libraries of near-duplicates are how skills
  stop being read.
- Shipping an artifact without a trigger.
- Capturing «tool X is broken» — see Step 2.
- Letting this pass grow heavy. If it cannot finish in seconds on a clean session,
  cut it, not the session.
- Mixing in Symbiosis Brain feedback — noisy recall, tool friction, missed recall
  are about the memory system itself, not about repetition. Keep them out of this
  pass: `brain-save` Step 0 runs a separate self-critique pass after this one when
  the `brain-self-critique` skill is installed.
