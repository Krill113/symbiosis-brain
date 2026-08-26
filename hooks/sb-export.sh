#!/bin/bash
# Symbiosis Brain — the single export point for the status-line bridges.
#
# Sourced by sb-statusline.sh BEFORE the first-line branch, so both bridges are
# written no matter whose line 1 wins: a user who set
# SYMBIOSIS_BRAIN_USER_STATUSLINE_CMD used to silently lose every save reminder,
# because the exports lived inside sb-base-statusline.sh.
# Also sourced by sb-base-statusline.sh when it runs on its own (SB_EXPORT_DONE unset).
#
# Input: $SB_STATUSLINE_INPUT — the status-line JSON, pre-read by the caller.
# Writes:
#   1. $SB_TMP/brain-context-pct-<sid>  — context %, read by brain-save-trigger.sh
#      and brain-save-marker.sh.
#   2. ${SYMBIOSIS_BRAIN_RATE_LIMITS_FILE:-$SB_TMP/claude-rate-limits.json} — one-line
#      JSON snapshot of the rate limits, readable by any watcher agent. Format is
#      documented in hooks/README.md. Opt out with SYMBIOSIS_BRAIN_RATE_LIMITS_DISABLED=1.
#
# Fork-free: bash builtins only. See sb-statusline.sh for why every fork here costs
# both latency and a zombie-leak window.

if [ -z "${SB_STATUSLINE_INPUT+set}" ]; then
  return 0 2>/dev/null || exit 0
fi
sb_data=$SB_STATUSLINE_INPUT

if declare -F sb_tmp_dir >/dev/null 2>&1; then
  sb_tmp_dir
else
  SB_TMP="${TMPDIR:-${TEMP:-/tmp}}"
fi
if declare -F sb_session_id >/dev/null 2>&1; then
  sb_session_id "$sb_data"
else
  SB_SESSION_ID=
fi

# Unix seconds, resolved once. $EPOCHSECONDS is a bash 5 builtin and costs nothing;
# older bash — notably macOS's stock /bin/bash 3.2 — pays for a single `date`.
if [ -z "${SB_NOW:-}" ]; then
  if [ -n "${EPOCHSECONDS:-}" ]; then SB_NOW=$EPOCHSECONDS; else SB_NOW=$(date +%s); fi
fi

# 1. Context % per session (never machine-global: two windows would bleed into each other)
sb_ctx=
[[ $sb_data =~ \"used_percentage\"[[:space:]]*:[[:space:]]*([0-9]+) ]] && sb_ctx=${BASH_REMATCH[1]}
if [ -n "$sb_ctx" ] && [ -n "$SB_SESSION_ID" ]; then
  printf '%s\n' "$sb_ctx" > "$SB_TMP/brain-context-pct-${SB_SESSION_ID}" 2>/dev/null
fi

# 2. Rate limits
sb_rate5h= sb_reset5h= sb_rate7d=
if [[ $sb_data =~ \"five_hour\"[[:space:]]*:[[:space:]]*\{([^}]*)\} ]]; then
  sb_fh=${BASH_REMATCH[1]}
  [[ $sb_fh =~ \"used_percentage\"[[:space:]]*:[[:space:]]*([0-9]+) ]] && sb_rate5h=${BASH_REMATCH[1]}
  [[ $sb_fh =~ \"resets_at\"[[:space:]]*:[[:space:]]*([0-9]+) ]] && sb_reset5h=${BASH_REMATCH[1]}
fi
if [[ $sb_data =~ \"seven_day\"[[:space:]]*:[[:space:]]*\{([^}]*)\} ]]; then
  sb_sd=${BASH_REMATCH[1]}
  [[ $sb_sd =~ \"used_percentage\"[[:space:]]*:[[:space:]]*([0-9]+) ]] && sb_rate7d=${BASH_REMATCH[1]}
fi
if [ -z "${SYMBIOSIS_BRAIN_RATE_LIMITS_DISABLED:-}" ] && [ -n "$sb_rate5h" ]; then
  printf '{"five_hour_pct":%s,"resets_at":%s,"seven_day_pct":%s,"ts":%s}\n' \
    "$sb_rate5h" "${sb_reset5h:-0}" "${sb_rate7d:-0}" "$SB_NOW" \
    > "${SYMBIOSIS_BRAIN_RATE_LIMITS_FILE:-$SB_TMP/claude-rate-limits.json}" 2>/dev/null
fi

export SB_EXPORT_DONE=1
