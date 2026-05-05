# Symbiosis Brain Hooks

Claude Code hooks and status line for automatic context management.

## Installed

- **brain-session-start.sh** — SessionStart (startup + compact): injects CRITICAL_FACTS.md, scope, resets trigger flags
- **brain-save-trigger.sh** — Stop + PreCompact + UserPromptSubmit: proactive brain-save reminders at context thresholds
- **statusline.sh** — Status bar: directory, model, context %, 5h reset timer, 5h/7d rate limit bars

## brain-save-trigger.sh

Three modes in one script:

| Mode | Event | Behavior |
|------|-------|----------|
| `stop` | Stop | Reminds to brain-save at **40/70/90%** context with delta-guard and SAVE_LATER support |
| `precompact` | PreCompact | Blocks compaction once for last-chance save |
| `prompt-check` | UserPromptSubmit | Injects brain-save instruction if compaction was blocked |

**Stop-mode design** (details in vault note `decisions/stop-hook-smart-trigger.md`):
- **Thresholds** `40 / 70 / 90%` — soft / serious / last-chance zones with escalating messages
- **Delta-guard** `20%` — below 90% zone, skip trigger if context grew by less than this since last `brain-save` (avoids double-dipping after a recent save)
- **SAVE_LATER marker** — user can postpone one soft-zone trigger by saying "потом"/"save later"; hard zones (70%+) ignore the marker
- **Marker coordination** — skill `brain-save` writes `/tmp/brain-last-save-pct-${SESSION_ID}` after each meaningful save so the hook knows it was recently fed

**Changing thresholds or delta-guard:** edit `THRESHOLDS=(40 70 90)` and `DELTA_GUARD=20` at the top of the script.

## Installation

```bash
mkdir -p ~/.claude/hooks
cp hooks/brain-session-start.sh ~/.claude/hooks/
cp hooks/brain-save-trigger.sh ~/.claude/hooks/
cp hooks/statusline.sh ~/.claude/hooks/
chmod +x ~/.claude/hooks/brain-*.sh ~/.claude/hooks/statusline.sh
```

Then add hook and statusLine configuration to `~/.claude/settings.json` (see README.md in project root).
