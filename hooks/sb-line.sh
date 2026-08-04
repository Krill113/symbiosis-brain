#!/bin/bash
# Symbiosis Brain — second statusline line. Reads stdin (JSON with session_id),
# but only uses SYMBIOSIS_BRAIN_SCOPE env-var + tmp markers + config env-vars to compose output.
#
# Fork-free: bash builtins only, no grep/sed/tr/cat. See sb-base-statusline.sh
# for why every fork here costs both latency and a zombie-leak window.
#
# Input: session JSON on stdin, or pre-read into $SB_STATUSLINE_INPUT when sourced.

if [ -n "${SB_STATUSLINE_INPUT+set}" ]; then
  INPUT=$SB_STATUSLINE_INPUT
else
  IFS= read -r -d '' INPUT
fi

SESSION_ID=
[[ $INPUT =~ \"session_id\"[[:space:]]*:[[:space:]]*\"([^\"]*)\" ]] && SESSION_ID=${BASH_REMATCH[1]}
SB_TMP="${TMPDIR:-${TEMP:-/tmp}}"

SCOPE="${SYMBIOSIS_BRAIN_SCOPE:-global}"
SAVE_THR="${SYMBIOSIS_BRAIN_SAVE_THRESHOLDS:-40,70,90}";  SAVE_THR=${SAVE_THR//,//}
RULES_THR="${SYMBIOSIS_BRAIN_RULES_ZONES:-30,60,85}";     RULES_THR=${RULES_THR//,//}
RULES_R="${SYMBIOSIS_BRAIN_RULES_TURN_INTERVAL:-10}"

LAST_SAVE=0
if [ -n "$SESSION_ID" ]; then
  sb_marker="$SB_TMP/brain-last-save-pct-${SESSION_ID}"
  if [ -r "$sb_marker" ]; then
    read -r LAST_SAVE < "$sb_marker"
    [ -n "$LAST_SAVE" ] || LAST_SAVE=0
  fi
fi

printf '🧠 [Symbiosis-Brain]  scope: %s  auto-save: [%s]  rules: [%s·R%s]  last-save: %s%%\n' \
  "$SCOPE" "$SAVE_THR" "$RULES_THR" "$RULES_R" "$LAST_SAVE"
