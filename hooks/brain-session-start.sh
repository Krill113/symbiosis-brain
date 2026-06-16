#!/bin/bash
# Symbiosis Brain — SessionStart hook (post-C1 architecture).
# Scope resolution: basename → kebab-case (Layer 1), then CLAUDE.md marker
# override (Layer 2) so SYMBIOSIS_BRAIN_SCOPE is correct for the recall/rules/
# save hooks that read it. Skill brain-init still re-resolves for richer fields
# (umbrella/source/version); this hook only needs the canonical scope.

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
SB_TMP="${TMPDIR:-${TEMP:-/tmp}}"

VAULT="${SYMBIOSIS_BRAIN_VAULT:-$HOME/symbiosis-brain-vault}"
TOOLS="${SYMBIOSIS_BRAIN_TOOLS}"
SCOPE=$(normalize_scope "$(basename "$PWD")")
[ -z "$SCOPE" ] && SCOPE="global"

# L2: marker override. The basename heuristic above is wrong whenever the
# folder name doesn't kebab-match the vault scope (e.g. LWhisperer → l-whisperer
# but vault scope is "lwhisper"). The skill brain-init resolves this into the
# model's context, but the recall/rules/save hooks read SYMBIOSIS_BRAIN_SCOPE,
# so the marker must win HERE too. Pure-bash (no uv) to stay within the 5s
# timeout. Mirrors scope_resolver.parse_marker: last marker wins, scope= required.
if [ -f "$PWD/CLAUDE.md" ]; then
  MARKER_SCOPE=$(grep -oE '<!--[[:space:]]*symbiosis-brain[[:space:]]+v[0-9]+[[:space:]]*:.*-->' "$PWD/CLAUDE.md" 2>/dev/null \
    | tail -1 \
    | sed -nE 's/.*[[:space:],:]scope[[:space:]]*=[[:space:]]*([A-Za-z0-9_-]+).*/\1/p')
  [ -n "$MARKER_SCOPE" ] && SCOPE="$MARKER_SCOPE"
fi

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

# Background pre-warm: fastembed + sqlite-vec page-cache priming.
# Fire-and-forget — must not block session start (hook timeout is 5s).
# Output suppressed so it can't pollute L0 context. nohup intentionally
# omitted: subshell + & + DEVNULL redirect already detaches under Claude
# Code (no controlling TTY), and nohup is missing on some git-bash envs.
if [ -n "$TOOLS" ] && [ -n "$VAULT" ] && command -v uv >/dev/null 2>&1; then
  ( uv run --quiet --directory "$TOOLS" \
      python -m symbiosis_brain prewarm --vault "$VAULT" \
      >/dev/null 2>&1 & ) >/dev/null 2>&1
fi

# Background roster prime: cache `claude mcp list` for UPS routing MCP-presence
# gates (brain-mcp-roster-<sid>). claude mcp list runs healthchecks (~7s) which
# blows the 5s SessionStart timeout, so it MUST be detached like the prewarm above.
# Atomic publish (write tmp + mv) so a concurrent UPS reader never sees a partial
# file. Fail-open: no cache → MCP `*-present` gates read 'undeterminable' (silent).
if [ -n "$SESSION_ID" ] && command -v claude >/dev/null 2>&1; then
  (
    _roster="$SB_TMP/brain-mcp-roster-${SESSION_ID}"
    if claude mcp list >"$_roster.tmp" 2>/dev/null; then
      mv -f "$_roster.tmp" "$_roster" 2>/dev/null || rm -f "$_roster.tmp" 2>/dev/null
    else
      rm -f "$_roster.tmp" 2>/dev/null
    fi
  ) >/dev/null 2>&1 &
fi

# Clean THIS session's trigger flags (threshold reset on compaction; no-op on fresh startup)
if [ -n "$SESSION_ID" ]; then
  rm -f "$SB_TMP/brain-triggered-${SESSION_ID}" \
        "$SB_TMP/brain-precompact-${SESSION_ID}" \
        "$SB_TMP/brain-precompact-pending-${SESSION_ID}" \
        "$SB_TMP/brain-last-save-pct-${SESSION_ID}" \
        "$SB_TMP/brain-save-later-${SESSION_ID}" \
        "$SB_TMP/brain-rules-shown-${SESSION_ID}" \
        "$SB_TMP/brain-rules-turn-counter-${SESSION_ID}" \
        "$SB_TMP/brain-context-pct-${SESSION_ID}"
  # NOTE: brain-route-turn-${SESSION_ID} is DELIBERATELY excluded here —
  # the monotonic routing counter must survive compact (SessionStart runs
  # on compact). See stage4 design §6.2. Orphan-GC reaps it by mtime only.
  echo "$SESSION_ID" > "$SB_TMP/brain-current-session"
fi

# Opportunistic GC of orphaned recall dedup files from dead/idle sessions
if command -v find >/dev/null 2>&1; then
  find "$SB_TMP" -maxdepth 1 -name 'brain-recall-seen-*.json' -mmin +60 -delete 2>/dev/null || true
  find "$SB_TMP" -maxdepth 1 -name 'brain-route-events-*.jsonl' -mmin +60 -delete 2>/dev/null || true
  find "$SB_TMP" -maxdepth 1 -name 'brain-route-seen-*.json' -mmin +60 -delete 2>/dev/null || true
  find "$SB_TMP" -maxdepth 1 -name 'brain-route-turn-*' -mmin +60 -delete 2>/dev/null || true
  find "$SB_TMP" -maxdepth 1 -name 'brain-mcp-roster-*' -mmin +60 -delete 2>/dev/null || true
fi

exit 0
