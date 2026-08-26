# Recipe: repeated mistake → action rule

An action rule fires a warning **at the moment you type a risky command**, before it
runs. It lives in `$SYMBIOSIS_BRAIN_VAULT/tool-routing.local.json`, is compiled to
`.index/action-rules.tsv`, and is matched by the PreToolUse hook for Bash and
PowerShell. Warning only — never a block.

## 1. Is it command-shaped?

The lesson must attach to a command the agent types itself (`uv tool install …`,
`Stop-Process -Name …`, `git reset --hard origin/…`). A judgment lesson («delegate
research», «verify before asserting») has no command to catch — patch a
`feedback`/`pattern` note instead and stop here.

## 2. Precision first

One rule firing on innocent commands every few sessions is worse than no rule:
banner blindness kills the whole mechanism. Design for the **rare, expensive**
command. Reject the rule if it cannot be separated from everyday commands.

## 3. Write the regex — POSIX ERE, not PCRE

The hook uses `grep -E`. That means:

- `[[:space:]]`, not `\s`; `\b` works (GNU grep); **no lookahead/lookbehind**
  `(?!…)` — it never matches anything and fails silently.
- Anchor to a command start: `(^|[;&|]+)[[:space:]]*cmd…` — kills most false
  positives from comments, `echo`, commit messages and greps over the very note that
  taught you the lesson.
- Cover the idioms: `Get-Process X | Stop-Process` as well as `Stop-Process -Name X`;
  `git -C dir …`; flags in either order.
- Multi-line commands are matched line by line — anchor per line.
- **Several patterns per tool side are fine.** The compiler validates a side as a
  union: every `test_match` vector must be caught by at least one pattern of that
  side, and no `test_nomatch` vector may be caught by any of them. A pattern that
  catches none of its own side's vectors is reported in
  `meta.json → unmatched_patterns` as a warning — the rule still compiles. Prefer one
  readable pattern per shape over a hand-fused alternation.

## 4. Route shape (append to the array in `tool-routing.local.json`)

```json
{"id": "kebab-case-class-level", "class": "action", "priority": 50,
 "command_triggers": {"bash": [{"re": "..."}], "powershell": [{"re": "..."}]},
 "hint": "Императив, 1–2 строки, что проверить/сделать иначе. [[mistakes/source-note]]",
 "test_match":   {"bash": ["dangerous variant", "another idiom"], "powershell": ["..."]},
 "test_nomatch": {"bash": ["innocent lookalike", "grep for the note text", "echo mention"], "powershell": ["..."]}}
```

- `id`: `[A-Za-z0-9._-]+`, class-level name.
- A tool side without `test_match` vectors is **not compiled** — two positives and
  two negatives per side, minimum. Include the classic false-positive shapes: a grep
  over the note, an `echo`/comment repeating the phrase, the safe sibling command.
- `priority`: higher fires first when two rules match the same command.
- Never hand-edit JSON unsafely: write a fragment file, merge with a JSON round-trip,
  keep a `.bak-<date>` of the previous file. If the shell path-guard trips on
  `Remove-Item` + regex classes in one command — that is itself a known rule; split
  the steps.

## 5. Compile and prove it

```bash
uv run --directory "$SYMBIOSIS_BRAIN_TOOLS" python -m symbiosis_brain compile-action-rules --vault "$SYMBIOSIS_BRAIN_VAULT"
cat "$SYMBIOSIS_BRAIN_VAULT/.index/action-rules.meta.json"
# `skipped` must be [] and `unmatched_patterns` must be [] — or read the reason
# before shipping the rule.
```

Then a live smoke through the hook with a fake payload. Point the hook at a
**throwaway vault**: a smoke hit is still a hit, and `.index/action-rule-hits.jsonl`
is the recidivism metric — do not seed it with your own tests.

```bash
SMOKE_VAULT=$(mktemp -d)
cp -r "$SYMBIOSIS_BRAIN_VAULT/.index" "$SMOKE_VAULT/.index"
echo '{"tool_name":"Bash","tool_input":{"command":"<dangerous>"},"session_id":"smoke"}' \
  | SYMBIOSIS_BRAIN_VAULT="$SMOKE_VAULT" bash "$SYMBIOSIS_BRAIN_TOOLS/hooks/brain-pre-action-trigger.sh"
```

A hit prints `{"hookSpecificOutput":…"additionalContext":"[action-rule <id>] …"}`;
an innocent command prints nothing. Hits are logged to
`.index/action-rule-hits.jsonl` — that log is the recidivism metric.

## 6. Report

`сделано правило <id> для <ошибка> (источник [[mistakes/…]]), ловит <bash/powershell>,
скомпилировано N/N` — one line to the owner.
