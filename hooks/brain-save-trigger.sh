#!/bin/bash
# brain-save-trigger.sh — proactive brain-save trigger
# Modes: stop (threshold check) | precompact (last-chance blocker) | prompt-check (inject reminder)
#
# Stop-mode logic (decision: decisions/stop-hook-smart-trigger.md):
#   - Thresholds fire at 40/70/90% absolute context usage
#   - Delta-guard: skip if (PCT - last_save_pct) < DELTA_GUARD, except ≥90% (always fires)
#   - SAVE_LATER marker lets the user skip ONE trigger in the soft zone (40-70%)
#   - Zone-based messaging (soft / serious / last-chance)

THRESHOLDS=(40 70 90)
DELTA_GUARD=20

MODE="${1:-stop}"
INPUT=$(cat)

SESSION_ID=$(echo "$INPUT" | grep -o '"session_id":"[^"]*"' | head -1 | sed 's/.*":"//;s/"$//')
[ -z "$SESSION_ID" ] && SESSION_ID="default"

TRIGGERED_FILE="/tmp/brain-triggered-${SESSION_ID}"

if [ "$MODE" = "stop" ]; then
  # Prevent infinite loop in blocking mode
  echo "$INPUT" | grep -q '"stop_hook_active":true' && exit 0

  # Read context percentage from statusline export (per-session, updated every 10s)
  PCT=$(cat "/tmp/brain-context-pct-${SESSION_ID}" 2>/dev/null)
  [ -z "$PCT" ] && exit 0

  # Read last-save marker (written by skill brain-save after successful brain_write)
  LAST_SAVE_PCT=$(cat "/tmp/brain-last-save-pct-${SESSION_ID}" 2>/dev/null)
  [ -z "$LAST_SAVE_PCT" ] && LAST_SAVE_PCT=0
  DELTA=$((PCT - LAST_SAVE_PCT))

  SAVE_LATER_FILE="/tmp/brain-save-later-${SESSION_ID}"

  # Find HIGHEST uncrossed threshold that PCT has crossed (iterate in reverse).
  # Rationale: when PCT jumps past multiple thresholds, we want the message to
  # reflect the current zone, not the lowest threshold. Example: PCT=72 fires
  # zone 70 ("serious"), not zone 40 ("soft").
  for (( i=${#THRESHOLDS[@]}-1; i>=0; i-- )); do
    T="${THRESHOLDS[$i]}"
    if [ "$PCT" -ge "$T" ] 2>/dev/null; then
      if ! grep -q "^${T}$" "$TRIGGERED_FILE" 2>/dev/null; then
        # Delta-guard: skip if recently saved (except at/above 90%, which is critical)
        if [ "$T" -lt 90 ] && [ "$DELTA" -lt "$DELTA_GUARD" ] 2>/dev/null; then
          exit 0
        fi

        # SAVE_LATER: one-shot skip, soft zone only (40-70%)
        if [ "$T" -ge 40 ] && [ "$T" -lt 70 ] && [ -f "$SAVE_LATER_FILE" ]; then
          rm -f "$SAVE_LATER_FILE"
          exit 0
        fi

        # Mark all passed thresholds as triggered (so lower zones don't re-fire)
        for MARK in "${THRESHOLDS[@]}"; do
          if [ "$MARK" -le "$PCT" ] 2>/dev/null && ! grep -q "^${MARK}$" "$TRIGGERED_FILE" 2>/dev/null; then
            echo "$MARK" >> "$TRIGGERED_FILE"
          fi
        done

        # Zone-based message
        if [ "$T" -lt 70 ]; then
          echo "🧠 Контекст ${PCT}%, delta +${DELTA}%. Сохрани если есть что — или скажи SAVE_LATER чтобы пропустить раз." >&2
        elif [ "$T" -lt 90 ]; then
          echo "🧠 Контекст ${PCT}% — пора сохранять знание, скоро /compact." >&2
        else
          echo "🧠 Контекст ${PCT}% — последний шанс сохранить перед авто-компакцией." >&2
        fi
        exit 2
      fi
    fi
  done
  exit 0
fi  # end of stop mode

if [ "$MODE" = "precompact" ]; then
  PRECOMPACT_FILE="/tmp/brain-precompact-${SESSION_ID}"
  if [ ! -f "$PRECOMPACT_FILE" ]; then
    touch "$PRECOMPACT_FILE"
    touch "/tmp/brain-precompact-pending-${SESSION_ID}"
    echo "🧠 Save memory? Type any message to trigger brain-save. Just /compact again to skip." >&2
    exit 2
  fi
  exit 0
fi

if [ "$MODE" = "prompt-check" ]; then
  # ── Inputs ──────────────────────────────────────────────
  PROMPT=$(echo "$INPUT" | grep -o '"prompt":"[^"]*"' | head -1 | sed 's/.*":"//;s/"$//')
  SCOPE="${SYMBIOSIS_BRAIN_SCOPE:-global}"

  # Pending compact relay (existing behaviour)
  PENDING_BLOCK=""
  PENDING="/tmp/brain-precompact-pending-${SESSION_ID}"
  if [ -f "$PENDING" ]; then
    rm -f "$PENDING"
    PENDING_BLOCK="🧠 Compaction was blocked. Run brain-save to preserve knowledge, then tell user to repeat /compact."
  fi

  # ── Defaults / config ───────────────────────────────────
  RECALL_ENABLED="${SYMBIOSIS_BRAIN_RECALL_ENABLED:-true}"
  RECALL_TOP_K="${SYMBIOSIS_BRAIN_RECALL_TOP_K:-5}"
  RECALL_SKIP_SHORT_CHARS="${SYMBIOSIS_BRAIN_RECALL_SKIP_SHORT_CHARS:-15}"

  RULES_ENABLED="${SYMBIOSIS_BRAIN_RULES_ENABLED:-true}"
  RULES_ZONES="${SYMBIOSIS_BRAIN_RULES_ZONES:-30,60,85}"
  RULES_TURN_INTERVAL="${SYMBIOSIS_BRAIN_RULES_TURN_INTERVAL:-10}"
  RULES_TEXT="${SYMBIOSIS_BRAIN_RULES_TEXT:-Отвечай tight, без воды. Большие чтения / поиск / анализ — делегируй субагентам.
Доступно: brain_search/brain_read (память), Serena (find_symbol/replace_symbol_body), субагенты (Explore/general-purpose).}"

  VAULT="${SYMBIOSIS_BRAIN_VAULT:-}"

  # ── A1: memory recall ───────────────────────────────────
  MEMORY_BLOCK=""
  SKIP_RECALL=0
  # bash ${#var} returns bytes on git-bash/Windows, not chars — use Python for char count.
  # PYTHONIOENCODING=utf-8 needed because Python's default stdin encoding on Windows is cp1251.
  PROMPT_LEN=$(printf '%s' "$PROMPT" | PYTHONIOENCODING=utf-8 python -c 'import sys; print(len(sys.stdin.read()))' 2>/dev/null || echo "${#PROMPT}")
  if [ "$RECALL_ENABLED" != "true" ]; then SKIP_RECALL=1; fi
  if [ "$PROMPT_LEN" -lt "$RECALL_SKIP_SHORT_CHARS" ]; then SKIP_RECALL=1; fi
  case "$PROMPT" in
    /*) SKIP_RECALL=1 ;;
  esac
  if echo "$PROMPT" | grep -qiE '^(да|нет|ок|yes|no|ok|continue|продолжай)$'; then
    SKIP_RECALL=1
  fi

  if [ "$SKIP_RECALL" = "0" ] && [ -n "$VAULT" ]; then
    # Timeout 12s: measured cold-start ~9.6s (Python+fastembed+sqlite-vec import,
    # sync_all, search), warm ~3.6s. Fresh process per prompt — every call pays
    # the full cost. Daemon/pre-warm tracked separately as A-v2.
    GIST_JSON=$(timeout 12 python -m symbiosis_brain search-gist \
      --vault "$VAULT" --query "$PROMPT" --scope "$SCOPE" \
      --limit "$RECALL_TOP_K" 2>/dev/null || echo "[]")
    HITS=$(echo "$GIST_JSON" | python -c "import sys,json
try:
    d=json.load(sys.stdin)
    if not d: sys.exit(0)
    print(f'[memory: {len(d)} hits, scope={\"$SCOPE\"}]')
    for n in d:
        print(f\"- {n['path']} — {n.get('gist','')}\")
except Exception:
    pass
" 2>/dev/null)
    if [ -n "$HITS" ]; then MEMORY_BLOCK="$HITS"; fi
  fi

  # ── A2: rules ───────────────────────────────────────────
  RULES_BLOCK=""
  if [ "$RULES_ENABLED" = "true" ]; then
    PCT=$(cat "/tmp/brain-context-pct-${SESSION_ID}" 2>/dev/null || echo 0)
    [ -z "$PCT" ] && PCT=0
    SHOWN_FILE="/tmp/brain-rules-shown-${SESSION_ID}"
    TURN_FILE="/tmp/brain-rules-turn-counter-${SESSION_ID}"
    TURNS=$(cat "$TURN_FILE" 2>/dev/null || echo 0)
    [ -z "$TURNS" ] && TURNS=0
    TURNS=$((TURNS + 1))

    ZONE_HIT=0
    HIGHEST_CROSSED=-1
    HIGHEST_SHOWN=-1
    for Z in $(echo "$RULES_ZONES" | tr ',' ' '); do
      if [ "$PCT" -ge "$Z" ] 2>/dev/null; then
        [ "$Z" -gt "$HIGHEST_CROSSED" ] 2>/dev/null && HIGHEST_CROSSED=$Z
        if grep -q "^${Z}$" "$SHOWN_FILE" 2>/dev/null; then
          [ "$Z" -gt "$HIGHEST_SHOWN" ] 2>/dev/null && HIGHEST_SHOWN=$Z
        fi
      fi
    done
    # Trigger only if the highest crossed zone hasn't been shown yet
    if [ "$HIGHEST_CROSSED" -gt "$HIGHEST_SHOWN" ] 2>/dev/null; then
      ZONE_HIT=1
      # Mark all crossed zones so they don't re-trigger individually
      for Z in $(echo "$RULES_ZONES" | tr ',' ' '); do
        if [ "$PCT" -ge "$Z" ] 2>/dev/null; then
          grep -q "^${Z}$" "$SHOWN_FILE" 2>/dev/null || echo "$Z" >> "$SHOWN_FILE"
        fi
      done
    fi

    if [ "$ZONE_HIT" = "1" ] || [ "$TURNS" -ge "$RULES_TURN_INTERVAL" ]; then
      RULES_BLOCK="[rules — context ${PCT}%]
${RULES_TEXT}"
      echo "0" > "$TURN_FILE"
    else
      echo "$TURNS" > "$TURN_FILE"
    fi
  fi

  # ── Compose <system-reminder> ───────────────────────────
  COMBINED=""
  if [ -n "$MEMORY_BLOCK" ]; then COMBINED="$MEMORY_BLOCK"; fi
  if [ -n "$RULES_BLOCK" ]; then
    if [ -n "$COMBINED" ]; then COMBINED="$COMBINED

$RULES_BLOCK"
    else COMBINED="$RULES_BLOCK"; fi
  fi
  if [ -n "$PENDING_BLOCK" ]; then
    if [ -n "$COMBINED" ]; then COMBINED="$COMBINED

$PENDING_BLOCK"
    else COMBINED="$PENDING_BLOCK"; fi
  fi

  if [ -n "$COMBINED" ]; then
    echo "<system-reminder>"
    echo "$COMBINED"
    echo "</system-reminder>"
  fi
  exit 0
fi  # end of prompt-check mode

exit 0
