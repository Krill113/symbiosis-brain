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
#   3. $SB_TMP/brain-model-<CLAUDE_PID>  — the model this window runs, read by the
#      MCP server (provenance.model_from_bridge) to fill written_by. Format is
#      documented in hooks/README.md.
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

# 3. Current model per Claude Code window — the bridge that fills written_by.
# Keyed by $CLAUDE_PID: that is the PID of the claude.exe window, and the MCP server
# is a direct child of it, so os.getppid() gives the server the same number and it
# reads its OWN window's file without walking the process tree — which is impossible
# fork-free anyway (MSYS reports ppid=1 when the parent is a native Windows process).
# Cut the object first, then the two fields inside it, exactly like five_hour above.
sb_model_id= sb_model_name=
if [[ $sb_data =~ \"model\"[[:space:]]*:[[:space:]]*\{([^}]*)\} ]]; then
  sb_md=${BASH_REMATCH[1]}
  [[ $sb_md =~ \"id\"[[:space:]]*:[[:space:]]*\"([^\"]*)\" ]] && sb_model_id=${BASH_REMATCH[1]}
  [[ $sb_md =~ \"display_name\"[[:space:]]*:[[:space:]]*\"([^\"]*)\" ]] && sb_model_name=${BASH_REMATCH[1]}
fi
sb_model_key=
if [ -n "${CLAUDE_PID:-}" ]; then
  sb_model_key="brain-model-${CLAUDE_PID}"
elif [ -n "$SB_SESSION_ID" ]; then
  sb_model_key="brain-model-sid-${SB_SESSION_ID}"   # clients without CLAUDE_PID
fi
# One line, three TAB-separated fields, trailing newline. The write is NOT atomic and
# cannot be made so: mv into place needs a fork, and Claude Code cancels the in-flight
# status script on every event. The reader therefore rejects anything that is not
# exactly three fields ending in a newline (I-14).
if [ -n "$sb_model_key" ] && [ -n "$sb_model_id$sb_model_name" ]; then
  printf '%s\t%s\t%s\n' "$sb_model_id" "$sb_model_name" "$SB_NOW" \
    > "$SB_TMP/$sb_model_key" 2>/dev/null
fi

export SB_EXPORT_DONE=1
