#!/bin/bash
# Bash fixture for PreToolUse hook (B1). Mirrors tests/test-prompt-check-hook.sh pattern.
set -u

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
HOOK="$REPO_ROOT/hooks/brain-pre-action-trigger.sh"

if [ ! -x "$HOOK" ]; then
  echo "FAIL: hook not executable at $HOOK"
  exit 1
fi

# Vault for testing — repo-relative test fixture vault would be set up by integration,
# but here we just smoke that hook responds. Real recall asserted in pytest CLI tests.
TMP_VAULT="$(mktemp -d)"
mkdir -p "$TMP_VAULT/feedback"
cat > "$TMP_VAULT/feedback/commit-style.md" <<'EOF'
---
name: commit-style
type: feedback
scope: global
gist: Не добавляй Co-Authored-By Claude в коммиты
---

# Commit style
EOF

export SYMBIOSIS_BRAIN_TOOLS="$REPO_ROOT"

# Convert to Windows-native path so Python subprocess (uv run) resolves /tmp correctly
# on Windows/git-bash (POSIX /tmp/... is not visible to Windows Python as-is).
WIN_VAULT=$(cygpath -w "$TMP_VAULT" 2>/dev/null || echo "$TMP_VAULT")
# Guard: cygpath could return empty string with exit 0 in some edge cases —
# fall back to POSIX path so SYMBIOSIS_BRAIN_VAULT is never empty.
[ -z "$WIN_VAULT" ] && WIN_VAULT="$TMP_VAULT"
export SYMBIOSIS_BRAIN_VAULT="$WIN_VAULT"

# Pre-warm: sync vault + build vector index (mirrors prewarmed production state)
uv run --quiet --directory "$REPO_ROOT" python -c "
from pathlib import Path
from symbiosis_brain.storage import Storage
from symbiosis_brain.search import SearchEngine
from symbiosis_brain.sync import VaultSync
vault = Path(r'$WIN_VAULT')
db = vault / '.index' / 'brain.db'
storage = Storage(db)
VaultSync(vault, storage).sync_all()
SearchEngine(storage).index_all()
" 2>/dev/null || true

PASS=0
FAIL=0

# ── Test 1: Bash whitelist (git commit) — expect additionalContext ──
INPUT='{"tool_name":"Bash","tool_input":{"command":"git commit -m \"feat: x\""},"session_id":"test-1"}'
OUT=$(echo "$INPUT" | bash "$HOOK")
if echo "$OUT" | grep -q '"hookSpecificOutput"' && echo "$OUT" | grep -q '"additionalContext"'; then
  echo "PASS: git commit → additionalContext emitted"
  PASS=$((PASS+1))
else
  echo "FAIL: git commit → no additionalContext (got: $OUT)"
  FAIL=$((FAIL+1))
fi

# ── Test 2: Bash non-whitelist (ls) — expect empty ──
INPUT='{"tool_name":"Bash","tool_input":{"command":"ls -la"},"session_id":"test-2"}'
OUT=$(echo "$INPUT" | bash "$HOOK")
if [ -z "$OUT" ]; then
  echo "PASS: ls → silent skip"
  PASS=$((PASS+1))
else
  echo "FAIL: ls → got output (expected empty): $OUT"
  FAIL=$((FAIL+1))
fi

# ── Test 3: Unknown tool (Read) — expect empty ──
INPUT='{"tool_name":"Read","tool_input":{"file_path":"/x"},"session_id":"test-3"}'
OUT=$(echo "$INPUT" | bash "$HOOK")
if [ -z "$OUT" ]; then
  echo "PASS: Read → silent skip"
  PASS=$((PASS+1))
else
  echo "FAIL: Read → got output (expected empty): $OUT"
  FAIL=$((FAIL+1))
fi

# ── Test 4: Kill-switch env var — expect empty regardless ──
INPUT='{"tool_name":"Bash","tool_input":{"command":"git commit -m x"},"session_id":"test-4"}'
OUT=$(echo "$INPUT" | SYMBIOSIS_BRAIN_PRE_ACTION_DISABLED=1 bash "$HOOK")
if [ -z "$OUT" ]; then
  echo "PASS: kill-switch → silent skip"
  PASS=$((PASS+1))
else
  echo "FAIL: kill-switch → got output: $OUT"
  FAIL=$((FAIL+1))
fi

# ── Test 5: Malformed stdin — expect empty, exit 0 ──
OUT=$(echo "{not valid json" | bash "$HOOK")
RC=$?
if [ "$RC" -eq 0 ] && [ -z "$OUT" ]; then
  echo "PASS: malformed stdin → exit 0, empty"
  PASS=$((PASS+1))
else
  echo "FAIL: malformed stdin → rc=$RC out=$OUT"
  FAIL=$((FAIL+1))
fi

# Routed mcp tool → one JSONL tool_used line carrying the monotonic turn.
SID='test-route-1'
EVT="/tmp/brain-route-events-${SID}.jsonl"
rm -f "$EVT"
echo '7' > "/tmp/brain-route-turn-${SID}"
INPUT='{"tool_name":"mcp__serena__find_referencing_symbols","tool_input":{},"session_id":"'$SID'"}'
echo "$INPUT" | bash "$HOOK" >/dev/null 2>&1
if [ -f "$EVT" ] && grep -q '"event":"tool_used"' "$EVT" && grep -q '"monotonic_turn":7' "$EVT" && grep -q 'find_referencing_symbols' "$EVT"; then echo 'PASS: routed tool appends tool_used w/ turn'; PASS=$((PASS+1)); else echo "FAIL: routed append (got: $(cat "$EVT" 2>/dev/null))"; FAIL=$((FAIL+1)); fi
LINES=$(wc -l < "$EVT"); if [ "$LINES" -eq 1 ]; then echo 'PASS: exactly one JSONL line'; PASS=$((PASS+1)); else echo "FAIL: expected 1 line got $LINES"; FAIL=$((FAIL+1)); fi
INPUT='{"tool_name":"Read","tool_input":{},"session_id":"'$SID'"}'
echo "$INPUT" | bash "$HOOK" >/dev/null 2>&1
if [ "$(wc -l < "$EVT")" -eq 1 ]; then echo 'PASS: non-routed tool appends nothing'; PASS=$((PASS+1)); else echo 'FAIL: non-routed tool wrote a line'; FAIL=$((FAIL+1)); fi
rm -f "/tmp/brain-route-turn-${SID}"
INPUT='{"tool_name":"WebSearch","tool_input":{},"session_id":"'$SID'"}'
echo "$INPUT" | bash "$HOOK" >/dev/null 2>&1; RC=$?
if [ "$RC" -eq 0 ] && grep -q '"monotonic_turn":0' "$EVT"; then echo 'PASS: missing counter → fail-open turn 0'; PASS=$((PASS+1)); else echo 'FAIL: missing counter not fail-open'; FAIL=$((FAIL+1)); fi
rm -f "$EVT"

# Cleanup
rm -rf "$TMP_VAULT"

echo
echo "Total: $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
