# Recipe: repeated action → script, skill, or proposal

## 1. Pick the form by what is inside the repetition

| inside the repeated action | form | tier |
|---|---|---|
| deterministic steps, fixed inputs → fixed output | **script** | 1 — do, report |
| steps that need judgment each time (what to look at, how to phrase) | **skill** | 2 — draft, show, install on OK |
| state, UI, several moving parts, more than a day of work | **proposal** | 3 — discuss first |

When unsure between script and skill: a script that needs a paragraph of «when to
use» is a skill; a skill whose body is one command is a script.

## 2. Script (tier 1)

- Location: your own tooling directory (`<workspace>/tools/<class-level-name>.<ps1|py|sh>`)
  for cross-project tooling; the target repo's `tools/` when it belongs to one project.
- Windows default: PowerShell or Python (bash tool here is Git-bash; MSYS mangles
  `/flags` and paths). Bash only for hook-adjacent work.
- Shape: arguments, `--help` / comment header with purpose and an example call,
  idempotent, no hidden state, exits non-zero on failure. Re-run it once on real
  input before reporting — a script that ran zero times is a guess, not a tool.
- Register: a prompt-trigger route in `tool-routing.local.json` (`class: "augment"`,
  `expected_tool: null`, `hint` names the script and when to reach for it) **and** a
  `pattern` note in the vault linking the file. Without the route it will be
  rewritten by hand again.
- Report: `made <script> for <repetition>, lives at <path>, trigger <route id>`.

## 3. Skill (tier 2)

Follow `skill-creator` / `superpowers:writing-skills`: baseline run **without** the
skill on a realistic prompt, then with it, compare, show both to the owner, install
to `~/.claude/skills/` only on OK. Keep the dev copy under version control — the
installed copy and the source drift otherwise. Description: «Use when …», triggering
conditions only,
under ~500 characters. Register a route the same way as for a script.

## 4. Proposal (tier 3)

Write `research/<date>-proposal-<name>.md` in the vault (type `research`,
scope of the project it serves):

- **Problem** — the repetition, with 3+ concrete moments and their cost.
- **Shape** — inputs, outputs, where it would live, what it would replace.
- **Why more than a script** — the state/UI/integration that pushes it past tier 1–2.
- **Open questions** for the owner.

Then tell the owner in two lines and stop. If approved: `superpowers:brainstorming`
→ `superpowers:writing-plans` → a verified build. Never start the build inside the
autolearn pass.

## 5. Always

- Patch before create (SKILL Step 4). A second near-identical script is worse than
  one script with a flag.
- Name at class level: `extract-workflow-result`, not `fix-todays-json`.
- Nothing ships without a trigger and a note that links it.
