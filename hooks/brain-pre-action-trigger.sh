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

if [ -z "$SYMBIOSIS_BRAIN_TOOLS" ] || [ -z "$SYMBIOSIS_BRAIN_VAULT" ]; then
  exit 0
fi

if ! command -v uv >/dev/null 2>&1; then
  exit 0
fi

INPUT=$(cat)
if [ -z "$INPUT" ]; then
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
