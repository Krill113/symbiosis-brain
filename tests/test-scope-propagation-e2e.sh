#!/bin/bash
# End-to-end scope propagation, run under the REAL production condition: the channel
# the hooks used to rely on (SessionStart -> CLAUDE_ENV_FILE -> SYMBIOSIS_BRAIN_SCOPE)
# is deliberately absent, because Claude Code sources that file before Bash TOOL
# commands only — never before a hook or the status line.
#
# Every previous test asserted that SessionStart WROTE the scope. None asserted that a
# consumer RECEIVED it, which is why a defect that pinned every recall to scope=global
# survived three months of green suites.
#
# Point it at another hooks directory to compare builds:
#   SB_HOOKS_DIR=~/.claude/hooks bash tests/test-scope-propagation-e2e.sh
set -u

HOOKS="${SB_HOOKS_DIR:-$(cd "$(dirname "$0")/.." && pwd)/hooks}"

WORK="$(mktemp -d)"
export TMPDIR="$WORK" TEMP="$WORK"
trap 'rm -rf "$WORK"' EXIT

# The broken channel, reproduced: no env var, no env file.
unset SYMBIOSIS_BRAIN_SCOPE
export CLAUDE_ENV_FILE=""

pass=0
fail=0
t() {
  if [ "$2" = "PASS" ]; then echo "✓ $1"; pass=$((pass+1)); else echo "✗ $1 ${3:+(got: $3)}"; fail=$((fail+1)); fi
}

# ── fixtures ──────────────────────────────────────────────────────────────────
VAULT="$WORK/vault"
mkdir -p "$VAULT/projects" "$VAULT/reference"
cat > "$VAULT/CRITICAL_FACTS.md" <<'EOF'
---
type: wiki
---
User: test-user
EOF
cat > "$VAULT/reference/scope-taxonomy.md" <<'TAX'
| Scope | Тип | Что хранит |
|---|---|---|
| `global` | cross-project | patterns |
| `fixture-net` | product | product notes |
TAX

PROJ="$WORK/fixture-net"
mkdir -p "$PROJ/src/deep"
cat > "$PROJ/CLAUDE.md" <<'EOF'
# Fixture project
<!-- symbiosis-brain v1: scope=fixture-net -->
EOF

BIN="$WORK/bin"
mkdir -p "$BIN"
# `claude` is called by SessionStart to prime its MCP roster; a talking binary would
# spawn a real headless session per run.
printf '#!/bin/sh\nexit 0\n' > "$BIN/claude"
# `uv` stands in for the search-gist call and records the arguments it was given.
cat > "$BIN/uv" <<'EOF'
#!/bin/sh
printf '%s\n' "$*" >> "$SB_TEST_ARGS_FILE"
echo '{"memory_hits":[{"path":"x.md","title":"X","scope":"fixture-net","gist":"g"}],"route_hints":[]}'
EOF
chmod +x "$BIN/claude" "$BIN/uv"
export PATH="$BIN:$PATH"

SID="e2e-scope-$$"
ARGS_FILE="$WORK/gist-args"
: > "$ARGS_FILE"

# ── 1. SessionStart resolves and publishes the scope ──────────────────────────
START_OUT=$( cd "$PROJ/src/deep" && \
  echo "{\"session_id\":\"$SID\",\"source\":\"startup\"}" | \
  SYMBIOSIS_BRAIN_VAULT="$VAULT" bash "$HOOKS/brain-session-start.sh" 2>/dev/null )

case "$START_OUT" in
  *"[scope: fixture-net]"*) t "session-start resolves the marker from a subdirectory" PASS ;;
  *) t "session-start resolves the marker from a subdirectory" FAIL "$(echo "$START_OUT" | grep -o '\[scope:[^]]*\]')" ;;
esac

BRIDGE="$WORK/brain-scope-${SID}"
if [ -r "$BRIDGE" ] && [ "$(cat "$BRIDGE")" = "fixture-net" ]; then
  t "session-start publishes the per-session bridge file" PASS
else
  t "session-start publishes the per-session bridge file" FAIL "$(cat "$BRIDGE" 2>/dev/null)"
fi

# ── 2. The recall hook searches that scope ────────────────────────────────────
PROMPT_INPUT="{\"session_id\":\"$SID\",\"cwd\":\"$PROJ/src/deep\",\"prompt\":\"a prompt long enough to clear the short-prompt guard\"}"
PROMPT_OUT=$( echo "$PROMPT_INPUT" | \
  SYMBIOSIS_BRAIN_RECALL_ENABLED=true SYMBIOSIS_BRAIN_RULES_ENABLED=false \
  SYMBIOSIS_BRAIN_TOOLS="$WORK/fake-tools" SYMBIOSIS_BRAIN_VAULT="$VAULT" \
  SB_TEST_ARGS_FILE="$ARGS_FILE" \
  bash "$HOOKS/brain-save-trigger.sh" prompt-check 2>/dev/null )

if grep -q -- "--scope fixture-net" "$ARGS_FILE"; then
  t "recall hook passes the resolved scope to search-gist" PASS
else
  t "recall hook passes the resolved scope to search-gist" FAIL "$(tr '\n' ' ' < "$ARGS_FILE")"
fi

case "$PROMPT_OUT" in
  *"scope=fixture-net"*) t "injected header names the resolved scope" PASS ;;
  *) t "injected header names the resolved scope" FAIL "$(echo "$PROMPT_OUT" | grep -o 'scope=[^]]*')" ;;
esac

# The regression itself: 'global' must appear nowhere in this run.
if grep -q -- "--scope global" "$ARGS_FILE"; then
  t "recall hook never falls back to scope=global" FAIL
else
  t "recall hook never falls back to scope=global" PASS
fi
case "$PROMPT_OUT" in
  *"scope=global"*) t "injected header never says scope=global" FAIL ;;
  *) t "injected header never says scope=global" PASS ;;
esac

# ── 3. The status line shows what recall will use ─────────────────────────────
LINE_OUT=$( echo "{\"session_id\":\"$SID\",\"cwd\":\"$PROJ/src/deep\"}" | bash "$HOOKS/sb-line.sh" 2>/dev/null )
case "$LINE_OUT" in
  *"scope: fixture-net"*) t "status line shows the resolved scope" PASS ;;
  *) t "status line shows the resolved scope" FAIL "$(echo "$LINE_OUT" | grep -o 'scope: [^ ]*')" ;;
esac

# ── 4. A session with nothing to resolve says so, instead of guessing ─────────
BARE="$WORK/bare/sub"
mkdir -p "$BARE"
BARE_SID="e2e-bare-$$"
: > "$ARGS_FILE"
BARE_OUT=$( echo "{\"session_id\":\"$BARE_SID\",\"cwd\":\"$BARE\",\"prompt\":\"a prompt long enough to clear the short-prompt guard\"}" | \
  SYMBIOSIS_BRAIN_RECALL_ENABLED=true SYMBIOSIS_BRAIN_RULES_ENABLED=false \
  SYMBIOSIS_BRAIN_TOOLS="$WORK/fake-tools" SYMBIOSIS_BRAIN_VAULT="$VAULT" \
  SB_TEST_ARGS_FILE="$ARGS_FILE" \
  bash "$HOOKS/brain-save-trigger.sh" prompt-check 2>/dev/null )

if [ -s "$ARGS_FILE" ] && ! grep -q -- "--scope" "$ARGS_FILE"; then
  t "unresolved session searches every scope (no --scope flag)" PASS
else
  t "unresolved session searches every scope (no --scope flag)" FAIL "$(tr '\n' ' ' < "$ARGS_FILE")"
fi

case "$BARE_OUT" in
  *"scope=all*"*) t "unresolved session is labelled in the header" PASS ;;
  *) t "unresolved session is labelled in the header" FAIL "$(echo "$BARE_OUT" | grep -o 'scope=[^]]*')" ;;
esac

echo ""
echo "Results: $pass passed, $fail failed"
[ "$fail" -eq 0 ] || exit 1
