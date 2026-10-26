---
name: brain-welcome
description: >
  Use when brain-init Step 0 finds <vault>/.sb-initialized absent on a new
  Symbiosis Brain installation, or the user explicitly requests re-onboarding.
---

# Brain Welcome — First-time Onboarding

## When to use

Invoked by `brain-init` Step 0 when `<vault>/.sb-initialized` is missing.
A user-typed `/brain-welcome` does not bypass the marker: if it exists, exit
silently. Re-onboarding requires explicit user authorization to reset it.

## Communication contract

- Speak the user's language throughout, including questions, summaries and
  saved profile text. The Russian examples below are models to adapt, not
  text to print verbatim. If the language is unknown, use the conversation's
  language; ask only if there is no usable signal.
- Be brief and practical. In Russian, use informal singular «ты». Use a
  similarly friendly, natural tone in other languages. Explain more only
  when the user asks or wants to learn; do not ask about answer style.
- Address any profession, not just developers. In user-facing text, say
  "notes", "memory", "work" and "folder" instead of internal names such as
  scope, vault, stack, config, JSON, PATH, graph, patterns or CRITICAL_FACTS.
  Use "settings" for configuration and "memory folder" for its location;
  never ask the user to learn those internal names. Explain an unavoidable
  technical term in parentheses. MCP and skill are acceptable only with a
  plain explanation of their benefit (for example, a skill is a ready-made
  instruction that helps the assistant handle a task). Keep identifiers
  unchanged; do not introduce technical terms just to explain them.
- Promise only supported behavior: saved notes can be retrieved, not perfect
  recall of every conversation or automatic transfer between all projects.
  Local note storage is not a promise that the assistant runs offline.

## Procedure

### Step 0 — Check before starting

If `<vault>/.sb-initialized` exists, exit silently. Resolve the vault through
`brain-init`; do not guess a different location.

Read `CRITICAL_FACTS.md` if it exists. Preserve it: do not overwrite an existing
profile during onboarding. Use its contents for the summary in Step 3 instead
of asking the user to repeat the introduction.

### Step 1 — Explain what changed (no question)

Give a short introduction. Russian example:

> Теперь у нас есть память между разговорами. Я сам сохраняю важное по ходу
> работы и сам вспоминаю нужное перед задачей — тебе ничего делать
> не обязательно. Если хочешь что-то точно запомнить — просто скажи.
> Тебе не придётся каждый раз объяснять всё заново.
>
> Заметки хранятся в папке на твоём компьютере. Их можно открыть и прочитать.

Do not imply that every detail is saved or that another project's notes will
always surface automatically. Do not claim that nothing leaves the computer:
notes retrieved by the assistant may be included in its model context, and
storage or synchronization depends on the user's setup.

### Step 2 — Get acquainted (one open question)

Skip this question if a profile already exists. Otherwise ask, for example:

> Давай познакомимся. Чем ты занимаешься и с чем тебе сейчас нужна помощь?
> Хватит пары предложений.

Wait for the answer. Use only stated facts; do not infer a profession,
expertise, preferences or strengths. Do not require tools or technologies
from someone who has not mentioned them. If the answer is unclear, ask one
short clarification at a time. After three unsuccessful clarifications,
offer to continue with an incomplete profile; do not invent missing details.

### Step 3 — Reflect understanding, then save

Summarize what was understood in one or two natural sentences and ask whether
it is right. Build the summary ONLY from this user's own answer. Shape:

> Ты <чем занимается — его словами> и хочешь, чтобы я помогал с <его задача>.
> Я правильно понял?

The lines below show tone and length only. They are deliberately different
people; never copy their professions, tasks or details into a real summary,
and never assume the user resembles any of them:

> Ты бухгалтер и хочешь быстрее разбирать таблицы с отчётами. Я правильно понял?
> Ты проектируешь инженерные сети и хочешь, чтобы я помогал проверять чертежи. Верно?
> Ты учишься на дизайнера и хочешь разобраться с программированием сайтов. Так?

Wait for confirmation or corrections. Incorporate corrections before writing.
This is a check of understanding, not a report that an internal file was
created. If a profile already exists, summarize it and offer corrections
through `brain-save` only if the user requests them; skip this step's write.

For a new profile, write `CRITICAL_FACTS.md` via `brain_write` with
`scope: global`, `type: wiki`, `tags: [user-profile, bootstrap, critical]`,
`title: Critical Facts — User Profile`, and `gist: <summary, at most 80 chars>`.
Keep the body at most 150 tokens: stated role, current work and relevant
facts in the user's language. Include tools or strengths only if supplied.
If the user agreed to an incomplete profile, mark missing information with
`<!-- TODO -->`. After a successful write, a short "Запомнил" is enough.

If saving fails, report it plainly and leave the marker absent so onboarding
can retry. A tool error may occur after the file was written: read the file
before retrying rather than overwriting it blindly.

### Step 4 — Show three useful phrases (no extra tour gate)

Explain the three actions in a few lines. Russian example:

> Можно говорить обычными словами:
> - «Запомни…» (`/brain-save`) — сохраним важное на будущее.
> - «Что ты помнишь про…» (`/brain-recall`) — поищем в наших заметках.
> - «Забудь…» — уберу из заметок то, что скажешь.
>
> Например: «Запомни: отвечай кратко, без жаргона» (`/brain-save`).

Present slash identifiers as optional ready-made instructions for the same
request, not commands the user must learn. Explain "skill" if that word is
used; do not require terminology to remember a communication preference.
There is no dedicated forget skill. Do not invent one or promise erasure of
chat history, backups or synced copies. For a forget request, find the
relevant note and clarify exactly what should be removed; use the actual
Brain edit/delete operation with its safety checks and user authorization.
Do not introduce an unimplemented help command. If the user wants more,
explain using concrete examples rather than internal routing mechanics.

### Step 5 — Offer Obsidian (one yes/no question)

Detect whether Obsidian is installed:
- macOS: `/Applications/Obsidian.app` exists.
- Linux: `which obsidian` or `~/.local/share/Obsidian/` exists.
- Windows: `~/AppData/Local/Obsidian/Obsidian.exe` or registered vaults JSON.

If not installed, ask, for example:

> Хочешь установить Obsidian — бесплатное приложение, где можно читать наши
> заметки и смотреть красивую карту связей между ними? Я помогу подключить
> папку с памятью.

If already installed, offer to open the memory folder in it, explaining the
same benefit. Do not discourage using it. Wait for an affirmative answer
before installing, registering or opening anything; silence is not consent.
If the user declines, skip silently and finish onboarding.

On installation consent, use the platform installer:
`winget install Obsidian.Obsidian` / `brew install --cask obsidian` /
`snap install obsidian`. Do not promise a fixed size or installation time.
If installation fails, offer `https://obsidian.md/download` for manual
installation; this optional failure does not block onboarding.

When installed and the user accepted opening it, preserve existing entries
while adding the vault path to Obsidian's vaults registry JSON. Do not
replace an unreadable registry blindly. Then open
`obsidian://open?vault=<vault-name>&file=CRITICAL_FACTS` through
`xdg-open` / `open` / `start`, encoding URI parameters as needed. Describe
what opens as "our notes" or "your introduction", not the internal filename.
If opening fails, explain briefly; the saved memory remains usable.

### Finish — Write the initialization marker

On the success path, write `<vault>/.sb-initialized` with the current ISO
timestamp. Do this only after the profile was saved successfully or an
existing profile was preserved, and the optional Obsidian step was handled.
The marker prevents repeating onboarding in the next conversation.
End with one short optional offer in the user's language, for example:

> Если хочешь, расскажу подробнее, как пользоваться памятью.

Do not start a detailed tutorial or require an answer to this offer.

## Idempotency / recovery

- A crash before completion leaves the marker absent, so the next session
  retries. Reuse an existing profile without overwriting it.
- A present marker means a silent exit, including on manual invocation.
- Optional Obsidian installation/opening failure does not discard saved
  facts or prevent writing the marker.
- Never report a successful save or completed initialization when its write
  failed. Keep technical recovery details out of the user's introduction.
