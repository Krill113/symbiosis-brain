#!/bin/bash
# Unit tests for brain-session-start.sh hook.
# Usage: bash tests/test-brain-session-start.sh   (run from repo root)

set -u

# Own temp dir: the hook resolves SB_TMP=${TMPDIR:-${TEMP:-/tmp}}, and on Git-Bash for
# Windows /tmp IS the live %LOCALAPPDATA%\Temp — the same dir the running sessions use.
# Pinning /tmp here made the suite rewrite the live brain-current-session and let the
# hook's opportunistic GC delete live brain-mcp-roster-* / brain-route-* files.
SB_TEST_TMP=$(mktemp -d)
export TMPDIR="$SB_TEST_TMP" TEMP="$SB_TEST_TMP"
trap 'rm -rf "$SB_TEST_TMP"' EXIT

# A no-op `claude` on PATH for the WHOLE file. The hook primes its MCP roster by
# running `claude mcp list` in a detached subshell, and every run_hook below is a
# SessionStart payload — so on a machine where the real CLI is installed the suite
# spawned a fresh headless Claude per case. Each of those fires SessionStart itself
# and opens the LIVE brain.db: the vault index log showed bursts of starts with
# different pids every 3-5 seconds, dozens of concurrent writers against one SQLite
# file. Harmless on a Linux runner (no CLI on PATH), destructive on the owner's box.
# The two cases that need a talking stub install their own further down.
mkdir -p "$SB_TEST_TMP/bin"
printf '#!/bin/sh
exit 0
' > "$SB_TEST_TMP/bin/claude"
chmod +x "$SB_TEST_TMP/bin/claude"
export PATH="$SB_TEST_TMP/bin:$PATH"

# The deployed hook is what SessionStart actually runs, so that is what these assert
# on; SB_HOOK points the suite at another copy (a worktree, a candidate build)
# without touching ~/.claude.
HOOK="${SB_HOOK:-$HOME/.claude/hooks/brain-session-start.sh}"
# Repo source-of-truth (used for sourcing normalize_scope helper in tests).
HOOK_SOURCE="${HOOK_SOURCE:-hooks/brain-session-start.sh}"
VAULT="$SB_TEST_TMP/sb-test-vault-$$"
FAKE_ROOT="$SB_TEST_TMP/sb-test-cwd-$$"
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
OUT=$(run_hook "$TMPDIR")
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
  ["$FAKE_ROOT/My/others/Ai/tools/WidgetCompare"]="widget-compare"
  ["$FAKE_ROOT/My/others/symbiosis-brain/sub"]="sub"
)
for cwd in "${!SCOPE_MAP[@]}"; do
  expected="${SCOPE_MAP[$cwd]}"
  OUT=$(run_hook "$cwd")
  assert_contains "regression: $(basename $cwd) → $expected" "$OUT" "\\[scope: $expected\\]"
done

# === Scope override via CLAUDE.md marker (Layer 2 in-hook) ===
# The hook reads the marker so SYMBIOSIS_BRAIN_SCOPE is correct for the
# recall/rules/save hooks. Marker scope wins over the basename heuristic.
setup_vault
PROJ="$FAKE_ROOT/My/alphanets"
mkdir -p "$PROJ"
cat > "$PROJ/CLAUDE.md" <<'EOF'
# Alpha-Сети
<!-- symbiosis-brain v1: scope=alpha-seti, umbrella=alpha -->
EOF
OUT=$(run_hook "$PROJ")
assert_contains "marker-override: basename alphanets → marker alpha-seti" "$OUT" '\[scope: alpha-seti\]'

# Regression: camelCase folder name that does NOT kebab-match the vault scope
# (the LWhisperer → l-whisperer bug; vault scope is "lwhisper"). Marker must win.
setup_vault
PROJ_CC="$FAKE_ROOT/My/others/LWhisperer"
mkdir -p "$PROJ_CC"
cat > "$PROJ_CC/CLAUDE.md" <<'EOF'
# LWhisper
<!-- symbiosis-brain v1: scope=lwhisper -->
EOF
OUT=$(run_hook "$PROJ_CC")
assert_contains "marker-override: LWhisperer (basename l-whisperer) → lwhisper" "$OUT" '\[scope: lwhisper\]'

# Marker parse is whitespace-tolerant and last-marker-wins (mirrors parse_marker).
setup_vault
PROJ_MULTI="$FAKE_ROOT/My/multi"
mkdir -p "$PROJ_MULTI"
cat > "$PROJ_MULTI/CLAUDE.md" <<'EOF'
<!-- symbiosis-brain v1: scope=old-scope -->
text
<!--symbiosis-brain  v2 :  umbrella=x ,  scope=migrated-scope , status=draft-->
EOF
OUT=$(run_hook "$PROJ_MULTI")
assert_contains "marker-override: last marker wins + ws-tolerant → migrated-scope" "$OUT" '\[scope: migrated-scope\]'

# No marker → basename heuristic still applies (no regression for marker-less projects).
setup_vault
PROJ_NM="$FAKE_ROOT/My/PlainFolder"
mkdir -p "$PROJ_NM"
OUT=$(run_hook "$PROJ_NM")
assert_contains "no-marker: basename heuristic preserved" "$OUT" '\[scope: plain-folder\]'

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
  local shown="$TMPDIR/brain-rules-shown-${sid}"
  local turns="$TMPDIR/brain-rules-turn-counter-${sid}"
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
# NOTE: these target the DEPLOYED hook ($HOME/.claude/hooks/brain-session-start.sh,
# or $SB_HOOK). CI installs the hooks in its `setup claude-code` step before running
# the suite, so they are green there. Locally they stay RED until the branch is
# published with `install --repair` — the deployed copy can be months older than the
# working tree. The counter-survives assertion is green either way (the per-session
# rm-block never listed brain-route-turn-<sid>).

# (1) SessionStart primes the MCP roster cache in a detached subshell: with a
# stubbed `claude` on PATH it must write brain-mcp-roster-<sid> (atomic mv).
test_session_start_primes_roster_cache() {
  setup_vault
  local sid="roster-prime-$$"
  local roster="$TMPDIR/brain-mcp-roster-${sid}"
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
  local counter="$TMPDIR/brain-route-turn-${sid}"
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
  local stale_events="$TMPDIR/brain-route-events-stale-${sid}.jsonl"
  local stale_seen="$TMPDIR/brain-route-seen-stale-${sid}.json"
  local stale_turn="$TMPDIR/brain-route-turn-stale-${sid}"
  local stale_roster="$TMPDIR/brain-mcp-roster-stale-${sid}"
  local fresh_turn="$TMPDIR/brain-route-turn-${sid}"
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

# (5) The payload parser must accept whitespace after the colon. json.dumps (used by
# the harness and by tests/test_brain_save_trigger_routing.py) emits
# {"session_id": "x"}; the old grep matched only {"session_id":"x"}, so those
# sessions silently got SESSION_ID="" — no env export, no bridge, no flag cleanup.
test_session_id_parsed_with_space_after_colon() {
  setup_vault
  local sid="space-sid-$$"
  local env_file
  env_file=$(mktemp)
  echo "{\"session_id\": \"${sid}\", \"source\": \"resume\"}" | \
    SYMBIOSIS_BRAIN_VAULT="$VAULT" CLAUDE_ENV_FILE="$env_file" bash "$HOOK" >/dev/null 2>&1
  if grep -q "export CLAUDE_SESSION_ID=\"${sid}\"" "$env_file" && \
     [ "$(cat "$TMPDIR/brain-current-session" 2>/dev/null)" = "$sid" ]; then
    echo "PASS: session_id_parsed_with_space_after_colon"
  else
    echo "FAIL: session_id_parsed_with_space_after_colon — env export or bridge missing"
    FAILED=$((FAILED + 1))
  fi
  rm -f "$env_file"
}

# (6) The last-save marker MUST survive compaction: SessionStart also runs on
# `compact`, and wiping the marker there made the delta-guard count from zero after
# every compact, so the very next threshold fired unconditionally (lens A, finding 4).
test_last_save_marker_survives_compact() {
  setup_vault
  local sid="compact-marker-$$"
  local marker="$TMPDIR/brain-last-save-pct-${sid}"
  local triggered="$TMPDIR/brain-triggered-${sid}"
  echo "31" > "$marker"
  echo "25" > "$triggered"

  echo "{\"session_id\":\"${sid}\",\"source\":\"compact\"}" | \
    SYMBIOSIS_BRAIN_VAULT="$VAULT" CLAUDE_ENV_FILE="" bash "$HOOK" >/dev/null 2>&1

  local ok=1
  [ "$(cat "$marker" 2>/dev/null)" = "31" ] || { ok=0; echo "  marker wiped: $marker"; }
  [ -f "$triggered" ] && { ok=0; echo "  trigger flags NOT cleaned: $triggered"; }
  if [ "$ok" = "1" ]; then
    echo "PASS: last_save_marker_survives_compact"
  else
    echo "FAIL: last_save_marker_survives_compact"
    FAILED=$((FAILED + 1))
  fi
  rm -f "$marker" "$triggered"
}

# (7) Prompt-recall dedup files (CP-5, prefix brain-prompt-recall-seen-) must be
# swept by the same opportunistic GC as the other per-session temp files.
test_prompt_recall_seen_files_gc_when_stale() {
  setup_vault
  local sid="gc-prompt-$$"
  local stale="$TMPDIR/brain-prompt-recall-seen-stale-${sid}.json"
  echo "{}" > "$stale"
  touch -d "120 minutes ago" "$stale" 2>/dev/null || \
    touch -t "$(date -d '120 minutes ago' +%Y%m%d%H%M 2>/dev/null)" "$stale" 2>/dev/null

  echo "{\"session_id\":\"${sid}\",\"source\":\"startup\"}" | \
    SYMBIOSIS_BRAIN_VAULT="$VAULT" CLAUDE_ENV_FILE="" bash "$HOOK" >/dev/null 2>&1

  if [ ! -f "$stale" ]; then
    echo "PASS: prompt_recall_seen_files_gc_when_stale"
  else
    echo "FAIL: prompt_recall_seen_files_gc_when_stale — stale dedup file survived"
    FAILED=$((FAILED + 1))
    rm -f "$stale"
  fi
}

# (9) The roster prime must not re-run when a fresh roster for this session exists:
# `claude mcp list` health-checks every server and starts a second serve against the
# live vault. A1a made SessionStart fire on resume/clear too, so without this gate the
# extra starts multiply.
test_roster_prime_skipped_when_roster_is_fresh() {
  setup_vault
  local sid="roster-gate-$$"
  local roster="$TMPDIR/brain-mcp-roster-${sid}"
  local stub_dir="$TMPDIR/stub-$$"
  mkdir -p "$stub_dir"
  printf '#!/bin/bash\necho called >> "%s/claude-calls"\n' "$TMPDIR" > "$stub_dir/claude"
  chmod +x "$stub_dir/claude"
  rm -f "$TMPDIR/claude-calls"
  printf 'symbiosis-brain: ok\n' > "$roster"      # fresh by construction

  echo "{\"session_id\":\"${sid}\",\"source\":\"resume\"}" | \
    PATH="$stub_dir:$PATH" SYMBIOSIS_BRAIN_VAULT="$VAULT" CLAUDE_ENV_FILE="" \
    bash "$HOOK" >/dev/null 2>&1
  sleep 1                                          # the prime, if any, is detached

  if [ ! -f "$TMPDIR/claude-calls" ]; then
    echo "PASS: roster_prime_skipped_when_roster_is_fresh"
  else
    echo "FAIL: roster_prime_skipped_when_roster_is_fresh — claude mcp list ran anyway"
    FAILED=$((FAILED + 1))
  fi
  rm -rf "$stub_dir" "$roster" "$TMPDIR/claude-calls"
}

# (8) A failed vault sync has no output channel of its own (SessionEnd is mute), so
# the alarm is surfaced on the next session start, next to CRITICAL_FACTS.
test_session_start_shows_sync_alarm() {
  setup_vault
  local sid="sync-alarm-$$"
  printf 'stage=conflict at=2026-08-25T21:15:00+03:00\n' > "$TMPDIR/brain-sync-failed"
  local out
  out=$(echo "{\"session_id\":\"${sid}\",\"source\":\"startup\"}" | \
        SYMBIOSIS_BRAIN_VAULT="$VAULT" CLAUDE_ENV_FILE="" bash "$HOOK" 2>/dev/null)
  rm -f "$TMPDIR/brain-sync-failed"
  if echo "$out" | grep -q "vault sync failed (conflict, 2026-08-25T21:15:00+03:00)"; then
    echo "PASS: session_start_shows_sync_alarm"
  else
    echo "FAIL: session_start_shows_sync_alarm — banner missing"
    FAILED=$((FAILED + 1))
  fi
}

test_session_id_parsed_with_space_after_colon
test_last_save_marker_survives_compact
test_prompt_recall_seen_files_gc_when_stale
test_roster_prime_skipped_when_roster_is_fresh
test_session_start_shows_sync_alarm

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
