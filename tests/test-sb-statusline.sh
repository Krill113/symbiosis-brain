#!/bin/bash
# Tests for sb-line.sh / sb-statusline.sh / sb-base-statusline.sh
set -e

HOOKS="$(cd "$(dirname "$0")/.." && pwd)/hooks"
LINE="$HOOKS/sb-line.sh"
WRAPPER="$HOOKS/sb-statusline.sh"
BASE="$HOOKS/sb-base-statusline.sh"
SESSION_ID="sb-status-$$"

cleanup() {
  rm -f "/tmp/brain-last-save-pct-${SESSION_ID}"
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
echo "12" > "/tmp/brain-last-save-pct-${SESSION_ID}"
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

echo ""
echo "Results: $pass passed, $fail failed"
[ "$fail" -eq 0 ] || exit 1
