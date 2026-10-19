---
name: brain-project-init
description: >
  Onboard a new project scope into Symbiosis Brain. Creates the project card
  in vault, appends a row to scope-taxonomy.md, and writes a marker into
  <project>/CLAUDE.md. Triggered by brain-init skill — never invoke directly.
  Three modes: full (default), recovery (card lost), migration (marker version >= 2).
---

# Brain Project Init — Onboarding

## Client compatibility

Names such as `brain_write`, `brain_search`, and `brain_sync` below are logical
Brain operation names. Use the actual exposed tool with the matching operation;
the MCP prefix differs by client. Preserve every onboarding, lock, recovery, and
idempotency step.

### Codex

Use native skill invocation when this skill is exposed by the host. Otherwise
resolve the canonical root with `symbiosis-brain resources --skills` (or the
host-supported `$CODEX_HOME/skills`), read its canonical `SKILL.md`, and execute
the original ritual rather than inventing a `Skill` tool. For lock and scope
commands, prefer the installed `brain-cli`; if unavailable, use the current
interpreter as `"<absolute current interpreter>" -m symbiosis_brain.scope_cli`. Do not use
`uv run --directory` for a wheel-only install. Prefer explicit
`SYMBIOSIS_BRAIN_VAULT` and `SYMBIOSIS_BRAIN_TOOLS`.

### OpenCode

The package `skillsDir` is registered through `skills.paths`; invoke this skill
natively or read its canonical `SKILL.md` from that resource and execute the
original ritual. Use `SYMBIOSIS_BRAIN_OPENCODE_PYTHON -m
symbiosis_brain.scope_cli` for lock and scope commands, with
`SYMBIOSIS_BRAIN_OPENCODE_VAULT` and `SYMBIOSIS_BRAIN_OPENCODE_TOOLS` for paths.
Do not use `uv run --directory`.

### Claude Code (default)

Keep the existing Claude behavior below unchanged, including its Bash, `uv`,
MCP, and Edit-tool paths.

## When to use

NEVER call this directly. It is invoked by `brain-init` Step 3.1, Step 5,
or Step 5 (migration / recovery branches). User-typed `/brain-project-init`
is allowed only for explicit re-onboarding.

## Inputs

The invoking skill or user passes:
- `scope` — required (already normalized).
- `project_path` — required (CWD usually).
- `mode` — `full` (default) | `recovery` | `migration`.
- `umbrella` — optional, pre-filled in recovery/migration.

## Procedure

### Step 1: Acquire lock

**Native clients:**

- **Codex:** run `brain-cli acquire-onboard-lock "$SCOPE" --timeout-s 120`.
  If unavailable, run `"<absolute current interpreter>" -m
  symbiosis_brain.scope_cli acquire-onboard-lock "$SCOPE" --timeout-s 120`.
- **OpenCode:** run `"$SYMBIOSIS_BRAIN_OPENCODE_PYTHON" -m
  symbiosis_brain.scope_cli acquire-onboard-lock "$SCOPE" --timeout-s 120`.

**Claude Code default:**

Run via Bash with **120s** timeout (onboarding can be slow on large vaults):

```bash
uv run --directory <symbiosis-brain-tools-path> brain-cli acquire-onboard-lock "$SCOPE" --timeout-s 120
```

(Fallback if `brain-cli` not on PATH: `uv run --directory <path> python -m symbiosis_brain.scope_cli acquire-onboard-lock "$SCOPE" --timeout-s 120`.)

Exit codes:
- `0` — got the lock, proceed.
- `1` — busy (another session is onboarding). See "busy" path below.
- `2` — lockdir unwritable (filesystem error). Tell the user: "Не могу создать lockfile в `<temp>/symbiosis-brain-onboard-<scope>.lock` — проверь права на запись в TEMP. Онбординг отложен." Exit silently.

**Busy path (exit 1):**
- Wait 5 seconds: `sleep 5`.
- Re-run `brain-cli scope-resolve "$PROJECT_PATH"`. If `source=marker_v1` now, another session finished — exit silently.
- Otherwise: tell the user "Параллельная сессия делает онбординг этого scope, ждём", then sleep 10 more, then re-check. After two failed waits — give up: emit one warning line and exit. Do not double-onboard.

### Step 2: Ask the user (mode=full only)

Skip this step if `mode=recovery` (we already have answers in CLAUDE.md marker / dead vault history) or `mode=migration` (we're upgrading marker, content unchanged).

> 🆕 Новый scope `<X>`. Расскажи коротко:
> 1. Зачем этот проект, какая общая задумка?
> 2. Это самостоятельный проект или член зонтика? (если зонтика — какого)
> 3. Это shared-library (от вас зависят), consumer (вы зависите), или standalone?
> 4. Если shared — какие siblings зависят? Если consumer — от чего зависите?

If user says "пропусти" / "потом" / "skip" / explicitly defers — switch to draft mode:
- Whyless artifacts: `## Зачем\n\nTODO`.
- Marker gets `status=draft`.
- Taxonomy row gets `<TODO: описание>` placeholder.

### Step 3: Create vault project card

Use `brain_write`:

```python
brain_write(
    path=f"{scope}/{scope}.md",
    title=<project name from PROJECT_PATH basename>,
    note_type="project",
    scope=scope,
    tags=["bootstrap"],
    body=<full body — see template below>,
)
```

**Body template** (replace placeholders):

```
---
scope: <scope>
umbrella: <umbrella or omit field>
type: project
title: <title>
role: <standalone | shared-library | consumer>
dependents: [<list>]
dependencies: [<list>]
---

## Зачем

<answer to question 1, or TODO in draft mode>

## Связи

<built from answers 3-4: who depends on us, who we depend on>
```

Frontmatter notes:
- `umbrella` field is omitted entirely (not empty string) when project is standalone.
- `role` defaults to `standalone` when user gave no answer.
- Empty arrays serialise as `[]`.

In `mode=recovery`: try `brain_search` for the same scope first. If a previous draft exists, merge `## Зачем` / `## Связи` instead of overwriting.

### Step 4: Append row to scope-taxonomy

**Native clients:** use the actual native file-edit capability for the targeted
table insertion below; do not issue a fake `Edit` tool call. Claude Code keeps
the original Edit behavior.

Read `vault/reference/scope-taxonomy.md`. Find the `## Whitelist` table. If a row already starts with `` | `<scope>` `` → leave it (idempotent, no duplicate). Otherwise, append a row matching the existing column layout:

```
| `<scope>` | <kind> | <one-line description from answer 1> | [[<scope>/<scope>]] |
```

`<kind>` defaults to `product`; use `umbrella-member` if `umbrella` is set, `umbrella` if this scope is itself a parent. Use Edit tool — DO NOT call `brain_write` (that creates new note). Insert the new row alphabetically, before the `## Антипаттерны` heading.

### Step 5: Append marker to <project>/CLAUDE.md

**Native clients:** use the actual native file-edit capability to read, create,
or append the marker; do not issue a fake `Edit` tool call. Claude Code keeps the
original Read/Edit behavior.

Read `<project_path>/CLAUDE.md` (or create with `# <ProjectName>\n` header if absent — never overwrite existing content).

If file already contains a `<!-- symbiosis-brain v` marker → exit (idempotent). Otherwise append at EOF with one blank line separator:

```

<!-- symbiosis-brain v1: scope=<scope>, umbrella=<umbrella>, status=<status?> -->
```

`umbrella` and `status` fields are omitted entirely when null (no `, umbrella=` segment, no `, status=` segment).

### Step 6: Sync

Call `brain_sync` to refresh the SQLite index for the new card.

### Step 7: Release lock

**Native clients:**

- **Codex:** run `brain-cli release-onboard-lock "$SCOPE"`. If unavailable, run
  `"<absolute current interpreter>" -m symbiosis_brain.scope_cli
  release-onboard-lock "$SCOPE"`.
- **OpenCode:** run `"$SYMBIOSIS_BRAIN_OPENCODE_PYTHON" -m
  symbiosis_brain.scope_cli release-onboard-lock "$SCOPE"`.

**Claude Code default:**

Run via Bash:
```bash
uv run --directory <symbiosis-brain-tools-path> brain-cli release-onboard-lock "$SCOPE"
```

Always run this — even on errors. Use a `trap`-equivalent: wrap Steps 3-6 such that lock is released on any exit path.

### Step 8: Brief confirmation

Single line:
> ✓ Scope `<scope>` зарегистрирован. Карточка [[<scope>/<scope>]], маркер в CLAUDE.md.

In draft mode:
> ✓ Scope `<scope>` зарегистрирован в режиме draft. Дополни `## Зачем` в [[<scope>/<scope>]] когда руки дойдут.

Do NOT continue dialogue. Control returns to brain-init.

## Idempotency

The skill is safe to re-run:
- Lock prevents concurrent invocations.
- Each artifact write checks for existing content first.
- `brain_sync` is naturally idempotent.

## Error recovery

If Step 3, 4, 5, or 6 fails, the lock MUST still be released (Step 7). The skill is allowed to leave partial state — re-running with the same scope picks up where it left off because each step is idempotent.
