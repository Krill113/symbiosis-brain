---
scope: symbiosis-brain
tags:
- scope
- taxonomy
title: Scope Taxonomy
type: reference
gist: Whitelist валидных scope-ов; правила добавления новых
---

Whitelist валидных scope-ов. Дополняется по мере онбординга проектов через
`brain-project-init`.

Правило в одну строку: **если проект удалят — `scope=global`; иначе — конкретный
проект, не зонтик.**

## Whitelist

| scope | kind | описание | проект-карточка |
|---|---|---|---|
| `global` | base | глобальные правила и принципы | — |
| `symbiosis-brain` | product | сам Symbiosis Brain | [[symbiosis-brain/symbiosis-brain]] |

## Антипаттерны

- ❌ Зонтичный scope для проект-специфики.
- ❌ Не-kebab-case scope (`fooBar` ≠ `foo-bar` для фильтра).
- ❌ Новый scope без записи в эту таблицу.
- ❌ `scope=global` для проектной specifics.

## Folder ↔ type convention

Папка — источник истины: `type:` заметки обязан совпадать с папкой, в которой она
лежит (внутри скоупа это `<scope>/<папка>/`). Исключение помечается явным
`allow_type_mismatch: true` во frontmatter — тогда правило для этой заметки не
проверяется.

| folder | type |
|--------|------|
| `archive/` | `project` |
| `decisions/` | `decision` |
| `patterns/` | `pattern` |
| `projects/` | `project` |
| `wiki/` | `wiki` |
| `feedback/` | `feedback` |
| `mistakes/` | `mistake` |
| `research/` | `research` |
| `user/` | `user` |
| `reference/` | `reference` |
