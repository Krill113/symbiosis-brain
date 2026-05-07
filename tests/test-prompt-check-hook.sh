#!/bin/bash
# Test brain-save-trigger.sh prompt-check mode (A1 + A2 + composer)
set -e

HOOK="$(cd "$(dirname "$0")/.." && pwd)/hooks/brain-save-trigger.sh"
SESSION_ID="test-prompt-$$"
PCT_FILE="/tmp/brain-context-pct-${SESSION_ID}"
SHOWN="/tmp/brain-rules-shown-${SESSION_ID}"
TURNS="/tmp/brain-rules-turn-counter-${SESSION_ID}"

cleanup() {
  rm -f "$PCT_FILE" "$SHOWN" "$TURNS" \
        "/tmp/brain-precompact-${SESSION_ID}" \
        "/tmp/brain-precompact-pending-${SESSION_ID}"
}
trap cleanup EXIT

pass=0
fail=0

t() {
  local name="$1"
  if [ "$2" = "PASS" ]; then
    echo "✓ $name"
    pass=$((pass+1))
  else
    echo "✗ $name"
    fail=$((fail+1))
  fi
}

run_hook() {
  local prompt="$1"
  local input="{\"session_id\":\"${SESSION_ID}\",\"prompt\":\"${prompt}\"}"
  echo "$input" | bash "$HOOK" prompt-check 2>/dev/null
}

export SYMBIOSIS_BRAIN_RECALL_ENABLED=false
export SYMBIOSIS_BRAIN_RULES_ENABLED=true
export SYMBIOSIS_BRAIN_RULES_ZONES="30,60,85"
export SYMBIOSIS_BRAIN_RULES_TURN_INTERVAL="10"

# Test 1: short prompt → A1 skipped (no memory section)
cleanup
echo "50" > "$PCT_FILE"
out=$(SYMBIOSIS_BRAIN_RECALL_ENABLED=true run_hook "ok")
if [[ "$out" != *"[memory:"* ]]; then t "short prompt skips A1" PASS; else t "short prompt skips A1" FAIL; fi

# Test 2: slash-command → A1 skipped
cleanup
out=$(SYMBIOSIS_BRAIN_RECALL_ENABLED=true run_hook "/compact")
if [[ "$out" != *"[memory:"* ]]; then t "slash-command skips A1" PASS; else t "slash-command skips A1" FAIL; fi

# Test 3: zone trigger → rules section emitted
cleanup
echo "63" > "$PCT_FILE"
echo "0" > "$TURNS"
out=$(run_hook "this is a long enough prompt to not trip the short-guard")
if [[ "$out" == *"[rules"* ]]; then t "zone 60 triggers rules" PASS; else t "zone 60 triggers rules" FAIL; fi
if grep -q "^60$" "$SHOWN" 2>/dev/null; then t "zone marked as shown" PASS; else t "zone marked as shown" FAIL; fi

# Test 4: turn counter trigger → rules section emitted
cleanup
echo "10" > "$PCT_FILE"
echo "10" > "$TURNS"
out=$(run_hook "another long enough prompt for guard")
if [[ "$out" == *"[rules"* ]]; then t "turn counter triggers rules" PASS; else t "turn counter triggers rules" FAIL; fi
if [ "$(cat "$TURNS" 2>/dev/null)" = "0" ]; then t "turn counter reset" PASS; else t "turn counter reset" FAIL; fi

# Test 5: zone already shown → no re-show
cleanup
echo "63" > "$PCT_FILE"
echo "60" > "$SHOWN"
echo "0" > "$TURNS"
out=$(run_hook "long enough prompt to bypass guard")
if [[ "$out" != *"[rules"* ]]; then t "marked zone not re-shown" PASS; else t "marked zone not re-shown" FAIL; fi

# Test 6: rules disabled → no rules section
cleanup
SYMBIOSIS_BRAIN_RULES_ENABLED=false out=$(SYMBIOSIS_BRAIN_RULES_ENABLED=false run_hook "long enough prompt to bypass guard")
if [[ "$out" != *"[rules"* ]]; then t "rules disabled silences A2" PASS; else t "rules disabled silences A2" FAIL; fi

# Test 7: A1 recall surfaces a memory block when SYMBIOSIS_BRAIN_TOOLS is set
# and the search-gist invocation succeeds. Stub `uv` returns a fixed JSON
# payload — keeps the test hermetic and fast.
cleanup
echo "10" > "$PCT_FILE"
TMPBIN="$(mktemp -d)"
cat > "$TMPBIN/uv" <<'EOF'
#!/bin/sh
echo '[{"path":"x.md","title":"X","scope":"global","gist":"g"}]'
EOF
chmod +x "$TMPBIN/uv"
out=$(SYMBIOSIS_BRAIN_RECALL_ENABLED=true \
      SYMBIOSIS_BRAIN_TOOLS=/tmp/fake-tools \
      SYMBIOSIS_BRAIN_VAULT=/tmp/fake-vault \
      SYMBIOSIS_BRAIN_RULES_ENABLED=false \
      PATH="$TMPBIN:$PATH" \
      run_hook "long enough prompt for guard")
if [[ "$out" == *"[memory: 1 hits"* ]]; then t "A1 recall surfaces memory block" PASS; else t "A1 recall surfaces memory block" FAIL; fi
rm -rf "$TMPBIN"

# Test 8: First-turn roster injection at pct=10 (below all zones)
cleanup
echo "10" > "$PCT_FILE"
out=$(SYMBIOSIS_BRAIN_RECALL_ENABLED=false SYMBIOSIS_BRAIN_RULES_ENABLED=true run_hook "long enough prompt to bypass guard")
if [[ "$out" == *"[rules"* ]]; then t "first-turn roster fires below all zones" PASS; else t "first-turn roster fires below all zones" FAIL; fi
if [ -f "$SHOWN" ] && grep -q "^0$" "$SHOWN"; then t "sentinel 0 written to shown file" PASS; else t "sentinel 0 written to shown file" FAIL; fi

# Test 9: First-turn injection only once (no spam).
# Self-contained: explicitly seed $SHOWN with sentinel "0" so this test
# doesn't silently break if anyone reorders or adds cleanup before it.
echo "10" > "$PCT_FILE"
echo "0" > "$SHOWN"
out=$(SYMBIOSIS_BRAIN_RECALL_ENABLED=false SYMBIOSIS_BRAIN_RULES_ENABLED=true run_hook "another long enough prompt below zones")
if [[ "$out" != *"[rules"* ]]; then t "first-turn roster doesn't repeat" PASS; else t "first-turn roster doesn't repeat" FAIL; fi

echo ""
echo "Results: $pass passed, $fail failed"
[ "$fail" -eq 0 ] || exit 1
