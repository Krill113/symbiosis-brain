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

# One session-id parser for every hook (sb-hooklib.sh). Fail-open: without the library
# SESSION_ID stays empty, the line still renders, only the markers are skipped.
sb_dir=${BASH_SOURCE[0]}
# Both separators: bash invoked with a Windows-style path leaves no '/' to cut on,
# and the library then silently fails to load.
case $sb_dir in *[/\\]*) sb_dir=${sb_dir%[/\\]*} ;; *) sb_dir=. ;; esac
if ! declare -F sb_session_id >/dev/null 2>&1; then
  . "$sb_dir/sb-hooklib.sh" 2>/dev/null || true
fi

SESSION_ID=
SB_TMP="${TMPDIR:-${TEMP:-/tmp}}"
if declare -F sb_session_id >/dev/null 2>&1; then
  sb_tmp_dir
  sb_session_id "$INPUT"
  SESSION_ID=$SB_SESSION_ID
fi

# The same ladder the recall hook walks (sb-hooklib.sh), so this line shows the scope
# that will actually be searched rather than a hopeful default. 'all*' means
# unresolved — no marker up the tree, no session bridge file — and recall then
# searches every scope instead of silently narrowing to global.
SCOPE='all*'
if declare -F sb_resolve_scope >/dev/null 2>&1; then
  sb_resolve_scope "$INPUT" "$SESSION_ID"
  SCOPE="${SB_SCOPE:-all*}"
elif [ -n "${SYMBIOSIS_BRAIN_SCOPE:-}" ]; then
  SCOPE="$SYMBIOSIS_BRAIN_SCOPE"
fi
# Defaults MUST match brain-save-trigger.sh:13 (25,35,45) and :144 (30,60,85) — this
# line advertises the thresholds the Stop-hook actually fires on. It shipped 40/70/90
# for months, visible only on installs that don't set the env explicitly.
SAVE_THR="${SYMBIOSIS_BRAIN_SAVE_THRESHOLDS:-25,35,45}";  SAVE_THR=${SAVE_THR//,//}
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

# Vault sync alarm (brain-sync.sh). Fork-free: a test and a builtin read.
SYNC_WARN=
sb_sync_marker="$SB_TMP/brain-sync-failed"
if [ -r "$sb_sync_marker" ]; then
  read -r sb_sync_line < "$sb_sync_marker"
  sb_sync_stage=${sb_sync_line#stage=}
  sb_sync_stage=${sb_sync_stage%% *}
  [ -n "$sb_sync_stage" ] && SYNC_WARN="  ⚠️sync:${sb_sync_stage}"
fi

printf '🧠 [Symbiosis-Brain]  scope: %s  auto-save: [%s]  rules: [%s·R%s]  last-save: %s%%%s\n' \
  "$SCOPE" "$SAVE_THR" "$RULES_THR" "$RULES_R" "$LAST_SAVE" "$SYNC_WARN"
