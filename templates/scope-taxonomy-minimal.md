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
| `symbiosis-brain` | product | сам Symbiosis Brain | [[projects/symbiosis-brain]] |

## Антипаттерны

- ❌ Зонтичный scope для проект-специфики.
- ❌ Не-kebab-case scope (`fooBar` ≠ `foo-bar` для фильтра).
- ❌ Новый scope без записи в эту таблицу.
- ❌ `scope=global` для проектной specifics.
