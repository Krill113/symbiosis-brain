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
- **Marker coordination** — skill `brain-save` writes `brain-last-save-pct-${SESSION_ID}` (under `SB_TMP`, resolved via the `${TMPDIR:-${TEMP:-/tmp}}` env-chain — same temp dir the hooks use) after each meaningful save so the hook knows it was recently fed.

**Changing thresholds or delta-guard:** set env vars in `~/.claude/settings.json` —
`SYMBIOSIS_BRAIN_SAVE_THRESHOLDS` (default `25,35,45`) and
`SYMBIOSIS_BRAIN_SAVE_DELTA_GUARD` (default `10`). The hook reads them at runtime;
no script edit needed.

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
