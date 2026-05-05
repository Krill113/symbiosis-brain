#!/bin/bash
# Claude Code status line with progress bars and rate limits

data=$(cat)

get_str() { echo "$data" | grep -o "\"$1\":\"[^\"]*\"" | head -1 | sed 's/.*":\s*"//;s/"$//'; }
get_num() { echo "$data" | grep -o "\"$1\":[0-9.]*" | head -1 | sed 's/.*://'; }

cwd=$(get_str cwd | sed 's|.*[/\\]||')
model=$(get_str display_name | sed 's/Claude //')
effort=$(grep -o '"effortLevel":"[^"]*"' ~/.claude/settings.json 2>/dev/null | sed 's/.*:"//;s/"//')
ctx=$(get_num used_percentage | head -1 | cut -d. -f1)
session_id=$(get_str session_id)

# Export context % per-session for brain-save-trigger.sh (avoid cross-session bleed)
[ -n "$ctx" ] && [ -n "$session_id" ] && echo "$ctx" > "/tmp/brain-context-pct-${session_id}"

# Rate limit data
rate5h=$(echo "$data" | grep -o '"five_hour":{[^}]*}' | grep -o '"used_percentage":[0-9.]*' | sed 's/.*://' | cut -d. -f1)
reset5h=$(echo "$data" | grep -o '"five_hour":{[^}]*}' | grep -o '"resets_at":[0-9]*' | sed 's/.*://')
rate7d=$(echo "$data" | grep -o '"seven_day":{[^}]*}' | grep -o '"used_percentage":[0-9.]*' | sed 's/.*://' | cut -d. -f1)

# Progress bar: ████░░░░░░ (10 chars)
bar() {
  local pct=${1:-0} width=10
  local filled=$((pct * width / 100))
  [ $filled -gt $width ] && filled=$width
  local empty=$((width - filled))
  local color="\033[32m"  # green
  [ "$pct" -ge 50 ] && color="\033[33m"  # yellow
  [ "$pct" -ge 80 ] && color="\033[31m"  # red
  printf "${color}%s\033[90m%s\033[0m" \
    "$(printf '█%.0s' $(seq 1 $filled 2>/dev/null))" \
    "$(printf '░%.0s' $(seq 1 $empty 2>/dev/null))"
}

# Time remaining until reset (e.g. "2:34" or "0:12")
time_remaining() {
  local resets_at=$1
  [ -z "$resets_at" ] && return
  local now=$(date +%s)
  local diff=$((resets_at - now))
  [ $diff -le 0 ] && { echo "0:00"; return; }
  local hours=$((diff / 3600))
  local mins=$(( (diff % 3600) / 60 ))
  printf "%d:%02d" $hours $mins
}

parts=""
[ -n "$cwd" ] && parts="$cwd"
if [ -n "$model" ]; then
  [ -n "$effort" ] && model="$model ($effort)"
  parts="$parts | $model"
fi
if [ -n "$ctx" ]; then
  parts="$parts | ctx:$(bar $ctx) ${ctx}%"
fi
# 5h reset timer (digits)
if [ -n "$reset5h" ]; then
  remaining=$(time_remaining $reset5h)
  parts="$parts | \033[36m⏱\033[0m ${remaining}"
fi
# Rate limit bars
if [ -n "$rate5h" ]; then
  parts="$parts | 5h:$(bar $rate5h) ${rate5h}%"
fi
if [ -n "$rate7d" ]; then
  parts="$parts | 7d:$(bar $rate7d) ${rate7d}%"
fi

echo -e "$parts"
