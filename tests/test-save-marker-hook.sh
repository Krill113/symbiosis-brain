#!/bin/bash
# Tests for hooks/brain-save-marker.sh — the PostToolUse hook that records the
# context % at the moment memory was written, so the Stop-hook delta-guard measures
# growth SINCE the last save instead of from zero.
#
# Contract under test (00-plan §7.3): session_id comes from the hook payload, a
# failed tool call is not a save, a missing context-pct file is silence, and the
# hook is mute (no stdout/stderr) and always exits 0.
set -u

HOOKS="$(cd "$(dirname "$0")/.." && pwd)/hooks"
MARKER_HOOK="$HOOKS/brain-save-marker.sh"

WORK="$(mktemp -d)"
export TMPDIR="$WORK" TEMP="$WORK"
SID="save-marker-$$"
PCT="$WORK/brain-context-pct-${SID}"
LAST="$WORK/brain-last-save-pct-${SID}"

FAILED=0
t() {
  if [ "$2" = "PASS" ]; then echo "PASS: $1"; else echo "FAIL: $1"; FAILED=$((FAILED + 1)); fi
}

run_marker() {  # stdin payload -> rc in $RC, output in $OUT
  OUT=$(printf '%s' "$1" | bash "$MARKER_HOOK" 2>&1)
  RC=$?
}

# (1) context-pct present -> marker written with the same value, hook is mute
rm -f "$PCT" "$LAST"
echo "37" > "$PCT"
run_marker "{\"session_id\":\"${SID}\",\"tool_name\":\"mcp__symbiosis-brain__brain_write\",\"tool_response\":{\"ok\":true}}"
if [ "$RC" = "0" ] && [ -z "$OUT" ] && [ "$(cat "$LAST" 2>/dev/null)" = "37" ]; then
  t "writes marker from context-pct" PASS
else
  t "writes marker from context-pct (rc=$RC out='$OUT' marker='$(cat "$LAST" 2>/dev/null)')" FAIL
fi

# (2) whitespace after the colon in the payload is parsed too (the deployed hooks
#     used to disagree: session-start matched "session_id":"x", save-trigger "x": "y")
rm -f "$PCT" "$LAST"
echo "41" > "$PCT"
run_marker "{\"session_id\": \"${SID}\", \"tool_name\": \"mcp__symbiosis-brain__brain_patch\"}"
if [ "$(cat "$LAST" 2>/dev/null)" = "41" ]; then
  t "parses session_id with a space after the colon" PASS
else
  t "parses session_id with a space after the colon" FAIL
fi

# (3) no context-pct file -> rc 0 and nothing created
rm -f "$PCT" "$LAST"
run_marker "{\"session_id\":\"${SID}\",\"tool_name\":\"mcp__symbiosis-brain__brain_write\"}"
if [ "$RC" = "0" ] && [ ! -f "$LAST" ]; then
  t "no context-pct -> silent no-op" PASS
else
  t "no context-pct -> silent no-op" FAIL
fi

# (4) failed tool call -> not a save
rm -f "$PCT" "$LAST"
echo "50" > "$PCT"
run_marker "{\"session_id\":\"${SID}\",\"tool_name\":\"mcp__symbiosis-brain__brain_write\",\"tool_response\":{\"is_error\":true}}"
if [ "$RC" = "0" ] && [ ! -f "$LAST" ]; then
  t "is_error response does not write a marker" PASS
else
  t "is_error response does not write a marker" FAIL
fi

# (4b) `"is_error":true` inside the NOTE TEXT is not a failed call. This batch itself
# ships two notes containing that literal (hooks/README.md, CHANGELOG), and matching the
# whole payload would have made saving them silently skip the marker.
rm -f "$PCT" "$LAST"
echo "50" > "$PCT"
run_marker "{\"session_id\":\"${SID}\",\"tool_name\":\"mcp__symbiosis-brain__brain_write\",\"tool_input\":{\"content\":\"the hook skips a response with \\\"is_error\\\":true in it\"},\"tool_response\":{\"ok\":true}}"
if [ "$(cat "$LAST" 2>/dev/null)" = "50" ]; then
  t "is_error inside the note body still writes the marker" PASS
else
  t "is_error inside the note body still writes the marker" FAIL
fi

# (5) no session_id -> rc 0, nothing created anywhere
rm -f "$PCT" "$LAST"
echo "50" > "$PCT"
run_marker '{"tool_name":"mcp__symbiosis-brain__brain_write"}'
if [ "$RC" = "0" ] && [ -z "$OUT" ] && [ ! -f "$LAST" ]; then
  t "missing session_id -> silent exit 0" PASS
else
  t "missing session_id -> silent exit 0" FAIL
fi

# (6) an existing marker is overwritten, not appended to
rm -f "$PCT"
echo "12" > "$LAST"
echo "44" > "$PCT"
run_marker "{\"session_id\":\"${SID}\",\"tool_name\":\"mcp__symbiosis-brain__brain_append\"}"
if [ "$(cat "$LAST" 2>/dev/null)" = "44" ]; then
  t "marker is overwritten atomically" PASS
else
  t "marker is overwritten atomically" FAIL
fi
if [ ! -f "$LAST.tmp" ]; then t "no .tmp left behind" PASS; else t "no .tmp left behind" FAIL; fi

# (7) a REFUSED write is not a save either. The server never raises: all sixteen
# refusals in server.py come back as a plain, non-error TextContent whose text starts
# with `Error: `. Before this arm a rejected brain_write stamped the marker and reset
# the Stop-hook delta-guard, so the 25/35 zones went quiet for another 10% of context.
rm -f "$PCT" "$LAST"
echo "50" > "$PCT"
run_marker "{\"session_id\":\"${SID}\",\"tool_name\":\"mcp__symbiosis-brain__brain_write\",\"tool_response\":{\"content\":[{\"type\":\"text\",\"text\":\"Error: gist required for this note type\"}]}}"
if [ "$RC" = "0" ] && [ ! -f "$LAST" ]; then
  t "server refusal (Error: ...) does not write a marker" PASS
else
  t "server refusal (Error: ...) does not write a marker" FAIL
fi

# (7b) the same with a space after the colon, the way a pretty-printed payload reads.
rm -f "$PCT" "$LAST"
echo "50" > "$PCT"
run_marker "{\"session_id\":\"${SID}\",\"tool_name\":\"mcp__symbiosis-brain__brain_write\",\"tool_response\":{\"content\":[{\"type\": \"text\", \"text\": \"Error: path must be within vault\"}]}}"
if [ "$RC" = "0" ] && [ ! -f "$LAST" ]; then
  t "server refusal with spaced JSON does not write a marker" PASS
else
  t "server refusal with spaced JSON does not write a marker" FAIL
fi

# (8) parser contract: the cut lands on the LAST `"tool_response"` in the payload,
# never the first (`##`, not `#`). Inside a JSON *string* the key can only appear
# escaped, so 4b above passes either way; an unescaped one needs a nested object, and
# then `#` would hand the case below the tool_input — with a `"is_error":true` in it
# a perfectly successful write would be dropped. Pinning the contract keeps the
# comment above the expansion honest.
rm -f "$PCT" "$LAST"
echo "50" > "$PCT"
run_marker "{\"session_id\":\"${SID}\",\"tool_name\":\"mcp__symbiosis-brain__brain_write\",\"tool_input\":{\"payload\":{\"tool_response\":{\"is_error\":true}}},\"tool_response\":{\"ok\":true}}"
if [ "$(cat "$LAST" 2>/dev/null)" = "50" ]; then
  t "cut uses the last tool_response key, not the first" PASS
else
  t "cut uses the last tool_response key, not the first" FAIL
fi

rm -rf "$WORK"

echo ""
if [ "$FAILED" -eq 0 ]; then
  echo "All tests PASSED"
  exit 0
else
  echo "$FAILED test(s) FAILED"
  exit 1
fi
