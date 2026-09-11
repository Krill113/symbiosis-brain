#!/bin/bash
# tests/test-mcp-roster-cache.sh — SessionStart shares one `claude mcp list` answer
# per cwd (plan A-2; src/symbiosis_brain/mcp_roster.py). Each `mcp list` call stands
# for a full health-check poll, which starts every stdio server — including a second
# `symbiosis-brain serve` on the live vault. The stub counts those calls.
#
# Run from the repo root:  bash tests/test-mcp-roster-cache.sh
# Against the working tree instead of the deployed hook:
#   SB_HOOK=hooks/brain-session-start.sh bash tests/test-mcp-roster-cache.sh
#
# Isolation: TMPDIR/TEMP, HOME and USERPROFILE all point into a throwaway dir. The
# cache key reads the config files under home, and nothing here may read — let alone
# write — the real ~/.claude. The vault points at a path that does not exist, so the
# hook's detached prewarm is a no-op. The python side runs from THIS checkout.

HOOK="${SB_HOOK:-$HOME/.claude/hooks/brain-session-start.sh}"
if [ ! -f "$HOOK" ]; then
  echo "FATAL: hook not found at $HOOK"
  exit 1
fi
HOOK="$(cd "$(dirname "$HOOK")" && pwd)/$(basename "$HOOK")"   # survives the cd below
export SYMBIOSIS_BRAIN_TOOLS="$PWD"

SB_TEST_TMP=$(mktemp -d)
trap 'rm -rf "$SB_TEST_TMP"' EXIT
export TMPDIR="$SB_TEST_TMP" TEMP="$SB_TEST_TMP"
export HOME="$SB_TEST_TMP/home" USERPROFILE="$SB_TEST_TMP/home"
export SYMBIOSIS_BRAIN_VAULT="$SB_TEST_TMP/no-vault"
export SYMBIOSIS_BRAIN_DEBUG_LOG="$SB_TEST_TMP/debug.log"
mkdir -p "$HOME/.claude"
printf '{"mcpServers": {"serena": {"command": "uvx"}}, "projects": {}}' >"$HOME/.claude.json"

# One cold `uv run` before the first case: the check runs under `timeout 3`, and a
# first start that times out would poll and turn a PASS into noise.
uv run --quiet --directory "$SYMBIOSIS_BRAIN_TOOLS" python -c "import symbiosis_brain.mcp_roster" >/dev/null 2>&1

FAILED=0
pass() { echo "PASS: $1"; }
fail() { echo "FAIL: $1 — $2"; FAILED=$((FAILED + 1)); }

# $1 = dir, $2 = ok | banner | fail
make_stub() {
  mkdir -p "$1"
  cat >"$1/claude" <<EOF
#!/bin/sh
# \`--version\` answers without counting: the cache key probes it. \`mcp list\` counts.
[ "\$1" = "--version" ] && { echo "9.9.9 (stub)"; exit 0; }
[ "\$1" = "mcp" ] || exit 0
echo x >>"$1/mcp-calls"
case "$2" in
  fail) exit 1 ;;
  banner) printf 'Checking MCP server health...\n\n'; exit 0 ;;
esac
printf 'Checking MCP server health...\n\n'
echo "serena: uvx serena - ✓ Connected"
echo "duckduckgo: npx ddg - ✓ Connected"
EOF
  chmod +x "$1/claude"
}

calls() { if [ -f "$1/mcp-calls" ]; then wc -l <"$1/mcp-calls" | tr -d ' '; else echo 0; fi; }

# $1 = sid, $2 = stub dir, $3 = cwd; extra VAR=value pairs may precede via env.
start() {
  ( cd "$3" && printf '{"session_id":"%s","source":"startup"}' "$1" | \
      PATH="$2:$PATH" CLAUDE_ENV_FILE="" bash "$HOOK" >/dev/null 2>&1 )
}

# Wait until the stub saw $2 polls AND the detached subshell of session $3 is done
# (its .tmp is created before the poll and removed or moved after publish).
settle() {
  local i=0
  while [ "$(calls "$1")" -lt "$2" ] && [ "$i" -lt 150 ]; do sleep 0.1; i=$((i + 1)); done
  i=0
  while [ -e "$TMPDIR/brain-mcp-roster-$3.tmp" ] && [ "$i" -lt 150 ]; do sleep 0.1; i=$((i + 1)); done
  sleep 0.3
}

shared_key() { ls "$TMPDIR"/brain-mcp-shared-*.key 2>/dev/null | head -1; }
reset() { rm -f "$TMPDIR"/brain-mcp-shared-* "$TMPDIR"/brain-mcp-roster-* "$SYMBIOSIS_BRAIN_DEBUG_LOG"; }

# T1: two sessions, one cwd — the second is served from the shared answer.
test_one_poll_for_two_sessions_in_one_cwd() {
  reset
  local d="$SB_TEST_TMP/t1"; mkdir -p "$d/cwd"; make_stub "$d/bin" ok
  start t1a "$d/bin" "$d/cwd"; settle "$d/bin" 1 t1a
  start t1b "$d/bin" "$d/cwd"; sleep 2
  local n; n=$(calls "$d/bin")
  if [ "$n" = 1 ] && grep -q duckduckgo "$TMPDIR/brain-mcp-roster-t1a" 2>/dev/null \
     && grep -q duckduckgo "$TMPDIR/brain-mcp-roster-t1b" 2>/dev/null; then
    pass one_poll_for_two_sessions_in_one_cwd
  else
    fail one_poll_for_two_sessions_in_one_cwd "mcp list calls=$n (want 1); key=$(shared_key)"
  fi
}

# T2: the stored key disagrees on cwd — a miss, logged WITH the field name.
test_moved_field_misses_and_names_it() {
  reset
  local d="$SB_TEST_TMP/t2"; mkdir -p "$d/cwd"; make_stub "$d/bin" ok
  start t2a "$d/bin" "$d/cwd"; settle "$d/bin" 1 t2a
  local key; key=$(shared_key)
  sed -i 's|"cwd": "[^"]*"|"cwd": "/elsewhere"|' "$key" 2>/dev/null
  start t2b "$d/bin" "$d/cwd"; settle "$d/bin" 2 t2b
  local n; n=$(calls "$d/bin")
  if [ "$n" = 2 ] && grep -q "roster: miss cwd" "$SYMBIOSIS_BRAIN_DEBUG_LOG" 2>/dev/null; then
    pass moved_field_misses_and_names_it
  else
    fail moved_field_misses_and_names_it "calls=$n (want 2); log: $(grep roster "$SYMBIOSIS_BRAIN_DEBUG_LOG" 2>/dev/null | tail -2)"
  fi
}

# T3: a shared answer older than a day is not trusted, and is replaced.
test_stale_shared_answer_is_repolled() {
  reset
  local d="$SB_TEST_TMP/t3"; mkdir -p "$d/cwd"; make_stub "$d/bin" ok
  start t3a "$d/bin" "$d/cwd"; settle "$d/bin" 1 t3a
  local key; key=$(shared_key); local shared="${key%.key}"
  touch -d "25 hours ago" "$shared" 2>/dev/null
  start t3b "$d/bin" "$d/cwd"; settle "$d/bin" 2 t3b
  local n; n=$(calls "$d/bin")
  if [ "$n" = 2 ] && grep -q "roster: miss ttl" "$SYMBIOSIS_BRAIN_DEBUG_LOG" 2>/dev/null \
     && [ -n "$(find "$shared" -mmin -5 2>/dev/null)" ]; then
    pass stale_shared_answer_is_repolled
  else
    fail stale_shared_answer_is_repolled "calls=$n (want 2); shared fresh? $(find "$shared" -mmin -5 2>/dev/null)"
  fi
}

# T6: a failed poll, or one that printed only the banner, publishes nothing at all.
test_broken_poll_publishes_nothing() {
  local mode
  for mode in fail banner; do
    reset
    local d="$SB_TEST_TMP/t6-$mode"; mkdir -p "$d/cwd"; make_stub "$d/bin" "$mode"
    start "t6$mode" "$d/bin" "$d/cwd"; settle "$d/bin" 1 "t6$mode"
    if [ ! -e "$TMPDIR/brain-mcp-roster-t6$mode" ] && [ -z "$(shared_key)" ]; then
      pass "broken_poll_publishes_nothing[$mode]"
    else
      fail "broken_poll_publishes_nothing[$mode]" "per-session: $(ls "$TMPDIR"/brain-mcp-roster-t6$mode 2>/dev/null); key: $(shared_key)"
    fi
  done
}

# Fail-open: without a usable python side the hook behaves as before the cache —
# one poll, the per-session file, no shared answer.
test_no_python_side_falls_back_to_the_per_session_poll() {
  reset
  local d="$SB_TEST_TMP/t7"; mkdir -p "$d/cwd"; make_stub "$d/bin" ok
  ( cd "$d/cwd" && printf '{"session_id":"t7","source":"startup"}' | \
      PATH="$d/bin:$PATH" CLAUDE_ENV_FILE="" SYMBIOSIS_BRAIN_TOOLS="$SB_TEST_TMP/no-tools" \
      bash "$HOOK" >/dev/null 2>&1 )
  settle "$d/bin" 1 t7
  if grep -q duckduckgo "$TMPDIR/brain-mcp-roster-t7" 2>/dev/null && [ -z "$(shared_key)" ]; then
    pass no_python_side_falls_back_to_the_per_session_poll
  else
    fail no_python_side_falls_back_to_the_per_session_poll "per-session: $(ls "$TMPDIR"/brain-mcp-roster-t7 2>/dev/null); key: $(shared_key)"
  fi
}

# GC: the shared answer outlives the 60-minute sweep of per-session files, not a day.
test_shared_answer_swept_after_a_day_not_an_hour() {
  reset
  local d="$SB_TEST_TMP/gc"; mkdir -p "$d/cwd"; make_stub "$d/bin" ok
  echo "serena: x - ✓ Connected" >"$TMPDIR/brain-mcp-roster-gc"   # fresh: no prime
  for f in brain-mcp-shared-hour brain-mcp-shared-hour.key; do
    : >"$TMPDIR/$f"; touch -d "120 minutes ago" "$TMPDIR/$f" 2>/dev/null
  done
  for f in brain-mcp-shared-day brain-mcp-shared-day.key; do
    : >"$TMPDIR/$f"; touch -d "1500 minutes ago" "$TMPDIR/$f" 2>/dev/null
  done
  start gc "$d/bin" "$d/cwd"
  if [ -e "$TMPDIR/brain-mcp-shared-hour" ] && [ -e "$TMPDIR/brain-mcp-shared-hour.key" ] \
     && [ ! -e "$TMPDIR/brain-mcp-shared-day" ] && [ ! -e "$TMPDIR/brain-mcp-shared-day.key" ] \
     && [ "$(calls "$d/bin")" = 0 ]; then
    pass shared_answer_swept_after_a_day_not_an_hour
  else
    fail shared_answer_swept_after_a_day_not_an_hour "left: $(ls "$TMPDIR" | grep brain-mcp-shared | tr '\n' ' ')"
  fi
}

test_one_poll_for_two_sessions_in_one_cwd
test_moved_field_misses_and_names_it
test_stale_shared_answer_is_repolled
test_broken_poll_publishes_nothing
test_no_python_side_falls_back_to_the_per_session_poll
test_shared_answer_swept_after_a_day_not_an_hour

echo
if [ "$FAILED" -eq 0 ]; then
  echo "All roster-cache tests passed"
  exit 0
fi
echo "$FAILED roster-cache test(s) failed"
exit 1
