#!/bin/bash
# brain-sync.sh — push vault to GitHub.
# Triggered by: SessionEnd hook (auto mode), /brain-sync slash command (manual mode).
# Soft-fail in auto mode — never block session.

VAULT="${SYMBIOSIS_BRAIN_VAULT:-$HOME/symbiosis-brain-vault}"
MODE="${1:-auto}"
SB_TMP="${TMPDIR:-${TEMP:-/tmp}}"

# Soft-fail guards
[ ! -d "$VAULT/.git" ] && exit 0
cd "$VAULT" 2>/dev/null || exit 0
git remote get-url origin >/dev/null 2>&1 || exit 0

# Stage and commit if there are uncommitted changes
git add -A 2>/dev/null
if ! git diff --cached --quiet 2>/dev/null; then
  TS=$(date '+%Y-%m-%d %H:%M')
  git commit -m "session: $TS" >/dev/null 2>&1
fi

# Push (silent in auto, verbose in manual). 30s timeout for network hangs.
if [ "$MODE" = "manual" ]; then
  timeout 30 git push 2>&1
else
  timeout 30 git push >/dev/null 2>&1 \
    || echo "$(date) brain-sync push failed" >> "$SB_TMP/brain-sync-errors.log" 2>/dev/null
fi

exit 0
