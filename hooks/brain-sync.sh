#!/bin/bash
# brain-sync.sh — sync the vault with GitHub.
# Triggered by: SessionEnd hook (auto mode), /brain-sync slash command (manual mode).
# Soft-fail in auto mode — never block the session. Always exit 0.
#
# Push-only sync diverged silently the moment a second machine (or a manual push)
# touched the vault, so we rebase first. A rebase that cannot be resolved
# mechanically is ABORTED, never auto-resolved: the vault is the user's memory and
# SessionEnd has no output channel to ask. The alarm is raised through two files
# that brain-session-start.sh and sb-line.sh read.
#
# Budget: SessionEnd allows 40s (install_lib._hooks_block). Both network steps are
# capped at SB_GIT_TIMEOUT (15s each), leaving room for add/commit on a large vault.
# The push budget went 30s -> 15s for that reason, and push is SKIPPED whenever the
# rebase step failed: after a conflict abort the local branch is behind, so a push
# could only be a non-fast-forward rejection or, worse, a force.
#
# Single-writer lock: SessionEnd has NO matcher in install_lib._hooks_block, so it
# fires in EVERY window. Two windows closing together used to be harmless (add /
# commit / push are idempotent enough), but `git rebase --abort` is not: the second
# process would abort the first process's in-flight rebase inside the live vault.

VAULT="${SYMBIOSIS_BRAIN_VAULT:-$HOME/symbiosis-brain-vault}"
MODE="${1:-auto}"
SB_TMP="${TMPDIR:-${TEMP:-/tmp}}"
SB_GIT_TIMEOUT="${SYMBIOSIS_BRAIN_SYNC_GIT_TIMEOUT:-15}"
SYNC_MARKER="$SB_TMP/brain-sync-failed"
SYNC_LOG="$SB_TMP/brain-sync-errors.log"
SYNC_LOCK="$SB_TMP/brain-sync.lock"

# Reap a lock left behind by a killed process (mkdir locks do not self-clear).
# 5 min > the 40s hook budget by a wide margin, so this can only hit a corpse.
find "$SB_TMP" -maxdepth 1 -name 'brain-sync.lock' -type d -mmin +5 \
     -exec rm -rf {} + 2>/dev/null

# Single writer. Losing the race is a normal outcome, not an error: exit quietly,
# leave no marker (the winner will write one if the sync actually fails).
mkdir "$SYNC_LOCK" 2>/dev/null || exit 0
trap 'rmdir "$SYNC_LOCK" 2>/dev/null' EXIT

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

STAGE=""

rebase_in_progress() {
  [ -d "$(git rev-parse --git-path rebase-merge 2>/dev/null)" ] || \
  [ -d "$(git rev-parse --git-path rebase-apply 2>/dev/null)" ]
}

# Anything half-finished that was ALREADY here when we arrived is not ours — the
# owner mid-rebase or mid-merge in the vault, or a corpse from a crash. Never abort
# it, never reset over it, never pile onto it: raise the alarm and stop.
OURS=1
if rebase_in_progress || [ -n "$(git ls-files --unmerged 2>/dev/null)" ]; then
  OURS=0
  STAGE="conflict"
fi

# Rebase onto the remote first. `log.md merge=union` (seeded into the vault's
# .gitattributes by the installer) resolves the append-only journal; anything else
# that conflicts is the owner's call.
#
# Abort ONLY a rebase we started ourselves — hence the $OURS flag rather than a bare
# directory test. The lock above already keeps two windows apart; this keeps us off
# the owner's own rebase.
if [ "$OURS" = "1" ] && \
   ! timeout "$SB_GIT_TIMEOUT" git pull --rebase --autostash >/dev/null 2>&1; then
  if rebase_in_progress; then
    git rebase --abort >/dev/null 2>&1
    STAGE="conflict"
  else
    STAGE="pull"
  fi
fi

# Autostash pop conflict — a SEPARATE failure mode, and `git pull` reports it with
# EXIT CODE 0 (measured 2026-08-25 on Git for Windows: the rebase itself
# fast-forwarded, only re-applying the stash left `UU` behind), so the branch above
# never sees it. Reachable only when the commit at the top did NOT run — no
# user.email, a refusing pre-commit hook — leaving staged changes for --autostash
# to take.
# Recovery is `git reset --hard HEAD`, NOT `git checkout -- .`: checkout (even with
# --force) refuses an unmerged path and leaves the tree dirty — also measured. The
# autostash entry survives the reset ("Your changes are safe in the stash"), so
# nothing unrecoverable is dropped, and the alarm tells the owner to `git stash pop`.
if [ "$OURS" = "1" ] && [ -z "$STAGE" ] && [ -n "$(git ls-files --unmerged 2>/dev/null)" ]; then
  git reset --hard HEAD >/dev/null 2>&1
  STAGE="conflict"
fi

if [ -z "$STAGE" ]; then
  if [ "$MODE" = "manual" ]; then
    timeout "$SB_GIT_TIMEOUT" git push 2>&1 || STAGE="push"
  else
    timeout "$SB_GIT_TIMEOUT" git push >/dev/null 2>&1 || STAGE="push"
  fi
fi

if [ -n "$STAGE" ]; then
  AT=$(date -Iseconds 2>/dev/null || date '+%Y-%m-%dT%H:%M:%S')
  {
    printf '%s mode=%s stage=%s\n' "$AT" "$MODE" "$STAGE"
    git status -sb 2>&1 | head -5
  } >> "$SYNC_LOG" 2>/dev/null
  # Alarm marker — exactly one line, read by brain-session-start.sh and sb-line.sh.
  printf 'stage=%s at=%s\n' "$STAGE" "$AT" > "$SYNC_MARKER" 2>/dev/null
  [ "$MODE" = "manual" ] && echo "brain-sync: $STAGE failed — see $SYNC_LOG"
else
  rm -f "$SYNC_MARKER" 2>/dev/null
fi

exit 0
