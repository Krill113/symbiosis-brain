#!/bin/bash
# Tests for sb-line.sh / sb-statusline.sh / sb-base-statusline.sh
set -e

HOOKS="$(cd "$(dirname "$0")/.." && pwd)/hooks"
LINE="$HOOKS/sb-line.sh"
WRAPPER="$HOOKS/sb-statusline.sh"
BASE="$HOOKS/sb-base-statusline.sh"
SESSION_ID="sb-status-$$"

# Own temp dir for the WHOLE file, exported before the first case. The helpers read
# ${TMPDIR:-${TEMP:-/tmp}}, and on Git-Bash for Windows a bare /tmp IS the live
# %LOCALAPPDATA%\Temp — the same dir the running sessions use. Pinning /tmp made the
# last-save case read whatever marker another window had just written (measured:
# 19 passed / 1 failed with TMPDIR set in the environment, 20 / 0 with it unset), and
# let the suite scribble over live bridge files on the way.
SB_WORK="$(mktemp -d)"
export TMPDIR="$SB_WORK" TEMP="$SB_WORK"

cleanup() {
  rm -rf "$SB_WORK"
}
trap cleanup EXIT

pass=0
fail=0
t() {
  if [ "$2" = "PASS" ]; then echo "✓ $1"; pass=$((pass+1)); else echo "✗ $1"; fail=$((fail+1)); fi
}

INPUT="{\"session_id\":\"${SESSION_ID}\"}"

# Test 1: sb-line.sh emits expected format
export SYMBIOSIS_BRAIN_SAVE_THRESHOLDS="40,70,90"
export SYMBIOSIS_BRAIN_RULES_ZONES="30,60,85"
export SYMBIOSIS_BRAIN_RULES_TURN_INTERVAL="10"
export SYMBIOSIS_BRAIN_SCOPE="alpha-seti"
echo "12" > "$SB_WORK/brain-last-save-pct-${SESSION_ID}"
out=$(echo "$INPUT" | bash "$LINE" 2>/dev/null)

if [[ "$out" == *"🧠 [Symbiosis-Brain]"* ]]; then t "sb-line emits header" PASS; else t "sb-line emits header" FAIL; fi
if [[ "$out" == *"scope: alpha-seti"* ]]; then t "sb-line shows scope" PASS; else t "sb-line shows scope" FAIL; fi
if [[ "$out" == *"auto-save: [40/70/90]"* ]]; then t "sb-line shows save thresholds" PASS; else t "sb-line shows save thresholds" FAIL; fi
if [[ "$out" == *"rules: [30/60/85·R10]"* ]]; then t "sb-line shows rules thresholds" PASS; else t "sb-line shows rules thresholds" FAIL; fi
if [[ "$out" == *"last-save: 12%"* ]]; then t "sb-line shows last-save" PASS; else t "sb-line shows last-save" FAIL; fi

# Test 2: sb-statusline wrapper delegates to user cmd
unset SYMBIOSIS_BRAIN_USER_STATUSLINE_CMD
out=$(echo "$INPUT" | SYMBIOSIS_BRAIN_USER_STATUSLINE_CMD="echo CUSTOM_USER_LINE" bash "$WRAPPER" 2>/dev/null)
if [[ "$out" == *"CUSTOM_USER_LINE"* ]]; then t "wrapper delegates to user cmd" PASS; else t "wrapper delegates to user cmd" FAIL; fi
if [[ "$out" == *"🧠 [Symbiosis-Brain]"* ]]; then t "wrapper still emits sb-line" PASS; else t "wrapper still emits sb-line" FAIL; fi

# Test 3: sb-statusline wrapper falls back to base
unset SYMBIOSIS_BRAIN_USER_STATUSLINE_CMD
out=$(echo "$INPUT" | bash "$WRAPPER" 2>/dev/null)
if [ -n "$out" ]; then t "wrapper produces output" PASS; else t "wrapper produces output" FAIL; fi
if [[ "$out" == *"🧠 [Symbiosis-Brain]"* ]]; then t "wrapper without user cmd emits sb-line" PASS; else t "wrapper without user cmd emits sb-line" FAIL; fi

# Test 4: user cmd that crashes — wrapper still emits sb-line
out=$(echo "$INPUT" | SYMBIOSIS_BRAIN_USER_STATUSLINE_CMD="false" bash "$WRAPPER" 2>/dev/null)
if [[ "$out" == *"🧠 [Symbiosis-Brain]"* ]]; then t "wrapper survives user cmd crash" PASS; else t "wrapper survives user cmd crash" FAIL; fi

# ── Stage-0 hygiene ───────────────────────────────────────────────────────────
# These assert on the bridge files the status line writes (context %, rate limits,
# sync alarm) — all of them inside $SB_WORK, which is exported at the top of the file.
SB_SID="sb-bridge-$$"
SB_JSON="{\"session_id\":\"${SB_SID}\",\"cwd\":\"/x/proj\",\"model\":{\"display_name\":\"Claude Fable 5\"},\"used_percentage\":23,\"five_hour\":{\"used_percentage\":33,\"resets_at\":1787662800},\"seven_day\":{\"used_percentage\":20}}"

# Test 5 (A2): without env the second line must show the SAME thresholds the Stop-hook
# actually fires on. sb-line.sh shipped 40/70/90 while brain-save-trigger.sh fired at
# 25/35/45 — invisible only because settings.json set the env explicitly.
out=$(echo "$SB_JSON" | env -u SYMBIOSIS_BRAIN_SAVE_THRESHOLDS -u SYMBIOSIS_BRAIN_RULES_ZONES \
        bash "$LINE" 2>/dev/null)
if [[ "$out" == *"auto-save: [25/35/45]"* ]]; then t "sb-line default thresholds are 25/35/45" PASS; else t "sb-line default thresholds are 25/35/45" FAIL; fi
if [[ "$out" == *"rules: [30/60/85"* ]]; then t "sb-line default rules zones unchanged" PASS; else t "sb-line default rules zones unchanged" FAIL; fi

# Test 6 (A-N7): both bridges are exported even when the user brings their own first
# line. Before the fix the exports lived inside sb-base-statusline.sh, so anyone with
# SYMBIOSIS_BRAIN_USER_STATUSLINE_CMD silently lost every save reminder.
rm -f "$SB_WORK/brain-context-pct-${SB_SID}" "$SB_WORK/claude-rate-limits.json"
echo "$SB_JSON" | SYMBIOSIS_BRAIN_USER_STATUSLINE_CMD="echo CUSTOM" bash "$WRAPPER" >/dev/null 2>&1
if [ "$(cat "$SB_WORK/brain-context-pct-${SB_SID}" 2>/dev/null)" = "23" ]; then
  t "context-pct exported with a user statusline cmd" PASS
else
  t "context-pct exported with a user statusline cmd" FAIL
fi

# Test 7 (A-N7 / owner decision 5): the rate-limit bridge is ON by default and its
# format is the documented one (hooks/README.md).
if grep -q '"five_hour_pct":33' "$SB_WORK/claude-rate-limits.json" 2>/dev/null &&
   grep -q '"resets_at":1787662800' "$SB_WORK/claude-rate-limits.json" 2>/dev/null &&
   grep -q '"seven_day_pct":20' "$SB_WORK/claude-rate-limits.json" 2>/dev/null &&
   grep -qE '"ts":[0-9]+' "$SB_WORK/claude-rate-limits.json" 2>/dev/null; then
  t "claude-rate-limits.json written by default" PASS
else
  t "claude-rate-limits.json written by default" FAIL
fi

# Test 8: the bridge honours its opt-out and its path override.
rm -f "$SB_WORK/claude-rate-limits.json"
echo "$SB_JSON" | SYMBIOSIS_BRAIN_RATE_LIMITS_DISABLED=1 bash "$WRAPPER" >/dev/null 2>&1
if [ ! -f "$SB_WORK/claude-rate-limits.json" ]; then t "rate-limit bridge respects the kill switch" PASS; else t "rate-limit bridge respects the kill switch" FAIL; fi
echo "$SB_JSON" | SYMBIOSIS_BRAIN_RATE_LIMITS_FILE="$SB_WORK/custom-limits.json" bash "$WRAPPER" >/dev/null 2>&1
if [ -s "$SB_WORK/custom-limits.json" ]; then t "rate-limit bridge respects the path override" PASS; else t "rate-limit bridge respects the path override" FAIL; fi

# Test 9: running the base line on its own must not lose the bridges (a direct call is
# still a supported entry point).
rm -f "$SB_WORK/brain-context-pct-${SB_SID}"
echo "$SB_JSON" | bash "$BASE" >/dev/null 2>&1
if [ "$(cat "$SB_WORK/brain-context-pct-${SB_SID}" 2>/dev/null)" = "23" ]; then
  t "base statusline still exports when called directly" PASS
else
  t "base statusline still exports when called directly" FAIL
fi

# Test 10 (A3): a failed vault sync is visible on the always-on status line.
printf 'stage=conflict at=2026-08-25T21:15:00+03:00\n' > "$SB_WORK/brain-sync-failed"
out=$(echo "$SB_JSON" | bash "$LINE" 2>/dev/null)
if [[ "$out" == *"⚠️sync:conflict"* ]]; then t "sb-line shows the sync alarm" PASS; else t "sb-line shows the sync alarm" FAIL; fi
rm -f "$SB_WORK/brain-sync-failed"
out=$(echo "$SB_JSON" | bash "$LINE" 2>/dev/null)
if [[ "$out" != *"⚠️sync"* ]]; then t "sb-line is quiet without the marker" PASS; else t "sb-line is quiet without the marker" FAIL; fi

# Test 11: fork-free contract. Claude Code cancels the in-flight status script on every
# new event; on Windows/MSYS a child caught mid-fork by that cancel is stranded forever.
# No pipelines and no command substitutions of text tools in the statusline helpers.
# (The single `$(date +%s)` fallback for bash 3.2 is deliberate and stays allowed.)
#
# The existence precondition is NOT decoration: with a missing file `grep` writes to
# stderr and leaves stdout empty, the outer `grep -q .` returns 1, the `if` takes the
# else branch and the case reports PASS. Without this guard the check would be green
# precisely while there is nothing to check.
fk_missing=""
for f in sb-export.sh sb-hooklib.sh sb-line.sh; do
  [ -r "$HOOKS/$f" ] || fk_missing="$fk_missing $f"
done
if [ -n "$fk_missing" ]; then
  t "statusline helpers stay fork-free (missing:$fk_missing)" FAIL
elif grep -nE '\|[[:space:]]*(grep|sed|awk|cut|tr|head|wc)[[:space:]]|\$\((grep|sed|awk|cut|tr|head|wc)[[:space:]]' \
     "$HOOKS/sb-export.sh" "$HOOKS/sb-hooklib.sh" "$HOOKS/sb-line.sh" | grep -q .; then
  t "statusline helpers stay fork-free" FAIL
else
  t "statusline helpers stay fork-free" PASS
fi

echo ""
echo "Results: $pass passed, $fail failed"
[ "$fail" -eq 0 ] || exit 1
