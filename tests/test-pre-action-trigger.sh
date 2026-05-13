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

# Cleanup
rm -rf "$TMP_VAULT"

echo
echo "Total: $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
