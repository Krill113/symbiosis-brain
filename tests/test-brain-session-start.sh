#!/bin/bash
# Unit tests for brain-session-start.sh hook.
# Usage: bash tests/test-brain-session-start.sh   (run from repo root)

set -u

HOOK="$HOME/.claude/hooks/brain-session-start.sh"
# Repo source-of-truth (used for sourcing normalize_scope helper in tests).
HOOK_SOURCE="${HOOK_SOURCE:-hooks/brain-session-start.sh}"
VAULT="/tmp/sb-test-vault-$$"
FAKE_ROOT="/tmp/sb-test-cwd-$$"
FAILED=0

if [ ! -f "$HOOK" ]; then
  echo "FATAL: hook not found at $HOOK"
  exit 1
fi

if [ ! -f "$HOOK_SOURCE" ]; then
  echo "FATAL: repo hook source not found at $HOOK_SOURCE"
  exit 1
fi

# === bash normalize_scope contract: must match Python ===
test_normalize() {
  local input="$1" expected="$2"
  local got
  got=$(bash -c "source \"$HOOK_SOURCE\" --source-only-normalize; normalize_scope \"$input\"")
  if [ "$got" = "$expected" ]; then
    echo "PASS: normalize($input) → $expected"
  else
    echo "FAIL: normalize($input) — expected '$expected', got '$got'"
    FAILED=$((FAILED + 1))
  fi
}

test_normalize "AlphaDiagnostics" "alpha-diagnostics"
test_normalize "Alpha.Pdf"        "alpha-pdf"
test_normalize "my_cool_app"       "my-cool-app"
test_normalize "beta"                "beta"
test_normalize "Alpha-Local"      "alpha-local"
test_normalize "ABCService"        "abc-service"
test_normalize "Project2026"       "project2026"
test_normalize ""                  ""

# === setup helpers (used by remaining e2e tests) ===

setup_vault() {
  rm -rf "$VAULT"
  mkdir -p "$VAULT/projects"
  cat > "$VAULT/CRITICAL_FACTS.md" <<'EOF'
---
name: Critical Facts
type: wiki
---
User: test-user
EOF
}

setup_fake_dirs() {
  rm -rf "$FAKE_ROOT"
  mkdir -p "$FAKE_ROOT/My/beta"
  mkdir -p "$FAKE_ROOT/My/alphanets"
  mkdir -p "$FAKE_ROOT/My/AlphaDetails"
  mkdir -p "$FAKE_ROOT/My/Alpha.Pdf"
  mkdir -p "$FAKE_ROOT/My/Alpha-Local"
  mkdir -p "$FAKE_ROOT/My/alpha-faq"
  mkdir -p "$FAKE_ROOT/My/alphalib"
  mkdir -p "$FAKE_ROOT/My/others/Ai/tools/WidgetCompare"
  mkdir -p "$FAKE_ROOT/My/others/symbiosis-brain/sub"
}

# Run hook with given CWD, capture stdout.
run_hook() {
  local cwd="$1"
  local input='{"session_id":"test-sess","source":"startup"}'
  ( cd "$cwd" && \
    echo "$input" | SYMBIOSIS_BRAIN_VAULT="$VAULT" CLAUDE_ENV_FILE="" bash "$HOOK" )
}

assert_contains() {
  local name="$1" output="$2" pattern="$3"
  if echo "$output" | grep -qE "$pattern"; then
    echo "PASS: $name"
  else
    echo "FAIL: $name — expected pattern: $pattern"
    echo "--- output ---"; echo "$output"; echo "--- end ---"
    FAILED=$((FAILED + 1))
  fi
}

assert_not_contains() {
  local name="$1" output="$2" pattern="$3"
  if echo "$output" | grep -qE "$pattern"; then
    echo "FAIL: $name — unexpected pattern: $pattern"
    echo "--- output ---"; echo "$output"; echo "--- end ---"
    FAILED=$((FAILED + 1))
  else
    echo "PASS: $name"
  fi
}

setup_fake_dirs

# === Core infrastructure: CRITICAL_FACTS always injected ===
setup_vault
OUT=$(run_hook "/tmp")
assert_contains "core: Symbiosis Brain marker present"  "$OUT" '=== Symbiosis Brain ==='
assert_contains "core: CRITICAL_FACTS content"          "$OUT" 'User: test-user'

# === Scope detection without marker — pure normalize_scope basename ===
setup_vault
declare -A SCOPE_MAP=(
  ["$FAKE_ROOT/My/beta"]="beta"
  ["$FAKE_ROOT/My/alphanets"]="alphanets"
  ["$FAKE_ROOT/My/AlphaDetails"]="alpha-details"
  ["$FAKE_ROOT/My/Alpha.Pdf"]="alpha-pdf"
  ["$FAKE_ROOT/My/Alpha-Local"]="alpha-local"
  ["$FAKE_ROOT/My/alpha-faq"]="alpha-faq"
  ["$FAKE_ROOT/My/alphalib"]="alphalib"
  ["$FAKE_ROOT/My/others/Ai/tools/WidgetCompare"]="api-diff-tool"
  ["$FAKE_ROOT/My/others/symbiosis-brain/sub"]="sub"
)
for cwd in "${!SCOPE_MAP[@]}"; do
  expected="${SCOPE_MAP[$cwd]}"
  OUT=$(run_hook "$cwd")
  assert_contains "regression: $(basename $cwd) → $expected" "$OUT" "\\[scope: $expected\\]"
done

# === Scope override via CLAUDE.md marker ===
setup_vault
PROJ="$FAKE_ROOT/My/alphanets"
mkdir -p "$PROJ"
cat > "$PROJ/CLAUDE.md" <<'EOF'
# Alpha-Сети
<!-- symbiosis-brain v1: scope=alpha-seti, umbrella=alpha -->
EOF
OUT=$(run_hook "$PROJ")
# Hook itself does NOT read marker — it stays naive (basename normalize).
# Marker override is applied by brain-init skill at Layer 2.
# So hook output remains "alphanets" here — this is INTENTIONAL.
assert_contains "marker-override is skill-level, not hook-level" "$OUT" '\[scope: alphanets\]'

# === SYMBIOSIS_BRAIN_SCOPE env contract for A-plan ===
setup_vault
ENV_FILE=$(mktemp)
( cd "$FAKE_ROOT/My/AlphaDetails" && \
  echo '{"session_id":"s1","source":"startup"}' | \
  SYMBIOSIS_BRAIN_VAULT="$VAULT" CLAUDE_ENV_FILE="$ENV_FILE" bash "$HOOK" >/dev/null )
assert_contains "env-contract: SYMBIOSIS_BRAIN_SCOPE exported" \
  "$(cat $ENV_FILE)" 'export SYMBIOSIS_BRAIN_SCOPE="alpha-details"'
rm -f "$ENV_FILE"

# === A-plan additions: rules flag cleanup + tool roster ===
test_session_start_cleans_rules_flags() {
  local sid="cleanup-$$"
  local shown="/tmp/brain-rules-shown-${sid}"
  local turns="/tmp/brain-rules-turn-counter-${sid}"
  echo "30" > "$shown"
  echo "5" > "$turns"

  setup_vault
  echo "{\"session_id\":\"${sid}\"}" | SYMBIOSIS_BRAIN_VAULT="$VAULT" CLAUDE_ENV_FILE="" bash "$HOOK" >/dev/null

  if [ -f "$shown" ] || [ -f "$turns" ]; then
    rm -f "$shown" "$turns"
    echo "FAIL: session_start_cleans_rules_flags — files not cleaned"
    FAILED=$((FAILED + 1))
  else
    echo "PASS: session_start_cleans_rules_flags"
  fi
}

test_session_start_emits_tool_roster() {
  setup_vault
  local out
  out=$(echo "{\"session_id\":\"roster-$$\"}" | SYMBIOSIS_BRAIN_VAULT="$VAULT" CLAUDE_ENV_FILE="" bash "$HOOK" 2>/dev/null)
  if echo "$out" | grep -q "Available tools:"; then
    echo "PASS: session_start_emits_tool_roster"
  else
    echo "FAIL: session_start_emits_tool_roster — no roster found in output"
    FAILED=$((FAILED + 1))
  fi
}

test_session_start_cleans_rules_flags
test_session_start_emits_tool_roster

# === Stage-4 routing: roster prime + route-file GC + counter exclusion ===
# NOTE: these target the DEPLOYED hook ($HOME/.claude/hooks/brain-session-start.sh).
# Until a go-live redeploy publishes the Stage-4 bash edits, the roster-prime and
# stale-GC assertions are RED-BY-DESIGN. The counter-survives assertion is GREEN
# already (the per-session rm-block never listed brain-route-turn-<sid>).

# (1) SessionStart primes the MCP roster cache in a detached subshell: with a
# stubbed `claude` on PATH it must write brain-mcp-roster-<sid> (atomic mv).
test_session_start_primes_roster_cache() {
  setup_vault
  local sid="roster-prime-$$"
  local roster="/tmp/brain-mcp-roster-${sid}"
  rm -f "$roster"
  local stub_bin
  stub_bin="$(mktemp -d)"
  cat > "$stub_bin/claude" <<'EOF'
#!/bin/sh
# stub `claude mcp list` — mimic the connected-roster stdout
echo "serena: uvx ... - ✓ Connected"
echo "duckduckgo: npx ... - ✓ Connected"
EOF
  chmod +x "$stub_bin/claude"

  echo "{\"session_id\":\"${sid}\",\"source\":\"startup\"}" | \
    PATH="$stub_bin:$PATH" SYMBIOSIS_BRAIN_VAULT="$VAULT" CLAUDE_ENV_FILE="" \
    bash "$HOOK" >/dev/null 2>&1

  # Background subshell writes the cache async — wait briefly for atomic publish.
  local waited=0
  while [ ! -f "$roster" ] && [ "$waited" -lt 50 ]; do
    sleep 0.1
    waited=$((waited + 1))
  done

  if [ -f "$roster" ] && grep -q "duckduckgo" "$roster"; then
    echo "PASS: session_start_primes_roster_cache"
  else
    echo "FAIL: session_start_primes_roster_cache — roster cache not written at $roster"
    FAILED=$((FAILED + 1))
  fi
  rm -f "$roster"
  rm -rf "$stub_bin"
}

# (2) The monotonic routing counter (brain-route-turn-<sid>) MUST survive a
# SessionStart run (it is deliberately EXCLUDED from the per-session rm-block).
test_route_turn_counter_survives_session_start() {
  setup_vault
  local sid="counter-survive-$$"
  local counter="/tmp/brain-route-turn-${sid}"
  echo "7" > "$counter"

  echo "{\"session_id\":\"${sid}\",\"source\":\"compact\"}" | \
    SYMBIOSIS_BRAIN_VAULT="$VAULT" CLAUDE_ENV_FILE="" bash "$HOOK" >/dev/null 2>&1

  if [ -f "$counter" ] && [ "$(cat "$counter" 2>/dev/null)" = "7" ]; then
    echo "PASS: route_turn_counter_survives_session_start"
  else
    echo "FAIL: route_turn_counter_survives_session_start — counter removed/changed by rm-block"
    FAILED=$((FAILED + 1))
  fi
  rm -f "$counter"
}

# (3) Opportunistic GC sweeps STALE route temp files (mtime > 60min) while
# leaving FRESH ones (including a fresh counter) untouched.
test_route_files_gc_when_stale() {
  setup_vault
  local sid="gc-$$"
  local stale_events="/tmp/brain-route-events-stale-${sid}.jsonl"
  local stale_seen="/tmp/brain-route-seen-stale-${sid}.json"
  local stale_turn="/tmp/brain-route-turn-stale-${sid}"
  local stale_roster="/tmp/brain-mcp-roster-stale-${sid}"
  local fresh_turn="/tmp/brain-route-turn-${sid}"
  for f in "$stale_events" "$stale_seen" "$stale_turn" "$stale_roster"; do
    echo "x" > "$f"
    touch -d "120 minutes ago" "$f" 2>/dev/null || touch -t "$(date -d '120 minutes ago' +%Y%m%d%H%M 2>/dev/null)" "$f" 2>/dev/null
  done
  echo "3" > "$fresh_turn"

  echo "{\"session_id\":\"${sid}\",\"source\":\"startup\"}" | \
    SYMBIOSIS_BRAIN_VAULT="$VAULT" CLAUDE_ENV_FILE="" bash "$HOOK" >/dev/null 2>&1

  local ok=1
  for f in "$stale_events" "$stale_seen" "$stale_turn" "$stale_roster"; do
    if [ -f "$f" ]; then ok=0; echo "  stale survived: $f"; fi
  done
  if [ ! -f "$fresh_turn" ]; then ok=0; echo "  fresh counter wrongly deleted: $fresh_turn"; fi

  if [ "$ok" = "1" ]; then
    echo "PASS: route_files_gc_when_stale"
  else
    echo "FAIL: route_files_gc_when_stale — stale route files not swept or fresh deleted"
    FAILED=$((FAILED + 1))
  fi
  rm -f "$stale_events" "$stale_seen" "$stale_turn" "$stale_roster" "$fresh_turn"
}

# (4) Structural guard (GREEN now, deploy-independent): the per-session rm-block
# must contain ZERO brain-route entries — the monotonic counter is excluded by
# design so it survives compact (stage4 design §6.2).
test_route_turn_excluded_from_sessionstart_rm() {
  # Extract the per-session rm-block from the REPO source-of-truth and assert
  # no brain-route-* path is listed inside it.
  local block
  block=$(awk '/^if \[ -n "\$SESSION_ID" \]; then/{c=1} c{print} /brain-current-session/{if(c)exit}' "$HOOK_SOURCE")
  local n
  n=$(printf '%s\n' "$block" | grep -c 'rm -f' )
  # Count brain-route ONLY on the rm-target path lines, never in comments — the
  # exclusion is documented by a comment that legitimately names the file.
  local routes
  routes=$(printf '%s\n' "$block" | grep -v '^[[:space:]]*#' | grep -c 'brain-route')
  if [ "$n" -ge 1 ] && [ "$routes" = "0" ]; then
    echo "PASS: route_turn_excluded_from_sessionstart_rm"
  else
    echo "FAIL: route_turn_excluded_from_sessionstart_rm — rm-block has $routes brain-route entr(ies) (must be 0)"
    FAILED=$((FAILED + 1))
  fi
}

test_session_start_primes_roster_cache
test_route_turn_counter_survives_session_start
test_route_files_gc_when_stale
test_route_turn_excluded_from_sessionstart_rm

# Cleanup
rm -rf "$VAULT" "$FAKE_ROOT"

echo ""
if [ "$FAILED" -eq 0 ]; then
  echo "All tests PASSED"
  exit 0
else
  echo "$FAILED test(s) FAILED"
  exit 1
fi
