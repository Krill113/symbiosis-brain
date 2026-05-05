#!/bin/bash
# Symbiosis Brain — second statusline line. Reads stdin (JSON with session_id),
# but only uses SYMBIOSIS_BRAIN_SCOPE env-var + /tmp markers + config env-vars to compose output.
INPUT=$(cat)

SESSION_ID=$(echo "$INPUT" | grep -o '"session_id":"[^"]*"' | head -1 | sed 's/.*":"//;s/"$//')

SCOPE="${SYMBIOSIS_BRAIN_SCOPE:-global}"
SAVE_THR=$(echo "${SYMBIOSIS_BRAIN_SAVE_THRESHOLDS:-40,70,90}" | tr ',' '/')
RULES_THR=$(echo "${SYMBIOSIS_BRAIN_RULES_ZONES:-30,60,85}" | tr ',' '/')
RULES_R="${SYMBIOSIS_BRAIN_RULES_TURN_INTERVAL:-10}"

LAST_SAVE=0
if [ -n "$SESSION_ID" ]; then
  LAST_SAVE=$(cat "/tmp/brain-last-save-pct-${SESSION_ID}" 2>/dev/null || echo 0)
fi

echo "🧠 [Symbiosis-Brain]  scope: ${SCOPE}  auto-save: [${SAVE_THR}]  rules: [${RULES_THR}·R${RULES_R}]  last-save: ${LAST_SAVE}%"
