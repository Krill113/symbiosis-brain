#!/bin/bash
# brain-pre-action-trigger.sh — PreToolUse hook for proactive brain recall (B1).
# Design: decisions/2026-05-13-brain-recall-action-trigger-design.md (in vault).
#
# Known cosmetic UI bug: anthropics/claude-code#41868 — successful hook with
# additionalContext may display as "hook error" in Claude Code UI 2026. The
# additionalContext is still injected; the indicator is wrong. Don't chase it.
#
# Fail-open principle: any error → exit 0 + empty stdout. Never block tool.

# Kill-switch via env var (instant disable without editing settings.json)
if [ "$SYMBIOSIS_BRAIN_PRE_ACTION_DISABLED" = "1" ]; then
  exit 0
fi

INPUT=$(cat)
if [ -z "$INPUT" ]; then
  exit 0
fi

# ── C5 Tier-1: pure-bash best-effort tool_used appender (Stage-4) ──
# Records routed-tool invocations for opportunistic compliance signal.
# NOT uv-run (would pay cold-start on every tool call). Routed-set =
# seed expected_tools (WebSearch/WebFetch/PowerShell/Serena/Playwright/
# brain/civil3d). Fail-open: never blocks the tool.
PA_SB_TMP="${TMPDIR:-${TEMP:-/tmp}}"
PA_TOOL=$(printf '%s' "$INPUT" | grep -o '"tool_name":"[^"]*"' | head -1 | sed 's/.*":"//;s/"$//')
PA_SID=$(printf '%s' "$INPUT" | grep -o '"session_id":"[^"]*"' | head -1 | sed 's/.*":"//;s/"$//')
[ -z "$PA_SID" ] && PA_SID="default"
case "$PA_TOOL" in
  WebSearch|WebFetch|PowerShell|mcp__serena__*|mcp__playwright__*|mcp__symbiosis-brain__*|mcp__civil3d-bridge__*)
    PA_TURN=$(cat "$PA_SB_TMP/brain-route-turn-${PA_SID}" 2>/dev/null || echo 0)
    case "$PA_TURN" in ''|*[!0-9]*) PA_TURN=0 ;; esac
    PA_TS=$(date -Iseconds 2>/dev/null || date)
    printf '{"ts":"%s","monotonic_turn":%s,"event":"tool_used","tool":"%s","session_id":"%s"}\n' \
      "$PA_TS" "$PA_TURN" "$PA_TOOL" "$PA_SID" \
      >> "$PA_SB_TMP/brain-route-events-${PA_SID}.jsonl" 2>/dev/null || true
    ;;
esac

if [ -z "$SYMBIOSIS_BRAIN_TOOLS" ] || [ -z "$SYMBIOSIS_BRAIN_VAULT" ]; then
  exit 0
fi

if ! command -v uv >/dev/null 2>&1; then
  exit 0
fi

DEBUG_LOG="${TMPDIR:-${TEMP:-/tmp}}/brain-hook-debug.log"

OUTPUT=$(printf '%s' "$INPUT" | timeout 30 uv run --quiet --directory "$SYMBIOSIS_BRAIN_TOOLS" \
  python -m symbiosis_brain pre-action-recall \
  --vault "$SYMBIOSIS_BRAIN_VAULT" 2>>"$DEBUG_LOG")
EXIT=$?

if [ "$EXIT" -ne 0 ]; then
  printf '[%s] pre-action-recall EXIT=%s\n' \
    "$(date -Iseconds 2>/dev/null || date)" "$EXIT" >> "$DEBUG_LOG"
  exit 0
fi

if [ -n "$OUTPUT" ]; then
  echo "$OUTPUT"
fi
exit 0
