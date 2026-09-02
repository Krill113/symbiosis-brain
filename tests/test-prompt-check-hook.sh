#!/bin/bash
# Test brain-save-trigger.sh prompt-check mode (A1 + A2 + composer)
set -e

# Pin tmp dir so the hook's SB_TMP=${TMPDIR:-${TEMP:-/tmp}} matches the /tmp paths
# used below (Linux CI often presets TMPDIR to a non-/tmp dir).
export TMPDIR=/tmp TEMP=/tmp

HOOK="$(cd "$(dirname "$0")/.." && pwd)/hooks/brain-save-trigger.sh"
SESSION_ID="test-prompt-$$"
PCT_FILE="/tmp/brain-context-pct-${SESSION_ID}"
SHOWN="/tmp/brain-rules-shown-${SESSION_ID}"
TURNS="/tmp/brain-rules-turn-counter-${SESSION_ID}"
ROUTE_TURN="/tmp/brain-route-turn-${SESSION_ID}"

cleanup() {
  rm -f "$PCT_FILE" "$SHOWN" "$TURNS" "$ROUTE_TURN" \
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

# Test 5: zone already shown → cadence-gated TOOLS line not re-shown.
# decompose default still emits DISCIPLINE every turn; only the throttled
# TOOLS/roster portion (keyed on the ASCII marker ".claude/docs/catalog/")
# must not repeat once its zone was shown.
cleanup
echo "63" > "$PCT_FILE"
echo "60" > "$SHOWN"
echo "0" > "$TURNS"
out=$(run_hook "long enough prompt to bypass guard")
if [[ "$out" != *".claude/docs/catalog/"* ]]; then t "marked zone: tools not re-shown" PASS; else t "marked zone: tools not re-shown" FAIL; fi
if [[ "$out" == *"[rules"* ]]; then t "marked zone: discipline still present" PASS; else t "marked zone: discipline still present" FAIL; fi

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

# Test 9: First-turn injection only once (no spam). Under decompose default,
# DISCIPLINE persists every turn; the cadence-gated TOOLS roster (keyed on
# ".claude/docs/catalog/") must not repeat after the first turn.
# Self-contained: explicitly seed $SHOWN with sentinel "0" so this test
# doesn't silently break if anyone reorders or adds cleanup before it.
echo "10" > "$PCT_FILE"
echo "0" > "$SHOWN"
out=$(SYMBIOSIS_BRAIN_RECALL_ENABLED=false SYMBIOSIS_BRAIN_RULES_ENABLED=true run_hook "another long enough prompt below zones")
if [[ "$out" != *".claude/docs/catalog/"* ]]; then t "first-turn tools roster doesn't repeat" PASS; else t "first-turn tools roster doesn't repeat" FAIL; fi

# Test 10: Bash hook writes debug log on search-gist failure
# Isolated via SYMBIOSIS_BRAIN_DEBUG_LOG so the test doesn't pollute (or destroy
# artifacts in) the production /tmp/brain-hook-debug.log used by real sessions.
cleanup
TMPLOGDIR="$(mktemp -d)"
DEBUG_LOG="$TMPLOGDIR/brain-hook-debug.log"
echo "10" > "$PCT_FILE"
TMPBIN="$(mktemp -d)"
cat > "$TMPBIN/uv" <<'EOF'
#!/bin/sh
echo "boom" >&2
exit 1
EOF
chmod +x "$TMPBIN/uv"
out=$(SYMBIOSIS_BRAIN_RECALL_ENABLED=true \
      SYMBIOSIS_BRAIN_TOOLS=/tmp/fake-tools \
      SYMBIOSIS_BRAIN_VAULT=/tmp/fake-vault \
      SYMBIOSIS_BRAIN_RULES_ENABLED=false \
      SYMBIOSIS_BRAIN_DEBUG_LOG="$DEBUG_LOG" \
      PATH="$TMPBIN:$PATH" \
      run_hook "long enough prompt for guard")
if [[ "$out" != *"[memory:"* ]]; then t "failure produces no memory block" PASS; else t "failure produces no memory block" FAIL; fi
if [ -s "$DEBUG_LOG" ] && grep -q "search-gist" "$DEBUG_LOG"; then t "debug log captured failure" PASS; else t "debug log captured failure" FAIL; fi
rm -rf "$TMPBIN" "$TMPLOGDIR"

# Test 11: Monotonic turn-counter (C5 §6.2) — increments UNCONDITIONALLY, even
# with RULES_ENABLED=false, on <15-char prompts, and on slash-command turns
# (outside the recall/rules gates). It must SURVIVE a SessionStart run with the
# same session_id (SessionStart deliberately EXCLUDES brain-route-turn-<sid>
# from its per-session rm-block so monotonicity carries across compact).
cleanup
SESSION_START_HOOK="$(cd "$(dirname "$0")/.." && pwd)/hooks/brain-session-start.sh"

# (a) grows with RULES_ENABLED=false on a normal turn → 1
SYMBIOSIS_BRAIN_RECALL_ENABLED=false SYMBIOSIS_BRAIN_RULES_ENABLED=false \
  run_hook "long enough prompt to bypass guard" >/dev/null
if [ "$(cat "$ROUTE_TURN" 2>/dev/null)" = "1" ]; then t "monotonic counter starts at 1 (rules off)" PASS; else t "monotonic counter starts at 1 (rules off)" FAIL; fi

# (b) grows on a <15-char prompt → 2
SYMBIOSIS_BRAIN_RECALL_ENABLED=false SYMBIOSIS_BRAIN_RULES_ENABLED=false \
  run_hook "ok" >/dev/null
if [ "$(cat "$ROUTE_TURN" 2>/dev/null)" = "2" ]; then t "monotonic counter grows on short prompt" PASS; else t "monotonic counter grows on short prompt" FAIL; fi

# (c) grows on a slash-command turn → 3
SYMBIOSIS_BRAIN_RECALL_ENABLED=false SYMBIOSIS_BRAIN_RULES_ENABLED=false \
  run_hook "/compact" >/dev/null
if [ "$(cat "$ROUTE_TURN" 2>/dev/null)" = "3" ]; then t "monotonic counter grows on slash turn" PASS; else t "monotonic counter grows on slash turn" FAIL; fi

# (d) survives a SessionStart run with the same session_id (counter NOT reset)
echo "{\"session_id\":\"${SESSION_ID}\",\"source\":\"compact\"}" | \
  SYMBIOSIS_BRAIN_VAULT=/tmp/fake-vault CLAUDE_ENV_FILE="" bash "$SESSION_START_HOOK" >/dev/null 2>&1
if [ "$(cat "$ROUTE_TURN" 2>/dev/null)" = "3" ]; then t "monotonic counter survives session-start (compact)" PASS; else t "monotonic counter survives session-start (compact)" FAIL; fi

# (e) keeps growing after compact → 4
SYMBIOSIS_BRAIN_RECALL_ENABLED=false SYMBIOSIS_BRAIN_RULES_ENABLED=false \
  run_hook "another long enough prompt after compact" >/dev/null
if [ "$(cat "$ROUTE_TURN" 2>/dev/null)" = "4" ]; then t "monotonic counter resumes after compact" PASS; else t "monotonic counter resumes after compact" FAIL; fi

# Test 13 (C-N4): the prompt is JSON, not a regex slice. `"prompt": *"[^"]*"` stopped at
# the first ESCAPED quote, so a prompt like `ok \"q\" ...` was seen as 4 characters —
# below RECALL_SKIP_SHORT_CHARS — and recall was suppressed on a perfectly long prompt.
cleanup
echo "10" > "$PCT_FILE"
TMPBIN="$(mktemp -d)"
ARGS_FILE="$TMPBIN/args"
cat > "$TMPBIN/uv" <<'EOF'
#!/bin/sh
printf '%s\n' "$*" >> "$SB_TEST_ARGS_FILE"
echo '[]'
EOF
chmod +x "$TMPBIN/uv"
esc_input="{\"session_id\":\"${SESSION_ID}\",\"prompt\":\"ok \\\"q\\\" and then a much longer tail that clears the guard\"}"
echo "$esc_input" | SYMBIOSIS_BRAIN_RECALL_ENABLED=true \
  SYMBIOSIS_BRAIN_TOOLS=/tmp/fake-tools \
  SYMBIOSIS_BRAIN_VAULT=/tmp/fake-vault \
  SYMBIOSIS_BRAIN_RULES_ENABLED=false \
  SB_TEST_ARGS_FILE="$ARGS_FILE" \
  PATH="$TMPBIN:$PATH" \
  bash "$HOOK" prompt-check >/dev/null 2>&1
if [ -s "$ARGS_FILE" ] && ! grep -q -- '--skip-memory' "$ARGS_FILE"; then
  t "escaped quote in prompt does not trip the short-prompt guard" PASS
else
  t "escaped quote in prompt does not trip the short-prompt guard" FAIL
fi
rm -rf "$TMPBIN"

# Test 14: the scope handed to search-gist comes from the resolver, not from a
# fallback. The env var these hooks used to trust is never set in a hook process.
cleanup
echo "10" > "$PCT_FILE"
TMPBIN="$(mktemp -d)"
ARGS_FILE="$TMPBIN/args"
cat > "$TMPBIN/uv" <<'EOF'
#!/bin/sh
printf '%s\n' "$*" >> "$SB_TEST_ARGS_FILE"
echo '{"memory_hits":[{"path":"x.md","title":"X","scope":"s","gist":"g"}],"route_hints":[]}'
EOF
chmod +x "$TMPBIN/uv"
PROJ_DIR="$TMPBIN/proj/sub"
mkdir -p "$PROJ_DIR"
printf '<!-- symbiosis-brain v1: scope=alpha-net -->\n' > "$TMPBIN/proj/CLAUDE.md"
cwd_input="{\"session_id\":\"${SESSION_ID}\",\"cwd\":\"$PROJ_DIR\",\"prompt\":\"a prompt long enough to clear the short-prompt guard\"}"
out=$(echo "$cwd_input" | SYMBIOSIS_BRAIN_RECALL_ENABLED=true \
  SYMBIOSIS_BRAIN_TOOLS=/tmp/fake-tools SYMBIOSIS_BRAIN_VAULT=/tmp/fake-vault \
  SYMBIOSIS_BRAIN_RULES_ENABLED=false SYMBIOSIS_BRAIN_SCOPE= \
  SB_TEST_ARGS_FILE="$ARGS_FILE" PATH="$TMPBIN:$PATH" bash "$HOOK" prompt-check 2>/dev/null)
if grep -q -- "--scope alpha-net" "$ARGS_FILE" 2>/dev/null; then t "scope resolved from payload cwd is passed to search-gist" PASS; else t "scope resolved from payload cwd is passed to search-gist" FAIL; fi
if [[ "$out" == *"scope=alpha-net"* ]]; then t "header names the resolved scope" PASS; else t "header names the resolved scope" FAIL; fi
rm -rf "$TMPBIN"

# Test 15: nothing resolvable -> no --scope flag at all (search every scope) and a
# header that says so. The old code passed --scope global here, a hard filter that
# hid every project note.
cleanup
echo "10" > "$PCT_FILE"
TMPBIN="$(mktemp -d)"
ARGS_FILE="$TMPBIN/args"
cat > "$TMPBIN/uv" <<'EOF'
#!/bin/sh
printf '%s\n' "$*" >> "$SB_TEST_ARGS_FILE"
echo '{"memory_hits":[{"path":"x.md","title":"X","scope":"s","gist":"g"}],"route_hints":[]}'
EOF
chmod +x "$TMPBIN/uv"
BARE_DIR="$TMPBIN/bare/sub"
mkdir -p "$BARE_DIR"
bare_input="{\"session_id\":\"no-bridge-$$\",\"cwd\":\"$BARE_DIR\",\"prompt\":\"a prompt long enough to clear the short-prompt guard\"}"
out=$(echo "$bare_input" | SYMBIOSIS_BRAIN_RECALL_ENABLED=true \
  SYMBIOSIS_BRAIN_TOOLS=/tmp/fake-tools SYMBIOSIS_BRAIN_VAULT=/tmp/fake-vault \
  SYMBIOSIS_BRAIN_RULES_ENABLED=false SYMBIOSIS_BRAIN_SCOPE= \
  SB_TEST_ARGS_FILE="$ARGS_FILE" PATH="$TMPBIN:$PATH" bash "$HOOK" prompt-check 2>/dev/null)
if [ -s "$ARGS_FILE" ] && ! grep -q -- "--scope" "$ARGS_FILE"; then t "unresolved scope omits the --scope flag" PASS; else t "unresolved scope omits the --scope flag" FAIL; fi
if [[ "$out" == *"scope=all*"* ]]; then t "header marks an unresolved scope" PASS; else t "header marks an unresolved scope" FAIL; fi
rm -rf "$TMPBIN"

echo ""
echo "Results: $pass passed, $fail failed"
[ "$fail" -eq 0 ] || exit 1
