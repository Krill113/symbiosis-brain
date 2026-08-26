# Symbiosis Brain Hooks

Claude Code hooks and status line for automatic context management. Bash is the
single source of truth — these `.sh` scripts are what `symbiosis-brain setup`
installs and what runs live. (The MCP server and recall engine are Python; only
the thin hook glue is bash.)

## Installed (6 hook events + status line)

| Script | Event(s) | Behavior |
|--------|----------|----------|
| **brain-session-start.sh** | SessionStart (startup + compact) | Inject CRITICAL_FACTS.md, resolve scope into env, reset per-session trigger flags, prewarm |
| **brain-save-trigger.sh** | Stop / PreCompact / UserPromptSubmit | Proactive brain-save reminders + active recall + rules roster (mode arg selects behavior) |
| **brain-pre-action-trigger.sh** | PreToolUse (`Task\|Edit\|Write\|MultiEdit\|NotebookEdit\|Bash`) | Inject `[recall: N hits]` before a tool runs; runs from `$SYMBIOSIS_BRAIN_TOOLS` |
| **brain-sync.sh** | SessionEnd | `git add/commit/push` the vault to GitHub (auto mode, soft-fail) |
| **sb-statusline.sh** | statusLine | Status bar: directory, model, context %, rate-limit bars (sources `sb-line.sh` + `sb-base-statusline.sh`) |

## brain-save-trigger.sh

Three modes in one script:

| Mode | Event | Behavior |
|------|-------|----------|
| `stop` | Stop | Reminds to brain-save at **25/35/45%** context with delta-guard and SAVE_LATER support |
| `precompact` | PreCompact | Blocks compaction once for a last-chance save |
| `prompt-check` | UserPromptSubmit | Active recall (`[memory: …]`) + rules roster + relays a blocked-compaction reminder |

**Stop-mode design** (details in vault note `decisions/stop-hook-smart-trigger.md`):
- **Thresholds** `25 / 35 / 45%` — soft / serious / last-chance zones with escalating messages. Calibrated for the 1M-context envelope (sessions typically stay in 0–50%, quality degrades around 40%).
- **Delta-guard** `10%` — below the top zone, skip a trigger if context grew by less than this since the last `brain-save` (avoids double-dipping after a recent save).
- **SAVE_LATER marker** — the user can postpone one soft-zone trigger by saying "потом"/"save later"; the top zone always fires.
- **Marker coordination** — the PostToolUse hook `brain-save-marker.sh` writes `brain-last-save-pct-${SESSION_ID}` (under `SB_TMP`, resolved via the `${TMPDIR:-${TEMP:-/tmp}}` env-chain — same temp dir the hooks use) on any successful `brain_write` / `brain_append` / `brain_patch`, so the hook knows it was recently fed. It reads `session_id` from its own payload, so the marker is correct in resumed, forked and multi-window sessions; the `brain-save` skill no longer touches it.

**Changing thresholds or delta-guard:** set env vars in `~/.claude/settings.json` —
`SYMBIOSIS_BRAIN_SAVE_THRESHOLDS` (default `25,35,45`) and
`SYMBIOSIS_BRAIN_SAVE_DELTA_GUARD` (default `10`). The hook reads them at runtime;
no script edit needed.

## Bridge files

The status line is the only component that sees the harness JSON on every tick, so it
publishes what other components need. All paths live under `SB_TMP`
(`${TMPDIR:-${TEMP:-/tmp}}`). Both bridges are written by `sb-export.sh`, which
`sb-statusline.sh` runs **before** it decides whose first line to render — so a user
who brings their own status line (`SYMBIOSIS_BRAIN_USER_STATUSLINE_CMD`) keeps them.

| File | Written by | Read by |
|------|-----------|---------|
| `brain-context-pct-<session_id>` | `sb-export.sh` (every tick) | `brain-save-trigger.sh` (Stop thresholds), `brain-save-marker.sh` |
| `brain-last-save-pct-<session_id>` | `brain-save-marker.sh` (PostToolUse) | `brain-save-trigger.sh` (delta-guard), `sb-line.sh` |
| `claude-rate-limits.json` | `sb-export.sh` (every tick) | any limit-watcher agent |
| `brain-sync-failed` | `brain-sync.sh` (on failure; removed on success) | `brain-session-start.sh` banner, `sb-line.sh` (`⚠️sync:<stage>`) |
| `brain-sync-errors.log` | `brain-sync.sh` (appended) | the human |

`claude-rate-limits.json` — one line, rewritten on every tick, written only when the
harness sent a `five_hour` block:

```json
{"five_hour_pct":33,"resets_at":1787662800,"seven_day_pct":20,"ts":1787660714}
```

`five_hour_pct` / `seven_day_pct` are whole percents; `resets_at` is the unix second the
5-hour window resets (`0` when the harness did not send it); `ts` is the unix second the
snapshot was written. Override the path with `SYMBIOSIS_BRAIN_RATE_LIMITS_FILE`, turn the
bridge off with `SYMBIOSIS_BRAIN_RATE_LIMITS_DISABLED=1`.

`brain-sync-failed` is exactly one line: `stage=<pull|push|conflict> at=<ISO8601>`.

**Extension point.** Set `SYMBIOSIS_BRAIN_USER_STATUSLINE_CMD` in `~/.claude/settings.json`
to run your own first line; `sb-statusline.sh` keeps rendering the Symbiosis Brain line
below it and keeps both bridges alive. `symbiosis-brain setup` moves a pre-existing
`statusLine.command` into that variable automatically.

## Installation

The supported path is `symbiosis-brain setup claude-code`, which copies these
hooks into `~/.claude/hooks/`, wires the six events + statusLine into
`settings.json`, and seeds the `SYMBIOSIS_BRAIN_*` env block. Manual install:

```bash
mkdir -p ~/.claude/hooks
cp hooks/brain-session-start.sh hooks/brain-save-trigger.sh \
   hooks/brain-pre-action-trigger.sh hooks/brain-sync.sh \
   hooks/sb-statusline.sh hooks/sb-line.sh hooks/sb-base-statusline.sh ~/.claude/hooks/
chmod +x ~/.claude/hooks/*.sh
```

Then add the hooks + statusLine configuration to `~/.claude/settings.json` (see
the project root README.md).
