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
PA_TOOL=$(printf '%s' "$INPUT" | grep -oE '"tool_name": *"[^"]*"' | head -1 | sed -E 's/.*: *"//;s/"$//')
PA_SID=$(printf '%s' "$INPUT" | grep -oE '"session_id": *"[^"]*"' | head -1 | sed -E 's/.*: *"//;s/"$//')
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

# ── Action-recall: pure-bash pre-action command matcher (Stage 1) ──
# Warns from past mistakes at the MOMENT a risky Bash/PowerShell command is
# about to run, from a TSV compiled ahead of time by
# `symbiosis-brain compile-action-rules` (see action_rules.py) — no uv/python
# here, must stay instant. On a hit we emit additionalContext ourselves and
# skip the python pre-action-recall call below (avoids two JSON blobs on
# stdout). Fail-open at every step: any missing piece just falls through to
# the existing path further down.
#
# Two-stage matcher: the overwhelming majority of commands match NO rule at
# all, so a per-toolkey "fast-reject" file (all of that tool's patterns
# combined) is checked first with ONE `grep -qEf` fork — was one grep fork
# PER rule row before (~180ms each on Windows, linear in rule count: ~2.9s
# at 16 rows). Only on an actual hit (rare — it means a warning is about to
# fire) do we pay the per-row loop below to identify *which* rule matched;
# that loop reuses the exact same grep -E engine action_rules.py validated
# the pattern with, so identification never diverges from validation.
case "$PA_TOOL" in
  Bash|PowerShell)
    case "$PA_TOOL" in
      Bash) AR_TOOLKEY=bash ;;
      PowerShell) AR_TOOLKEY=powershell ;;
    esac
    AR_RULES="${SYMBIOSIS_BRAIN_VAULT}/.index/action-rules.tsv"
    AR_FAST_RE="${SYMBIOSIS_BRAIN_VAULT}/.index/action-rules.${AR_TOOLKEY}.re"
    if [ -f "$AR_RULES" ] && [ -s "$AR_FAST_RE" ]; then
      # Honor the pre-action config's off-switches (previously only the env
      # kill-switch above worked for this path). One cheap grep, only paid
      # when the action-rule files actually exist.
      AR_CONFIG="${HOME:-$USERPROFILE}/.claude/symbiosis-brain-pre-action.json"
      AR_SKIP=""
      if [ -f "$AR_CONFIG" ]; then
        if grep -q '"enabled"[[:space:]]*:[[:space:]]*false' "$AR_CONFIG" 2>/dev/null; then
          AR_SKIP=1
        elif grep -q '"matchers"' "$AR_CONFIG" 2>/dev/null; then
          if ! grep -q "\"matchers\"[^]]*\"$PA_TOOL\"" "$AR_CONFIG" 2>/dev/null; then
            AR_SKIP=1
          fi
        fi
      fi
      if [ -z "$AR_SKIP" ]; then
        # Extract tool_input.command (whitespace-tolerant after the colon,
        # matching the "tool_name"/"session_id" extractions above), then
        # undo the JSON string escaping so multi-line scripts and quoted
        # args match as the real bytes a shell would see — not the literal
        # two-character sequences `\n` / `\"` the JSON encoding left behind.
        # grep (no -z) then tests each real line independently, so a risky
        # command buried on line 2 of a heredoc/multi-step script is seen.
        AR_CMD=$(printf '%s' "$INPUT" | grep -oE '"command": *"([^"\\]|\\.)*"' | head -1 | sed -E 's/^"command": *"//; s/"$//')
        if [ -n "$AR_CMD" ]; then
          AR_CMD=$(printf '%s' "$AR_CMD" | sed -e 's/\\\\/\x01/g' -e 's/\\n/\n/g' -e 's/\\t/\t/g' -e 's/\\r/\r/g' -e 's/\\"/"/g' -e 's/\x01/\\/g')
          if printf '%s' "$AR_CMD" | grep -qEf "$AR_FAST_RE" 2>/dev/null; then
            AR_HIT_ID=""
            AR_HIT_HINT=""
            while IFS=$'\t' read -r AR_T AR_ID AR_RE AR_HINT || [ -n "$AR_T" ]; do
              AR_T="${AR_T%$'\r'}"
              AR_HINT="${AR_HINT%$'\r'}"
              [ "$AR_T" = "$AR_TOOLKEY" ] || continue
              [ -n "$AR_RE" ] || continue
              if printf '%s' "$AR_CMD" | grep -qE "$AR_RE" 2>/dev/null; then
                AR_HIT_ID="$AR_ID"
                AR_HIT_HINT="$AR_HINT"
                break
              fi
            done < "$AR_RULES"
            if [ -n "$AR_HIT_ID" ]; then
              AR_HITS_FILE="${SYMBIOSIS_BRAIN_VAULT}/.index/action-rule-hits.jsonl"
              AR_TS=$(date -Iseconds 2>/dev/null || date)
              printf '{"ts":"%s","session_id":"%s","rule_id":"%s","tool":"%s"}\n' \
                "$AR_TS" "$PA_SID" "$AR_HIT_ID" "$AR_TOOLKEY" \
                >> "$AR_HITS_FILE" 2>/dev/null || true
              # Unbounded-growth guard: rotate down to the last 500 hits once
              # the file passes ~1000 lines. Only paid on an actual hit.
              AR_HITS_N=$(wc -l < "$AR_HITS_FILE" 2>/dev/null || echo 0)
              case "$AR_HITS_N" in ''|*[!0-9]*) AR_HITS_N=0 ;; esac
              if [ "$AR_HITS_N" -gt 1000 ] 2>/dev/null; then
                tail -n 500 "$AR_HITS_FILE" > "${AR_HITS_FILE}.tmp" 2>/dev/null \
                  && mv "${AR_HITS_FILE}.tmp" "$AR_HITS_FILE" 2>/dev/null || true
              fi
              printf '{"hookSpecificOutput":{"hookEventName":"PreToolUse","additionalContext":"[action-rule %s] %s"}}\n' \
                "$AR_HIT_ID" "$AR_HIT_HINT"
              exit 0
            fi
          fi
        fi
      fi
    fi
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
