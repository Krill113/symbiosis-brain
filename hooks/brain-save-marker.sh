#!/bin/bash
# brain-save-marker.sh — PostToolUse hook for mcp__symbiosis-brain__brain_write |
# brain_append | brain_patch.
#
# Records the context % at the moment memory was written, so the Stop-hook
# delta-guard (brain-save-trigger.sh) measures growth SINCE the last save instead
# of from zero. Replaces Step 8 of the brain-save skill: session_id comes from this
# payload, which is correct in resumed / forked / multi-window sessions where
# CLAUDE_SESSION_ID is empty or points at another window.
#
# Contract: never writes to stdout/stderr, never blocks, always exit 0.

DIR=${BASH_SOURCE[0]%/*}
[ "$DIR" = "${BASH_SOURCE[0]}" ] && DIR=.
. "$DIR/sb-hooklib.sh" 2>/dev/null || exit 0

INPUT=$(cat)

sb_session_id "$INPUT"
[ -n "$SB_SESSION_ID" ] || exit 0

# A failed write is not a save. Match inside tool_response ONLY: the payload also
# carries tool_input, i.e. the note's own text, and a note that merely MENTIONS
# `"is_error":true` (this very checkpoint writes two — hooks/README.md and CHANGELOG)
# would otherwise be read as a failed call and silently skip the marker.
# The harness emits tool_response LAST, so the tail after that key is the response
# and nothing else. Fork-free by design (parameter expansion, no jq, no python).
SB_RESP=${INPUT#*\"tool_response\"}
[ "$SB_RESP" = "$INPUT" ] && SB_RESP=          # no tool_response key -> nothing to inspect
case "$SB_RESP" in
  *'"is_error":true'*|*'"is_error": true'*) exit 0 ;;
esac

sb_tmp_dir
PCT=
PCT_FILE="$SB_TMP/brain-context-pct-${SB_SESSION_ID}"
[ -r "$PCT_FILE" ] && read -r PCT < "$PCT_FILE"
[ -n "$PCT" ] || exit 0

MARKER="$SB_TMP/brain-last-save-pct-${SB_SESSION_ID}"
printf '%s\n' "$PCT" > "$MARKER.tmp" 2>/dev/null || exit 0
mv -f "$MARKER.tmp" "$MARKER" 2>/dev/null || rm -f "$MARKER.tmp" 2>/dev/null
exit 0
