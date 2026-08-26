# Symbiosis Brain Hooks

Claude Code hooks and status line for automatic context management. Bash is the
single source of truth — these `.sh` scripts are what `symbiosis-brain setup`
installs and what runs live. (The MCP server and recall engine are Python; only
the thin hook glue is bash.)

Every hook is **fail-open**: a broken payload, an unreadable temp dir or a missing
helper produces silence and exit 0, never a blocked tool call or a blocked prompt.

## Installed (7 hook events + status line)

| Script | Event(s) | Behavior |
|--------|----------|----------|
| **brain-session-start.sh** | SessionStart (`startup`, `resume`, `clear`, `compact`) | Inject CRITICAL_FACTS.md, resolve scope into env, reset per-session trigger flags, prewarm, report a failed vault sync |
| **brain-save-trigger.sh** | Stop / PreCompact / UserPromptSubmit | Proactive brain-save reminders + active recall + rules roster (mode arg selects behavior) |
| **brain-pre-action-trigger.sh** | PreToolUse (`Task\|Agent\|Edit\|Write\|MultiEdit\|NotebookEdit\|Bash\|PowerShell`) | Inject `[recall: N hits]`, `[action-rule …]` and route hints before a tool runs; runs from `$SYMBIOSIS_BRAIN_TOOLS` |
| **brain-save-marker.sh** | PostToolUse (`brain_write\|brain_append\|brain_patch`) | Record the current context % as the last-save baseline for the Stop-hook delta-guard |
| **brain-sync.sh** | SessionEnd | Commit the vault, `pull --rebase --autostash`, push; leave an alarm marker instead of resolving a conflict |
| **sb-statusline.sh** | statusLine | Wrapper: exports the data bridges, then renders row 1 (yours or ours) and row 2 (Symbiosis Brain state) |
| **sb-hooklib.sh** | *(sourced)* | `sb_tmp_dir` / `sb_session_id` — the fork-free parser sourced by `brain-session-start.sh`, `brain-save-marker.sh` and the three status-line scripts (`brain-save-trigger.sh` / `brain-pre-action-trigger.sh` still parse with grep+sed) |
| **sb-export.sh** | *(sourced)* | The single export point for the two status-line bridges |
| **sb-line.sh** | *(sourced)* | Row 2: scope, context %, save thresholds, rules zones, sync alarm |
| **sb-base-statusline.sh** | *(sourced)* | Default row 1 — replaced when you set your own status line |

`sb-statusline.sh` and everything it sources are **fork-free by contract**: Claude Code
debounces status-line updates and cancels the in-flight script, and on Windows/MSYS a
child caught mid-fork is stranded as a suspended orphan. Read stdin with a builtin,
match with bash regex, `source` instead of piping into a fresh shell.

## brain-save-trigger.sh

Three modes in one script:

| Mode | Event | Behavior |
|------|-------|----------|
| `stop` | Stop | Reminds to brain-save at **25/35/45%** context with delta-guard and SAVE_LATER support |
| `precompact` | PreCompact | Blocks compaction once for a last-chance save |
| `prompt-check` | UserPromptSubmit | Active recall (`[memory: …]`) + rules roster + relays a blocked-compaction reminder |

**Stop-mode design** (details in vault note `decisions/stop-hook-smart-trigger.md`):
- **Thresholds** `25 / 35 / 45%` — soft / serious / last-chance zones with escalating messages. Calibrated for the 1M-context envelope (sessions typically stay in 0–50%, quality degrades around 40%).
- **Delta-guard** `10%` — below the top zone, skip a trigger if context grew by less than this since the last save (avoids double-dipping after a recent save). The top zone ignores the delta-guard on purpose.
- **SAVE_LATER marker** — the user can postpone one soft-zone trigger by saying "потом"/"save later"; the top zone always fires.
- **Marker coordination** — `brain-last-save-pct-<sid>` is written by the PostToolUse hook `brain-save-marker.sh` on any successful `brain_write` / `brain_append` / `brain_patch`, not by the `brain-save` skill. The session id comes from the hook's own stdin, so resumed and forked sessions get a correct marker too. The marker survives `/compact`: SessionStart no longer wipes it, otherwise every compaction reset the delta to zero.

**Changing thresholds or delta-guard:** set env vars in `~/.claude/settings.json` —
`SYMBIOSIS_BRAIN_SAVE_THRESHOLDS` (default `25,35,45`) and
`SYMBIOSIS_BRAIN_SAVE_DELTA_GUARD` (default `10`). The hook reads them at runtime;
no script edit needed.

## Status line: your row 1, our row 2

`sb-statusline.sh` reads the status JSON once, exports the bridges, and only then
decides who renders row 1:

- set `SYMBIOSIS_BRAIN_USER_STATUSLINE_CMD` to your own command — it receives the same
  JSON on stdin and owns row 1;
- otherwise `sb-base-statusline.sh` renders the default row 1.

Row 2 (`sb-line.sh`) is always ours. **The bridges are exported before that fork in the
logic**, so bringing your own status line no longer costs you save reminders — which it
silently did until 2026-08.

### Bridge 1 — context percentage

`$SB_TMP/brain-context-pct-<session_id>` — one integer, rewritten on every status tick.
`brain-save-trigger.sh` reads it to decide whether a threshold was crossed, and
`brain-save-marker.sh` copies it into `brain-last-save-pct-<session_id>` on every save.
`SB_TMP` is `${TMPDIR:-${TEMP:-/tmp}}` — the same chain every hook uses.

### Bridge 2 — rate limits

`$SB_TMP/claude-rate-limits.json` (override with `SYMBIOSIS_BRAIN_RATE_LIMITS_FILE`,
disable with `SYMBIOSIS_BRAIN_RATE_LIMITS_DISABLED=1`). One line, rewritten on every
tick that carries limit data, and readable by any supervising agent:

```json
{"five_hour_pct":33,"resets_at":1787662800,"seven_day_pct":20,"ts":1787660714}
```

| field | meaning |
|---|---|
| `five_hour_pct` | percent of the 5-hour window consumed, integer |
| `seven_day_pct` | percent of the 7-day window consumed, integer |
| `resets_at` | unix seconds when the 5-hour window resets (`0` if the harness did not send it) |
| `ts` | unix seconds when the file was written |

The file is written only when the status JSON actually contains a `five_hour` block —
no data, no file, no stale numbers.

**Where to set the override.** `SYMBIOSIS_BRAIN_RATE_LIMITS_FILE` (and
`SYMBIOSIS_BRAIN_RATE_LIMITS_DISABLED`) must come from the environment — from
`env` in `~/.claude/settings.json`, or from the parent shell. Since 2026-08 they can no
longer be set by your row-1 command: `sb-statusline.sh` sources `sb-export.sh` *before*
the fork in the logic and marks it done with `export SB_EXPORT_DONE=1`, so by the time
your command runs both bridges are already on disk. That is the whole point of the
change — but it means a row-1 script that used to export this variable now has no effect
on it.

## Vault sync and its alarm marker

`brain-sync.sh auto` runs on SessionEnd: commit → `git pull --rebase --autostash` →
`git push`. Each git step is bounded by `SYMBIOSIS_BRAIN_SYNC_GIT_TIMEOUT` (default 15s),
which is why the SessionEnd hook is registered with a 40s budget. The script always
exits 0 — a failing sync must never hold the session hostage.

Failures leave two artifacts under `SB_TMP`:

- `brain-sync-errors.log` — appended: `<ISO8601> mode=<auto|manual> stage=<pull|push|conflict>`
  followed by `git status -sb | head -5`;
- `brain-sync-failed` — exactly one line: `stage=<pull|push|conflict> at=<ISO8601>`.

A conflict is never resolved automatically: the rebase is aborted, the tree is left
clean, and the marker is raised. The marker is shown in two places — a banner from
`brain-session-start.sh` at the next session start, and ` ⚠️sync:<stage>` at the end of
status row 2. A successful sync removes it.

`log.md` is the one file merged with `merge=union` (seeded into the vault's
`.gitattributes` at setup) — an append-only journal has no real conflicts.

## Installation

The supported path is `symbiosis-brain setup claude-code`, which copies these
hooks into `~/.claude/hooks/`, installs `~/.claude/commands/brain-sync.md`, wires the
seven events + statusLine into `settings.json`, and seeds the `SYMBIOSIS_BRAIN_*` env
block. `--repair` re-runs the same steps on an existing install and keeps the three
newest `.bak.*` copies of anything it overwrites.

Manual install:

```bash
mkdir -p ~/.claude/hooks
cp hooks/sb-hooklib.sh hooks/sb-export.sh \
   hooks/brain-session-start.sh hooks/brain-save-trigger.sh \
   hooks/brain-save-marker.sh hooks/brain-pre-action-trigger.sh hooks/brain-sync.sh \
   hooks/sb-statusline.sh hooks/sb-line.sh hooks/sb-base-statusline.sh ~/.claude/hooks/
chmod +x ~/.claude/hooks/*.sh
```

Then add the hooks + statusLine configuration to `~/.claude/settings.json` (see
the project root README.md). `symbiosis-brain doctor` checks that every required hook
is present.
