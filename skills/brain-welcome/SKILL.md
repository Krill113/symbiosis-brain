---
name: brain-welcome
description: >
  First-time onboarding for a new Symbiosis Brain installation. Triggered ONLY
  by brain-init Step 0 when <vault>/.sb-initialized is absent. Shows pitch,
  collects user introduction → CRITICAL_FACTS.md, optionally offers tour and
  Obsidian. Writes .sb-initialized marker on success — never re-runs.
---

# Brain Welcome — First-time Onboarding

## When to use

NEVER call directly. Invoked exclusively by `brain-init` Step 0 when
`<vault>/.sb-initialized` is missing. User-typed `/brain-welcome` is
allowed only for explicit re-onboarding.

## Procedure

### Step 1 — Pitch + capability (no question)

Print verbatim (Russian — do NOT translate):

````
🧠 Symbiosis Brain установлен. Теперь у нас общая память.

Раньше каждая сессия начиналась с нуля — я не помнил твой стек,
не знал прошлых решений, не видел связей между проектами. С этой
минуты:

  • контекст держится сам — стек, привычки, домен; пересказывать
    себя каждый раз больше не надо
  • прошлые решения и болезненные уроки всплывают там, где
    нужны, ДО того как ты о них вспомнишь
  • знание перетекает между проектами — что разобрали в одном
    репозитории, не теряется в другом

Команды по запросу: /brain-recall, /brain-save, /brain-help.
````

### Step 2 — Знакомство (1 open question)

Print:

````
Чтобы это работало с первого дня — давай познакомимся. Расскажи
в нескольких предложениях:

  • кто ты по работе, какой стек, в какой области
  • над чем мы сейчас сидим — главные проекты, задумки

Это нужно один раз. Дальше пересказывать не придётся — ничего
не пропадёт.
````

Wait for answer. Then write `wiki/CRITICAL_FACTS.md` via `brain_write` with
frontmatter (`scope: global`, `type: wiki`, `tags: [user-profile, bootstrap, critical]`,
`title: Critical Facts — User Profile`, `gist: <≤80 chars summary>`) and body
distilled from the user's answer (≤150 tokens, role + stack + active projects + key strength).

If parsing the answer fails three times, write a skeleton with `<!-- TODO: дополни -->`
and proceed.

Print confirmation:

````
✓ Запомнил. Карточка CRITICAL_FACTS.md создана — буду обращаться
к ней в любой сессии.
````

### Step 3 — Опциональный tour (1 y/n)

Print:

````
Хочешь сейчас разобрать пару паттернов работы со Symbiosis Brain —
когда я вспоминаю сам, а когда лучше попросить руками? Минут 5.
Или продолжим текущим, наберёшь /brain-help когда понадобится.
````

If "да" / "yes" / "y" — give a 4-bullet tour:
- `recall` перед задачами (`brain_search` + `brain_read`)
- `save` после insights (skill `brain-save`)
- scope-routing (CWD basename → scope; CLAUDE.md marker override)
- Obsidian-граф (если установлен)

If "нет" / no / blank / anything else — proceed silently.

### Step 4 — Опциональный Obsidian (1 y/n)

Detect Obsidian:
- macOS: `/Applications/Obsidian.app` exists
- Linux: `which obsidian` or `~/.local/share/Obsidian/` exists
- Windows: `~/AppData/Local/Obsidian/Obsidian.exe` or registered vaults JSON

If NOT installed, print:

````
Кстати, заметка CRITICAL_FACTS.md, которую я только что записал —
уже лежит в твоей папке. Хочешь увидеть её как граф связей?

Установлю Obsidian (бесплатный, ~50 МБ), подключу твою папку
заметок, и сразу открою файл. Минута дела. [Y/n]
````

If "Y": run platform install (`winget install Obsidian.Obsidian` / `brew install --cask obsidian` / `snap install obsidian`).
If install fails — open `https://obsidian.md/download` in browser, ask user to install
manually, then proceed silently.

If installed (after install or pre-existing), update Obsidian's vaults registry
JSON to include the vault path, then open
`obsidian://open?vault=<vault-name>&file=CRITICAL_FACTS` via `xdg-open`/`open`/`start`.

If user declines — skip silently.

### Финал — write `.sb-initialized` marker

Always at the end (success path). Write `<vault>/.sb-initialized` with current ISO timestamp.
brain-welcome never runs again on this vault.

## Idempotency / recovery

- If skill crashes during Step 2, marker is NOT written → next session retries.
- If marker present, skill exits silently on Step 0 — even when invoked manually.
- If `wiki/CRITICAL_FACTS.md` already exists when skill runs → skip Step 2's write,
  re-confirm to user: "вижу что CRITICAL_FACTS уже есть, обновим вручную если нужно".
