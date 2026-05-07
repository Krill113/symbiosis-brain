---
name: brain-save
description: >
  Save knowledge to Symbiosis Brain with quality gates and deduplication. Use when you've
  learned something important — decisions, patterns, mistakes, user preferences, research
  findings. Also use when the user asks to remember something. SHOULD trigger proactively
  after significant work, not only when explicitly asked.
  MUST also trigger at session end or after completing large multi-step tasks —
  does a quick retrospective self-scan for dead ends, workflow improvements,
  and missed user signals before saving.
---

# Brain Save — Knowledge Preservation

## When to use

Trigger proactively (don't wait for the user to ask) when:
- User corrected your approach or pointed out an error
- Debugging completed and root cause was found
- An architectural or design decision was made
- A non-trivial bug or gotcha was discovered
- A brainstorming session produced actionable decisions
- Expert information was received from a third party
- The user explicitly asks to remember something
- **Session ending** or a large multi-step task just completed (see Step 0)

## Procedure

### Step 0: Retrospective (session end / large task completed)

If triggered at session end or after a multi-step task — do a quick self-scan before Step 1:

- **Dead ends / wasted effort?** Wrong path tried, unnecessary retries → save as `mistake`
- **Workflow improvement?** Subagent strategy that worked, better tool usage → save as `pattern`
- **Implicit user signals missed?** User rephrased, showed frustration, had to redirect → save as `user`
- **Tool/API surprise?** Unexpected behavior worth remembering → save as `pattern`
- **Insights worth keeping?** Brainstormed ideas, third-party suggestions, design alternatives, project ideas — anything useful for future work that isn't an error → save as `decision` / `pattern` / `wiki` / `research` (pick by content type)
- **Session-close / parallel-session handoff?** Triggers: explicit "закрываем" / "запомни на чём остановились" / "продолжим в другом окне", or context > 70% after substantive work, or before starting parallel session in another window → update `## Handoff YYYY-MM-DD` section in `projects/<scope>.md` per [[wiki/handoff-pattern]] (4 bullets: shipped / WIP / parked / next-step). Same-day re-trigger → overwrite that block, don't duplicate.

Most sessions produce 0 findings. Don't force it. If nothing stands out — skip to end.
Each finding that passes the scan goes through Steps 1–7 as usual.

### Step 1: Quality gate

Ask yourself: **"Is this needed in a FUTURE session? Can it be easily derived from code, git, or docs?"**

- If NO to the first or YES to the second → don't save. Stop here.
- Save: decisions with reasoning, patterns, mistakes/gotchas, user preferences, research findings, cross-project relationships.
- Don't save: file structure (read code), task progress (use tasks), git history (use git log), anything already in CLAUDE.md.

### Step 2: Deduplication check — BLOCKING

**STOP before `brain_write`.** Run `brain_search` with the topic keywords. This is not optional and not a courtesy — duplicate notes degrade graph queries and split context across files.

Decision tree:

- **Existing note covers >60% of the same topic** → `brain_patch` or `brain_append` to update it. Do NOT create a new file.
- **Related but distinct note exists** → create new, but the new note MUST `[[link]]` to the existing one (graph-connectedness gate, see Step 4).
- **Nothing close** → continue.

**Common failure mode** (see [[feedback/symbiosis-brain-usage-self-critique-2026-05-07]]): under flow pressure ("я уже знаю что писать"), this step gets skipped. Don't skip. The cost of a 2-second `brain_search` is far below the cost of a duplicate that fragments knowledge.

If you're saving in a session where you already searched on the same topic in Step 1 of `brain-recall` — that result counts; you don't need to re-search.

### Step 3: Determine metadata

- **type:** `decision` (choice with reasoning), `pattern` (recurring approach), `mistake` (bug + fix), `research` (investigation findings), `user` (preferences), `project` (facts/context), `reference` (external pointers)
- **scope:** project name (e.g. `beta`, `alpha`, `widgetcompare`) for project-specific knowledge. `global` for cross-project knowledge.
- **tags:** 2-5 keywords
- **valid_from:** today's date. Always set.
- **provenance:** if the information came from a specific person or source, note it. Format: "Source: [who] told [what] during [context]"

### Step 4: Wiki-links

Include minimum 2 `[[entity]]` references to existing entities in the vault. Isolated notes are useless — they won't appear in graph traversal.

### Step 4.5: Write gist field

Сформируй `gist:` — 1 строка, ≤80 символов, отвечает на «зачем эта заметка существует».

**Правила:**
- Не дублируй title.
- Не пиши «что лежит» (содержательный gist) — пиши «что эта заметка позволяет» (функциональный gist).
- Если оставил пустым — `brain_write` вернёт warning. Используется для mid-conversation recall.

**Шаблоны по типам:**

| Тип | Что писать в gist |
|---|---|
| `project` | Что за проект, чем отличается (не roadmap) |
| `decision` | Что решили + ключевая причина |
| `pattern` | Когда применять + что даёт |
| `mistake` | Что пошло не так + почему помнить |
| `research` | Что искали + основной вывод |
| `feedback` | Что заметил пользователь + что менять |
| `user` | Какой принцип/профиль (1 фраза) |
| `wiki` | Что узнаёт читатель |
| `reference` | Куда смотреть и зачем |

Передай в `brain_write` параметр `gist`:

```
brain_write(path=..., title=..., body=..., gist="...", ...)
```

### Step 5: Apply depth rules

- **Global scope** → brief: what it is, why it matters, what it connects to. Enough to REMEMBER and know WHERE to look.
- **Project scope** → detailed: specific decisions, architecture, nuances. But only what can't be learned from reading the code.

### Step 6: Save

Call `brain_write` with all metadata.

### Step 7: Confirm

Output 1 line: what was saved and where. Example: "Saved: decision about X → decisions/x-approach.md"

### MCP fallback

If `brain_write` is unavailable, save to auto-memory (MEMORY.md) with a note:
"[Temporary — migrate to Symbiosis Brain when available]"

### Dogfooding

If you notice friction, bugs, or improvement ideas while using Symbiosis Brain,
add them to `projects/symbiosis-brain-backlog.md` via `brain_write`. Don't force analysis — just note real observations.
