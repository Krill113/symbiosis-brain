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

# One session-id parser for every hook (hooks/sb-hooklib.sh): the inline greps used
# to disagree about whitespace after the colon, so half of the payloads the harness
# sends parsed as an empty session id. Fail-open: without the library the hook goes
# silent instead of guessing.
DIR=${BASH_SOURCE[0]}
# Both separators: bash invoked with a Windows-style path leaves no '/' to cut on,
# and the library then silently fails to load.
case $DIR in *[/\\]*) DIR=${DIR%[/\\]*} ;; *) DIR=. ;; esac
. "$DIR/sb-hooklib.sh" 2>/dev/null || exit 0

INPUT=$(cat)
sb_session_id "$INPUT"
SESSION_ID="$SB_SESSION_ID"
sb_tmp_dir

VAULT="${SYMBIOSIS_BRAIN_VAULT:-$HOME/symbiosis-brain-vault}"
TOOLS="${SYMBIOSIS_BRAIN_TOOLS}"
# L1: the CLAUDE.md marker, walked up from $PWD (sb_scope_from_dir). The marker is the
# only authoritative source of a scope; a CLAUDE.md without one stops the walk instead
# of letting a project inherit an unrelated ancestor's scope.
sb_scope_from_dir "$PWD"
SCOPE="$SB_SCOPE"
if [ -n "$SCOPE" ] && [ "${SB_SCOPE_DEPTH:-0}" -gt 0 ] && sb_scope_is_broad "$SCOPE"; then
  SCOPE=""   # an umbrella or workspace-root scope is never inherited downwards
fi

# L2: the basename guess, accepted only when the vault taxonomy knows that scope.
# An invented scope is not a milder failure than none: as a search filter it silently
# degenerates to global-only and hides every project note.
if [ -z "$SCOPE" ]; then
  CAND=$(normalize_scope "$(basename "$PWD")")
  if [ -n "$CAND" ] && sb_scope_registered "$CAND"; then SCOPE="$CAND"; fi
fi

# Set env vars for other hooks, brain-init skill, and bash commands in this session
if [ -n "$CLAUDE_ENV_FILE" ]; then
  echo "export SYMBIOSIS_BRAIN_VAULT=\"$VAULT\"" >> "$CLAUDE_ENV_FILE"
  echo "export SYMBIOSIS_BRAIN_TOOLS=\"$TOOLS\"" >> "$CLAUDE_ENV_FILE"
  echo "export SYMBIOSIS_BRAIN_SCOPE=\"$SCOPE\"" >> "$CLAUDE_ENV_FILE"
  [ -n "$SESSION_ID" ] && echo "export CLAUDE_SESSION_ID=\"$SESSION_ID\"" >> "$CLAUDE_ENV_FILE"
fi

# Bridge for the other hooks. They cannot read CLAUDE_ENV_FILE — Claude Code sources it
# before Bash TOOL commands, not before hook or status-line processes — so the resolved
# scope is published as a per-session file, like every other cross-hook value here.
# Written even when empty: an empty file means "resolved to nothing", which a reader
# must not confuse with "not written yet".
if [ -n "$SESSION_ID" ]; then
  printf '%s\n' "$SCOPE" > "$SB_TMP/brain-scope-${SESSION_ID}" 2>/dev/null || true
fi

# L0: inject critical facts
if [ -f "$VAULT/CRITICAL_FACTS.md" ]; then
  echo "=== Symbiosis Brain ==="
  cat "$VAULT/CRITICAL_FACTS.md"
  echo ""
fi

# Vault sync alarm. brain-sync.sh runs at SessionEnd, which has no output channel:
# a failed push/rebase would otherwise stay invisible until the vault diverged for
# good. The marker is removed by the next successful sync.
if [ -r "$SB_TMP/brain-sync-failed" ]; then
  read -r SYNC_LINE < "$SB_TMP/brain-sync-failed"
  SYNC_STAGE=${SYNC_LINE#stage=}; SYNC_STAGE=${SYNC_STAGE%% *}
  SYNC_AT=${SYNC_LINE##*at=}
  echo "⚠️ vault sync failed (${SYNC_STAGE}, ${SYNC_AT}) — see $SB_TMP/brain-sync-errors.log"
  echo ""
fi

# Tool roster (one-line cheat sheet, low cost, refreshes on session start)
echo "Available tools: brain_search/brain_read/brain_write (memory), Serena (find_symbol/replace_symbol_body), subagents (Explore/general-purpose), screenshot."
echo ""

if [ -n "$SCOPE" ]; then
  echo "[scope: $SCOPE]"
else
  echo "[scope: unresolved -> all scopes]"
fi

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
# Skip the prime when a fresh roster for THIS session already exists. A1a widens
# SessionStart from 2 matchers to 4, so `claude mcp list` would now detach on every
# resume/clear as well — and it health-checks every server, which starts a SECOND
# `symbiosis-brain serve` against the live vault. That start is not read-only
# (server.py:114-152 runs index_all()/repair_index() on drift), and this build of
# SQLite (3.50.4) still carries the WAL-Reset race we deliberately parked.
# 60 min matches the GC threshold for the same file below, so "stale -> swept ->
# re-primed" is one cycle, not two competing numbers.
_roster="$SB_TMP/brain-mcp-roster-${SESSION_ID}"
if [ -n "$SESSION_ID" ] && command -v claude >/dev/null 2>&1 && \
   { [ ! -f "$_roster" ] || [ -n "$(find "$_roster" -mmin +60 2>/dev/null)" ]; }; then
  (
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
        "$SB_TMP/brain-save-later-${SESSION_ID}" \
        "$SB_TMP/brain-rules-shown-${SESSION_ID}" \
        "$SB_TMP/brain-rules-turn-counter-${SESSION_ID}" \
        "$SB_TMP/brain-context-pct-${SESSION_ID}"
  # NOTE: brain-route-turn-${SESSION_ID} is DELIBERATELY excluded here —
  # the monotonic routing counter must survive compact (SessionStart runs
  # on compact). See stage4 design §6.2. Orphan-GC reaps it by mtime only.
  # NOTE: brain-last-save-pct-${SESSION_ID} is DELIBERATELY excluded too —
  # SessionStart also runs on compact, and wiping the marker there reset the
  # Stop-hook delta-guard to zero, so the next threshold fired unconditionally
  # after every compaction. On a fresh startup the session id is new and there
  # is nothing to wipe anyway.
  echo "$SESSION_ID" > "$SB_TMP/brain-current-session"
fi

# Opportunistic GC of orphaned recall dedup files from dead/idle sessions
if command -v find >/dev/null 2>&1; then
  find "$SB_TMP" -maxdepth 1 -name 'brain-recall-seen-*.json' -mmin +60 -delete 2>/dev/null || true
  find "$SB_TMP" -maxdepth 1 -name 'brain-prompt-recall-seen-*.json' -mmin +60 -delete 2>/dev/null || true
  find "$SB_TMP" -maxdepth 1 -name 'brain-route-events-*.jsonl' -mmin +60 -delete 2>/dev/null || true
  find "$SB_TMP" -maxdepth 1 -name 'brain-route-seen-*.json' -mmin +60 -delete 2>/dev/null || true
  find "$SB_TMP" -maxdepth 1 -name 'brain-route-turn-*' -mmin +60 -delete 2>/dev/null || true
  find "$SB_TMP" -maxdepth 1 -name 'brain-mcp-roster-*' -mmin +60 -delete 2>/dev/null || true
  find "$SB_TMP" -maxdepth 1 -name 'brain-model-*' -mmin +60 -delete 2>/dev/null || true
  # 24h, not 60min like the counters above: this one is written once per session and
  # must outlive a long session, not be refreshed by every turn.
  find "$SB_TMP" -maxdepth 1 -name 'brain-scope-*' -mmin +1440 -delete 2>/dev/null || true
fi

exit 0
