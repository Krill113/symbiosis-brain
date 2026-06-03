#!/bin/bash
# Test brain-save-trigger.sh stop + precompact modes: threshold zones (soft /
# serious / last-chance), env-driven thresholds, delta-guard, SAVE_LATER one-shot
# skip, and precompact blocking. Ports the coverage that previously lived only in
# tests/test_brain_save_trigger_py.py — kept after the python hook shim was removed
# (bash is the single source of truth).
#
# The bash hook hardcodes /tmp/ flag paths (TMPDIR is NOT consulted on the stop
# path), so we isolate with a unique pid-based session id and clean every flag
# file we touch — no real session collides. See
# [[mistakes/test-subprocess-inherits-system-tmpdir]].

HOOK="$(cd "$(dirname "$0")/.." && pwd)/hooks/brain-save-trigger.sh"
SID="test-stop-$$"
PCT_FILE="/tmp/brain-context-pct-${SID}"
TRIGGERED="/tmp/brain-triggered-${SID}"
LAST_SAVE="/tmp/brain-last-save-pct-${SID}"
SAVE_LATER="/tmp/brain-save-later-${SID}"
PRECOMPACT="/tmp/brain-precompact-${SID}"
PENDING="/tmp/brain-precompact-pending-${SID}"

cleanup() {
  rm -f "$PCT_FILE" "$TRIGGERED" "$LAST_SAVE" "$SAVE_LATER" "$PRECOMPACT" "$PENDING"
}
trap cleanup EXIT

# Deterministic defaults regardless of the inherited environment.
unset SYMBIOSIS_BRAIN_SAVE_THRESHOLDS SYMBIOSIS_BRAIN_SAVE_DELTA_GUARD

pass=0
fail=0
t() {
  local name="$1"
  if [ "$2" = "PASS" ]; then echo "✓ $name"; pass=$((pass+1));
  else echo "✗ $name"; fail=$((fail+1)); fi
}

# Run a mode; capture combined stdout+stderr in OUT and exit code in RC without
# aborting (the hook exits 2 on a trigger).
run() {
  local mode="$1"
  local input="{\"session_id\":\"${SID}\"}"
  OUT=$(echo "$input" | bash "$HOOK" "$mode" 2>&1)
  RC=$?
}

# 1. No pct file → no-op, exit 0
cleanup
run stop
[ "$RC" = "0" ] && t "stop without pct returns 0" PASS || t "stop without pct returns 0" FAIL

# 2. Below soft threshold (default 25) → exit 0
cleanup
echo "20" > "$PCT_FILE"
run stop
[ "$RC" = "0" ] && t "below soft threshold returns 0" PASS || t "below soft threshold returns 0" FAIL

# 3. Soft zone (27% with 25/35/45) → exit 2, message + SAVE_LATER hint, triggered has 25
cleanup
echo "27" > "$PCT_FILE"
run stop
ok=PASS
[ "$RC" = "2" ] || ok=FAIL
[[ "$OUT" == *"Контекст 27%"* ]] || ok=FAIL
[[ "$OUT" == *"SAVE_LATER"* ]] || ok=FAIL
grep -q "^25$" "$TRIGGERED" 2>/dev/null || ok=FAIL
t "soft zone fires with default thresholds" $ok

# 4. Serious (middle) zone (37%) → "пора сохранять"
cleanup
echo "37" > "$PCT_FILE"
run stop
ok=PASS
[ "$RC" = "2" ] || ok=FAIL
[[ "$OUT" == *"пора сохранять"* ]] || ok=FAIL
t "serious zone message" $ok

# 5. Last-chance zone (47% ≥ top 45) → "последний шанс"
cleanup
echo "47" > "$PCT_FILE"
run stop
ok=PASS
[ "$RC" = "2" ] || ok=FAIL
[[ "$OUT" == *"последний шанс"* ]] || ok=FAIL
t "last-chance zone message" $ok

# 6. Thresholds read from env (40,70,90; 42% marks 40 only, not 70)
cleanup
echo "42" > "$PCT_FILE"
SYMBIOSIS_BRAIN_SAVE_THRESHOLDS="40,70,90" run stop
ok=PASS
[ "$RC" = "2" ] || ok=FAIL
grep -q "^40$" "$TRIGGERED" 2>/dev/null || ok=FAIL
grep -q "^70$" "$TRIGGERED" 2>/dev/null && ok=FAIL
t "thresholds read from env" $ok

# 7. Already-triggered zone → no refire
cleanup
echo "37" > "$PCT_FILE"
printf "25\n35\n" > "$TRIGGERED"
run stop
[ "$RC" = "0" ] && t "already-triggered zone does not refire" PASS || t "already-triggered zone does not refire" FAIL

# 8. Delta-guard skips recent save (37%, last-save 30, delta 7 < 10) → exit 0
cleanup
echo "37" > "$PCT_FILE"
echo "30" > "$LAST_SAVE"
run stop
[ "$RC" = "0" ] && t "delta-guard skips recent save" PASS || t "delta-guard skips recent save" FAIL

# 9. Delta-guard read from env (guard 5, delta 7 ≥ 5 → fires)
cleanup
echo "37" > "$PCT_FILE"
echo "30" > "$LAST_SAVE"
SYMBIOSIS_BRAIN_SAVE_DELTA_GUARD="5" run stop
[ "$RC" = "2" ] && t "delta-guard read from env" PASS || t "delta-guard read from env" FAIL

# 10. SAVE_LATER one-shot skip in soft zone — consumed
cleanup
echo "27" > "$PCT_FILE"
: > "$SAVE_LATER"
run stop
ok=PASS
[ "$RC" = "0" ] || ok=FAIL
[ -f "$SAVE_LATER" ] && ok=FAIL
t "SAVE_LATER skips one in soft zone and is consumed" $ok

# 11. precompact first call blocks and writes pending
cleanup
run precompact
ok=PASS
[ "$RC" = "2" ] || ok=FAIL
[[ "$OUT" == *"Save memory?"* ]] || ok=FAIL
[ -f "$PRECOMPACT" ] || ok=FAIL
[ -f "$PENDING" ] || ok=FAIL
t "precompact first call blocks and writes pending" $ok

# 12. precompact second call passes through
cleanup
: > "$PRECOMPACT"
run precompact
[ "$RC" = "0" ] && t "precompact second call passes through" PASS || t "precompact second call passes through" FAIL

echo ""
echo "Results: $pass passed, $fail failed"
[ "$fail" -eq 0 ] || exit 1
