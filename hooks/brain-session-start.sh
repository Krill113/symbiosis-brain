#!/bin/bash
# Symbiosis Brain — SessionStart hook (post-C1 architecture).
# Layer 1: dumb basename → kebab-case scope. Skill brain-init handles
# marker-based override at Layer 2.

# Allow `source <hook> --source-only-normalize` to expose normalize_scope
# to tests без выполнения тела хука.
normalize_scope() {
  local raw="$1"
  if [ -z "$raw" ]; then echo ""; return; fi
  # Step 1: insert dashes between camelCase boundaries (FooBar → Foo-Bar, ABCService → ABC-Service).
  # Two-pass via sed: lower-then-upper, then upper-followed-by-upper-lower.
  local s
  s=$(printf '%s' "$raw" | sed -E 's/([a-z0-9])([A-Z])/\1-\2/g; s/([A-Z])([A-Z][a-z])/\1-\2/g')
  # Step 2: lowercase
  s=$(printf '%s' "$s" | tr '[:upper:]' '[:lower:]')
  # Step 3: separators → dash
  s=$(printf '%s' "$s" | tr '._ \t' '----')
  # Step 4: drop non-alphanumeric-dash
  s=$(printf '%s' "$s" | sed -E 's/[^a-z0-9-]//g')
  # Step 5: collapse multi-dashes, strip edges
  s=$(printf '%s' "$s" | sed -E 's/-+/-/g; s/^-//; s/-$//')
  printf '%s' "$s"
}

# When sourced for tests, only define functions and exit.
if [ "$1" = "--source-only-normalize" ]; then return 0 2>/dev/null || exit 0; fi

INPUT=$(cat)
SESSION_ID=$(echo "$INPUT" | grep -o '"session_id":"[^"]*"' | head -1 | sed 's/.*":"//;s/"$//')

VAULT="${SYMBIOSIS_BRAIN_VAULT:-$HOME/symbiosis-brain-vault}"
TOOLS="${SYMBIOSIS_BRAIN_TOOLS}"
SCOPE=$(normalize_scope "$(basename "$PWD")")
[ -z "$SCOPE" ] && SCOPE="global"

# Set env vars for other hooks, brain-init skill, and bash commands in this session
if [ -n "$CLAUDE_ENV_FILE" ]; then
  echo "export SYMBIOSIS_BRAIN_VAULT=\"$VAULT\"" >> "$CLAUDE_ENV_FILE"
  echo "export SYMBIOSIS_BRAIN_TOOLS=\"$TOOLS\"" >> "$CLAUDE_ENV_FILE"
  echo "export SYMBIOSIS_BRAIN_SCOPE=\"$SCOPE\"" >> "$CLAUDE_ENV_FILE"
  [ -n "$SESSION_ID" ] && echo "export CLAUDE_SESSION_ID=\"$SESSION_ID\"" >> "$CLAUDE_ENV_FILE"
fi

# L0: inject critical facts
if [ -f "$VAULT/CRITICAL_FACTS.md" ]; then
  echo "=== Symbiosis Brain ==="
  cat "$VAULT/CRITICAL_FACTS.md"
  echo ""
fi

# Tool roster (one-line cheat sheet, low cost, refreshes on session start)
echo "Available tools: brain_search/brain_read/brain_write (memory), Serena (find_symbol/replace_symbol_body), subagents (Explore/general-purpose), screenshot."
echo ""

echo "[scope: $SCOPE]"

# Clean THIS session's trigger flags (threshold reset on compaction; no-op on fresh startup)
if [ -n "$SESSION_ID" ]; then
  rm -f "/tmp/brain-triggered-${SESSION_ID}" \
        "/tmp/brain-precompact-${SESSION_ID}" \
        "/tmp/brain-precompact-pending-${SESSION_ID}" \
        "/tmp/brain-last-save-pct-${SESSION_ID}" \
        "/tmp/brain-save-later-${SESSION_ID}" \
        "/tmp/brain-rules-shown-${SESSION_ID}" \
        "/tmp/brain-rules-turn-counter-${SESSION_ID}"
  echo "$SESSION_ID" > /tmp/brain-current-session
fi

exit 0
