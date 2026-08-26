#!/bin/bash
# Claude Code status line with progress bars and rate limits.
#
# Fork-free by design: every value is extracted with bash builtins (regex match,
# parameter expansion, printf -v) instead of grep/sed/cut/seq/date pipelines.
# Claude Code re-runs the status line on every event with a 300ms debounce and
# cancels the in-flight script when a new event arrives; on Windows/MSYS a child
# caught mid-fork by that cancel is stranded as a suspended orphan forever. Each
# avoided fork removes ~85ms of latency and one leak window.
#
# Input: session JSON on stdin, or pre-read into $SB_STATUSLINE_INPUT when sourced.

if [ -n "${SB_STATUSLINE_INPUT+set}" ]; then
  data=$SB_STATUSLINE_INPUT
else
  IFS= read -r -d '' data
fi

# Extraction helpers — set REPLY instead of writing to stdout, so callers never
# need a command substitution (which would fork).
sb_str() {
  if [[ $data =~ \"$1\"[[:space:]]*:[[:space:]]*\"([^\"]*)\" ]]; then REPLY=${BASH_REMATCH[1]}; else REPLY=; fi
}
sb_int() {  # integer part only — matches the old `cut -d. -f1`
  if [[ $data =~ \"$1\"[[:space:]]*:[[:space:]]*([0-9]+) ]]; then REPLY=${BASH_REMATCH[1]}; else REPLY=; fi
}

sb_str cwd;             cwd=${REPLY##*[/\\]}
sb_str display_name;    model=${REPLY#Claude }
sb_int used_percentage; ctx=$REPLY
sb_str session_id;      session_id=$REPLY

effort=
sb_settings=~/.claude/settings.json
if [ -r "$sb_settings" ]; then
  settings=$(<"$sb_settings")
  [[ $settings =~ \"effortLevel\"[[:space:]]*:[[:space:]]*\"([^\"]*)\" ]] && effort=${BASH_REMATCH[1]}
fi

SB_TMP="${TMPDIR:-${TEMP:-/tmp}}"

# Bridges (context %, rate limits) live in sb-export.sh — the single export point,
# which sb-statusline.sh runs BEFORE it decides whose first line to render. When this
# file is invoked on its own (a supported entry point), run them here instead.
if [ -z "${SB_EXPORT_DONE:-}" ]; then
  sb_dir=${BASH_SOURCE[0]%/*}
  [ "$sb_dir" = "${BASH_SOURCE[0]}" ] && sb_dir=.
  SB_STATUSLINE_INPUT=$data
  . "$sb_dir/sb-hooklib.sh" 2>/dev/null || true
  . "$sb_dir/sb-export.sh" 2>/dev/null || true
fi

# Unix seconds, resolved once per render. $EPOCHSECONDS is a bash 5 builtin and costs
# nothing; older bash — notably macOS's stock /bin/bash 3.2 — pays for a single `date`.
if [ -z "${SB_NOW:-}" ]; then
  if [ -n "${EPOCHSECONDS:-}" ]; then SB_NOW=$EPOCHSECONDS; else SB_NOW=$(date +%s); fi
fi

# Rate limit data
rate5h= reset5h= rate7d=
if [[ $data =~ \"five_hour\"[[:space:]]*:[[:space:]]*\{([^}]*)\} ]]; then
  sb_fh=${BASH_REMATCH[1]}
  [[ $sb_fh =~ \"used_percentage\"[[:space:]]*:[[:space:]]*([0-9]+) ]] && rate5h=${BASH_REMATCH[1]}
  [[ $sb_fh =~ \"resets_at\"[[:space:]]*:[[:space:]]*([0-9]+) ]] && reset5h=${BASH_REMATCH[1]}
fi
if [[ $data =~ \"seven_day\"[[:space:]]*:[[:space:]]*\{([^}]*)\} ]]; then
  sb_sd=${BASH_REMATCH[1]}
  [[ $sb_sd =~ \"used_percentage\"[[:space:]]*:[[:space:]]*([0-9]+) ]] && rate7d=${BASH_REMATCH[1]}
fi

# Progress bar: ████░░░░░░ (10 chars) -> REPLY
sb_bar() {
  local pct=${1:-0} width=10 filled empty color f e
  [[ $pct =~ ^[0-9]+$ ]] || pct=0
  filled=$(( pct * width / 100 ))
  (( filled > width )) && filled=$width
  (( filled < 0 )) && filled=0
  empty=$(( width - filled ))
  color=$'\033[32m'                      # green
  (( pct >= 50 )) && color=$'\033[33m'   # yellow
  (( pct >= 80 )) && color=$'\033[31m'   # red
  printf -v f '%*s' "$filled" ''; f=${f// /█}
  printf -v e '%*s' "$empty" '';  e=${e// /░}
  REPLY="${color}${f}"$'\033[90m'"${e}"$'\033[0m'
}

# Time remaining until reset (e.g. "2:34" or "0:12") -> REPLY
sb_eta() {
  local resets_at=$1 diff hours mins
  REPLY=
  [ -z "$resets_at" ] && return
  diff=$(( resets_at - SB_NOW ))
  if (( diff <= 0 )); then REPLY="0:00"; return; fi
  hours=$(( diff / 3600 ))
  mins=$(( (diff % 3600) / 60 ))
  printf -v REPLY '%d:%02d' "$hours" "$mins"
}

parts=""
[ -n "$cwd" ] && parts="$cwd"
if [ -n "$model" ]; then
  [ -n "$effort" ] && model="$model ($effort)"
  parts="$parts | $model"
fi
if [ -n "$ctx" ]; then
  sb_bar "$ctx"
  parts="$parts | ctx:$REPLY ${ctx}%"
fi
# 5h reset timer (digits)
if [ -n "$reset5h" ]; then
  sb_eta "$reset5h"
  parts="$parts | "$'\033[36m'"⏱"$'\033[0m'" $REPLY"
fi
# Rate limit bars
if [ -n "$rate5h" ]; then
  sb_bar "$rate5h"
  parts="$parts | 5h:$REPLY ${rate5h}%"
fi
if [ -n "$rate7d" ]; then
  sb_bar "$rate7d"
  parts="$parts | 7d:$REPLY ${rate7d}%"
fi

printf '%s\n' "$parts"
