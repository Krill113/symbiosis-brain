#!/bin/bash
# Symbiosis Brain — helpers shared by every hook and both status lines.
#
# Fork-free by contract: bash builtins only (regex match, parameter expansion).
# The status line sources this file, and Claude Code cancels the in-flight status
# script on every new event; on Windows/MSYS a child caught mid-fork by that cancel
# is stranded as a suspended orphan forever. See sb-statusline.sh for the full note.
#
# The functions set variables instead of echoing, so callers never need a command
# substitution (which would fork). Sourced, never executed.

# -> SB_PY_CMD: direct installed interpreter, checkout venv, or legacy uv.
# Selection is fork-free; a selected interpreter's failure is never retried.
sb_python_command() {
  SB_PY_CMD=()
  if [ -n "${SYMBIOSIS_BRAIN_PYTHON:-}" ] && [ -f "$SYMBIOSIS_BRAIN_PYTHON" ]; then
    SB_PY_CMD=("$SYMBIOSIS_BRAIN_PYTHON")
  elif [ -f "${SYMBIOSIS_BRAIN_TOOLS:-}/.venv/Scripts/python.exe" ]; then
    SB_PY_CMD=("$SYMBIOSIS_BRAIN_TOOLS/.venv/Scripts/python.exe")
  elif [ -f "${SYMBIOSIS_BRAIN_TOOLS:-}/.venv/bin/python" ]; then
    SB_PY_CMD=("$SYMBIOSIS_BRAIN_TOOLS/.venv/bin/python")
  elif [ -n "${SYMBIOSIS_BRAIN_TOOLS:-}" ] && command -v uv >/dev/null 2>&1; then
    SB_PY_CMD=(uv run --quiet --directory "$SYMBIOSIS_BRAIN_TOOLS" python)
  fi
}

# -> SB_TMP: the temp dir every hook artifact lives in.
sb_tmp_dir() {
  SB_TMP="${TMPDIR:-${TEMP:-/tmp}}"
}

# $1 = hook payload (JSON as a single string) -> SB_SESSION_ID ("" when absent).
# One parser for all hooks: brain-session-start.sh used to match "session_id":"x"
# and brain-save-trigger.sh "session_id": "x", so whichever spacing the harness
# emitted, one of them silently read an empty session id.
sb_session_id() {
  SB_SESSION_ID=
  [[ $1 =~ \"session_id\"[[:space:]]*:[[:space:]]*\"([^\"]*)\" ]] && SB_SESSION_ID=${BASH_REMATCH[1]}
}

# -> SB_SCOPE ("" when the text carries no usable marker). $1 = file content.
# Mirrors scope_resolver.parse_marker: the LAST complete marker wins, `scope=` is
# required, whitespace around the keys is tolerated. Two stages on purpose. A single
# pattern that reaches straight for the value lets a dash-bearing scope run into the
# closing delimiter (scope=a-b--> captured as "a-b--"), and lets a malformed last
# marker backtrack into an earlier, stale one. Both hand the caller a plausible-looking
# WRONG scope, which is the one outcome this resolver exists to prevent.
sb_marker_scope() {
  SB_SCOPE=
  local content=$1 marker= body= part= key= val=
  local -a parts=()
  [[ $content =~ .*(\<!--[[:space:]]*symbiosis-brain[[:space:]]+v[0-9]+[[:space:]]*:[^\>]*--\>) ]] || return 0
  marker=${BASH_REMATCH[1]}
  body=${marker#*:}
  body=${body%--\>}
  IFS=',' read -ra parts <<< "$body"
  for part in "${parts[@]}"; do
    key=${part%%=*}; val=${part#*=}
    # Trim the edges only: an inner space means a malformed value, not one to compact.
    key=${key#"${key%%[![:space:]]*}"}; key=${key%"${key##*[![:space:]]}"}
    val=${val#"${val%%[![:space:]]*}"}; val=${val%"${val##*[![:space:]]}"}
    if [ "$key" = "scope" ] && [[ $val =~ ^[A-Za-z0-9_-]+$ ]]; then
      SB_SCOPE=$val
      return 0
    fi
  done
  return 0
}

# $1 = directory -> SB_SCOPE, SB_SCOPE_DEPTH (0 = the directory itself, 1 = its parent).
# Walks up to the nearest CLAUDE.md and reads its marker. The walk stops at the FIRST
# CLAUDE.md it meets, marked or not: a directory that ships a CLAUDE.md without a marker
# declares no scope, and must not silently inherit an unrelated ancestor's one (live
# example: a git worktree whose CLAUDE.md lost the marker its sibling still carries).
sb_scope_from_dir() {
  SB_SCOPE=; SB_SCOPE_DEPTH=0
  local dir=$1 depth=0 bs
  printf -v bs '\134'   # a backslash, spelled in octal: a literal one does
                       # not survive every transport that edits this file.
  dir=${dir//"$bs"/"/"}
  while [[ $dir == *"//"* ]]; do dir=${dir//"//"/"/"}; done
  while [ -n "$dir" ] && [ "$depth" -lt 10 ]; do
    if [ -f "$dir/CLAUDE.md" ]; then
      [ -r "$dir/CLAUDE.md" ] && sb_marker_scope "$(<"$dir/CLAUDE.md")"
      SB_SCOPE_DEPTH=$depth
      return 0
    fi
    [[ $dir == */* ]] || break
    dir=${dir%/*}
    depth=$((depth+1))
  done
  return 0
}

# $1 = scope -> 0 when the vault's taxonomy carries a row for it.
# Fail-open: with no taxonomy to read (fresh install, vault not mounted) every scope
# counts as registered — the check may narrow a guess, never lose a known scope.
sb_scope_registered() {
  local sc=$1 tax="${SYMBIOSIS_BRAIN_VAULT:-}/reference/scope-taxonomy.md" content= re=
  [ -n "$sc" ] || return 1
  [ -n "${SYMBIOSIS_BRAIN_VAULT:-}" ] && [ -r "$tax" ] || return 0
  content=$(<"$tax")
  re='\|[[:space:]]*`'"$sc"'`[[:space:]]*\|'
  [[ $content =~ $re ]]
}

# $1 = scope -> 0 when the taxonomy types it as an umbrella or a workspace root: a scope
# that legitimately exists but that no project may INHERIT from an ancestor directory
# (searching an umbrella from inside a product violates the routing model).
sb_scope_is_broad() {
  local sc=$1 tax="${SYMBIOSIS_BRAIN_VAULT:-}/reference/scope-taxonomy.md" content= row= re=
  [ -n "$sc" ] || return 1
  [ -n "${SYMBIOSIS_BRAIN_VAULT:-}" ] && [ -r "$tax" ] || return 1
  content=$(<"$tax")
  re='\|[[:space:]]*`'"$sc"'`[[:space:]]*\|([^|]*)\|'
  [[ $content =~ $re ]] || return 1
  row=${BASH_REMATCH[1]}
  case $row in
    *umbrella-member*) return 1 ;;
    *workspace-root*|*зонтик*|*umbrella*) return 0 ;;
  esac
  return 1
}

# $1 = hook payload (JSON as a single string, "" allowed), $2 = session id ("" allowed)
# -> SB_SCOPE ("" = unresolved) and SB_SCOPE_SRC (cwd|session|env|none).
# The ladder exists because no single channel is reliable: CLAUDE_ENV_FILE reaches Bash
# tool commands but not hook processes, so a hook that trusts the env var alone reads
# nothing at all. An unresolved scope stays EMPTY on purpose — the caller must then
# search every scope instead of substituting 'global', which is a hard filter that hides
# every project note behind a filter no caller asked for.
sb_resolve_scope() {
  SB_SCOPE=; SB_SCOPE_SRC=none
  local payload=$1 sid=$2 f=
  if [[ $payload =~ \"cwd\"[[:space:]]*:[[:space:]]*\"([^\"]*)\" ]]; then
    sb_scope_from_dir "${BASH_REMATCH[1]}"
    if [ -n "$SB_SCOPE" ]; then
      if [ "${SB_SCOPE_DEPTH:-0}" -gt 0 ] && sb_scope_is_broad "$SB_SCOPE"; then
        SB_SCOPE=
      else
        SB_SCOPE_SRC=cwd
        return 0
      fi
    fi
  fi
  if [ -n "$sid" ]; then
    f="${SB_TMP:-${TMPDIR:-${TEMP:-/tmp}}}/brain-scope-${sid}"
    if [ -r "$f" ]; then
      read -r SB_SCOPE < "$f" || true
      if [ -n "$SB_SCOPE" ]; then
        SB_SCOPE_SRC=session
        return 0
      fi
    fi
  fi
  if [ -n "${SYMBIOSIS_BRAIN_SCOPE:-}" ]; then
    SB_SCOPE=$SYMBIOSIS_BRAIN_SCOPE
    SB_SCOPE_SRC=env
    return 0
  fi
  SB_SCOPE=
  return 0
}

# Decode selected JSON strings with builtins only. Unicode is emitted as UTF-8
# bytes, including surrogate pairs, independently of the shell locale. Control
# characters are rejected so model names cannot inject terminal escape sequences.
sb_json_text() {
  local rest=${1:1} ch esc hex oct code low bytes byte
  rest=${rest%\"}
  REPLY=
  [ "${#rest}" -le 1024 ] || return 1
  if [[ $rest != *\\* ]]; then
    [[ $rest == *[[:cntrl:]]* ]] && return 1
    REPLY=$rest
    return 0
  fi
  while [ -n "$rest" ]; do
    ch=${rest:0:1}; rest=${rest:1}
    if [ "$ch" = '\' ]; then
      esc=${rest:0:1}; rest=${rest:1}
      case $esc in
        '"'|'\'|'/') ch=$esc ;;
        u)
          hex=${rest:0:4}; rest=${rest:4}
          [[ $hex =~ ^[0-9a-fA-F]{4}$ ]] || return 1
          code=$((16#$hex))
          if (( code >= 55296 && code <= 56319 )); then
            [[ $rest == '\u'* ]] || return 1
            low=${rest:2:4}; rest=${rest:6}
            [[ $low =~ ^[0-9a-fA-F]{4}$ ]] || return 1
            low=$((16#$low))
            (( low >= 56320 && low <= 57343 )) || return 1
            code=$((65536 + (code-55296)*1024 + low-56320))
          elif (( code >= 56320 && code <= 57343 )); then
            return 1
          fi
          (( code >= 32 && (code < 127 || code > 159) )) || return 1
          bytes=
          if (( code < 128 )); then
            printf -v bytes '\\%03o' "$code"
          elif (( code < 2048 )); then
            printf -v bytes '\\%03o\\%03o' "$((192 + code/64))" "$((128 + code%64))"
          elif (( code < 65536 )); then
            printf -v bytes '\\%03o\\%03o\\%03o' "$((224 + code/4096))" "$((128 + code/64%64))" "$((128 + code%64))"
          else
            printf -v bytes '\\%03o\\%03o\\%03o\\%03o' "$((240 + code/262144))" "$((128 + code/4096%64))" "$((128 + code/64%64))" "$((128 + code%64))"
          fi
          printf -v ch '%b' "$bytes"
          ;;
        *) return 1 ;;
      esac
    fi
    [[ $ch == [[:cntrl:]] ]] && return 1
    REPLY=$REPLY$ch
  done
  return 0
}

# Nonnegative JSON number -> integer part, positive flag and nonzero fraction.
# Work on decimal digits, not floating-point rounding (99.999 must stay 99).
sb_json_number() {
  local n=$1 mant exp=0 digits fraction= scale count=0 sign=1
  local re='^(0|[1-9][0-9]*)(\.[0-9]+)?([eE][+-]?[0-9]+)?$'
  SB_NUMBER_INT=; SB_NUMBER_POSITIVE=0; SB_NUMBER_FRACTION=0
  [ "${#n}" -le 128 ] && [[ $n =~ $re ]] || return 1
  mant=${n%%[eE]*}
  if [ "$mant" != "$n" ]; then
    exp=${n#*[eE]}
    case $exp in -*) sign=-1; exp=${exp#-} ;; +*) exp=${exp#+} ;; esac
    while [[ $exp == 0* ]] && [ "${#exp}" -gt 1 ]; do exp=${exp#0}; done
    if [ "${#exp}" -gt 3 ]; then exp=999; fi
    exp=$((sign * 10#$exp))
  fi
  case $mant in *.*) fraction=${mant#*.} ;; esac
  digits=${mant/./}
  while [[ $digits == 0* ]] && [ "${#digits}" -gt 1 ]; do digits=${digits#0}; done
  if [ "$digits" = 0 ]; then SB_NUMBER_INT=0; return 0; fi
  SB_NUMBER_POSITIVE=1
  scale=$((exp - ${#fraction}))
  count=$((${#digits} + scale))
  if [ "$count" -le 0 ]; then
    SB_NUMBER_INT=0; SB_NUMBER_FRACTION=1
  elif [ "$count" -gt 15 ]; then
    return 1  # unusable as a percentage or a client token counter
  elif [ "$scale" -ge 0 ]; then
    SB_NUMBER_INT=$digits
    while [ "$scale" -gt 0 ]; do SB_NUMBER_INT=${SB_NUMBER_INT}0; scale=$((scale-1)); done
  else
    SB_NUMBER_INT=${digits:0:$count}
    fraction=${digits:$count}
    [[ $fraction =~ [1-9] ]] && SB_NUMBER_FRACTION=1
  fi
  return 0
}

# Skip an irrelevant subtree in chunks. Quoted braces stay inside string tokens;
# leaf containers are consumed together instead of decoding every unrelated key.
# This scans client-serialized JSON, not an untrusted-document validation API.
sb_json_skip_container() {
  local tail=$1 opening=$2 token matched depth=1 iterations=0
  local scalar='([^][{}"\\]|"([^"\\]|\\.)*")'
  local pattern='^('"$scalar"'|\{'"$scalar"'*\}|\['"$scalar"'*\])*[][{}]'
  local -a stack=()
  stack[1]=$opening
  SB_JSON_REST=
  while [ "$depth" -gt 0 ]; do
    iterations=$((iterations+1)); [ "$iterations" -le 8192 ] || return 1
    [[ $tail =~ $pattern ]] || return 1
    matched=${BASH_REMATCH[0]}; token=${matched: -1}
    tail=${tail:${#matched}}
    case $token in
      '{'|'[')
        depth=$((depth+1)); [ "$depth" -le 32 ] || return 1
        stack[$depth]=$token ;;
      '}'|']')
        if { [ "$token" = '}' ] && [ "${stack[$depth]}" = '{' ]; } ||
           { [ "$token" = ']' ] && [ "${stack[$depth]}" = '[' ]; }; then
          depth=$((depth-1))
        else return 1
        fi ;;
    esac
  done
  SB_JSON_REST=$tail
  return 0
}

# A bounded token walk over client-serialized JSON. Strings are opaque tokens;
# object/array ancestry, not text order, determines which scalar supplies a field.
# No command substitution, external parser, associative arrays or eval.
sb_status_observation() {
  SB_CONTEXT_PCT=; SB_MODEL_ID=; SB_MODEL_NAME=; SB_EFFORT=; SB_OBS_SESSION_ID=
  local rest=$1 token matched depth=0 key= path= value= type= state= valid=1 steps=0
  local context_seen=0 pct= usage_seen=0 usage_positive=0 usage_bad=0 legacy_pct= seen="|"
  local token_re='^[[:space:]]*("([^"\\]|\\.)*"|[][{}:,]|-?(0|[1-9][0-9]*)(\.[0-9]+)?([eE][+-]?[0-9]+)?|true|false|null)'
  local -a kinds=() paths=() states=() keys=()
  [ "${#rest}" -le 65536 ] || return 0
  while [ -n "$rest" ]; do
    [[ $rest =~ ^[[:space:]]*$ ]] && break
    steps=$((steps+1)); [ "$steps" -le 8192 ] || { valid=0; break; }
    [[ $rest =~ $token_re ]] || { valid=0; break; }
    token=${BASH_REMATCH[1]}; matched=${BASH_REMATCH[0]}
    rest=${rest:${#matched}}
    if [ "$depth" -eq 0 ]; then
      [ "$steps" -eq 1 ] && [ "$token" = '{' ] || { valid=0; break; }
      depth=1; kinds[1]=object; paths[1]=; states[1]=first; keys[1]=
      continue
    fi
    state=${states[$depth]}
    case $token in
      '}'|']')
        if { [ "$token" = '}' ] && [ "${kinds[$depth]}" = object ]; } ||
           { [ "$token" = ']' ] && [ "${kinds[$depth]}" = array ]; }; then
          case $state in first|comma) ;; *) valid=0; break ;; esac
          depth=$((depth-1))
          if [ "$depth" -eq 0 ]; then
            [ -z "${rest//[[:space:]]/}" ] || valid=0
            break
          fi
          continue
        fi
        valid=0; break ;;
      ':')
        [ "$state" = colon ] || { valid=0; break; }
        states[$depth]=value; continue ;;
      ',')
        [ "$state" = comma ] || { valid=0; break; }
        states[$depth]=next; continue ;;
    esac
    if [ "${kinds[$depth]}" = object ]; then
      case $state in
        first|next)
          [[ $token == \"* ]] || { valid=0; break; }
          if sb_json_text "$token"; then key=$REPLY; else key='!'; fi
          # Dots cannot be schema-key separators supplied by the input itself.
          case $key in *[!a-zA-Z0-9_]*) key='!' ;; esac
          keys[$depth]=$key; states[$depth]=colon; continue ;;
        value) ;;
        *) valid=0; break ;;
      esac
      path=${paths[$depth]}.${keys[$depth]}
    else
      case $state in first|next) ;; *) valid=0; break ;; esac
      path=${paths[$depth]}'[]'
    fi
    states[$depth]=comma
    case $path in
      .session_id|.context_window|.context_window.used_percentage|.used_percentage|.context_window.current_usage|.context_window.current_usage.input_tokens|.context_window.current_usage.cache_read_input_tokens|.context_window.current_usage.cache_creation_input_tokens|.model|.model.id|.model.display_name|.effort|.effort.level)
        # Duplicate schema members have no unambiguous meaning. Reject the whole
        # observation instead of combining state from different object values.
        case $seen in *"|$path|"*) valid=0; break ;; esac
        seen=$seen$path'|' ;;
    esac
    [ "$path" = '.context_window' ] && context_seen=1
    [ "$path" = '.context_window.current_usage' ] && usage_seen=1
    case $token in
      '{'|'[')
        case $path in
          .context_window.current_usage) [ "$token" != '{' ] && usage_bad=1 ;;
          .context_window.current_usage.input_tokens|.context_window.current_usage.cache_read_input_tokens|.context_window.current_usage.cache_creation_input_tokens) usage_bad=1 ;;
        esac
        case $path in
          .context_window|.context_window.current_usage|.model|.effort) ;;
          *)
            if sb_json_skip_container "$rest" "$token"; then
              rest=$SB_JSON_REST
              continue
            else valid=0; break
            fi ;;
        esac
        depth=$((depth+1)); [ "$depth" -le 32 ] || { valid=0; break; }
        if [ "$token" = '{' ]; then kinds[$depth]=object; else kinds[$depth]=array; fi
        paths[$depth]=$path; states[$depth]=first; keys[$depth]=
        continue ;;
    esac
    type=scalar; value=$token
    if [[ $token == \"* ]]; then
      type=string; value=
      case $path in
        .session_id|.model.id|.model.display_name|.effort.level)
          if sb_json_text "$token"; then value=$REPLY; fi ;;
      esac
    fi
    case $path in
      .session_id)
        if [ "$type" = string ] && [[ $value =~ ^[a-zA-Z0-9_-]+$ ]] && [ "${#value}" -le 128 ]; then
          SB_OBS_SESSION_ID=$value
        fi ;;
      .context_window.used_percentage|.used_percentage)
        if [ "$type" = scalar ] && sb_json_number "$value"; then
          if [ "${#SB_NUMBER_INT}" -le 3 ] && [ "$SB_NUMBER_INT" -le 100 ] &&
             { [ "$SB_NUMBER_INT" -ne 100 ] || [ "$SB_NUMBER_FRACTION" = 0 ]; }; then
            if [ "$path" = '.used_percentage' ]; then legacy_pct=$SB_NUMBER_INT; else pct=$SB_NUMBER_INT; fi
          fi
        fi ;;
      .context_window.current_usage) usage_bad=1 ;;
      .context_window.current_usage.input_tokens|.context_window.current_usage.cache_read_input_tokens|.context_window.current_usage.cache_creation_input_tokens)
        if [ "$type" = scalar ] && sb_json_number "$value" && [ "$SB_NUMBER_FRACTION" = 0 ]; then
          [ "$SB_NUMBER_POSITIVE" = 1 ] && usage_positive=1
        else usage_bad=1
        fi ;;
      .model.id) [ "$type" = string ] && SB_MODEL_ID=$value ;;
      .model.display_name) [ "$type" = string ] && SB_MODEL_NAME=$value ;;
      .effort.level)
        if [ "$type" = string ]; then
          case $value in low|medium|high|xhigh|max) SB_EFFORT=$value ;; esac
        fi ;;
    esac
  done
  if [ "$valid" != 1 ] || [ "$depth" -ne 0 ] || [ "$steps" -eq 0 ]; then
    SB_MODEL_ID=; SB_MODEL_NAME=; SB_EFFORT=; SB_OBS_SESSION_ID=
    return 0
  fi
  # Legacy clients may supply a root percentage. A present context_window always
  # owns the result, even when null; never fall through to a different namespace.
  [ "$context_seen" = 0 ] && pct=$legacy_pct
  if [ "$usage_seen" = 1 ] && { [ "$usage_bad" = 1 ] || [ "$usage_positive" = 0 ]; }; then
    pct=
  fi
  SB_CONTEXT_PCT=$pct
  return 0
}

# Read one complete observation. A truncated write, old timestamp, clock rollback
# or disagreement with the legacy numeric file is unknown, not a threshold event.
# The 60s TTL covers several 10s status refreshes without keeping old data forever.
sb_context_read() {
  SB_CONTEXT_PCT=
  local sid=$1 now=${SB_NOW:-${EPOCHSECONDS:-}} pct= ts= extra= legacy= file=
  [[ $sid =~ ^[a-zA-Z0-9_-]+$ ]] && [ "${#sid}" -le 128 ] || return 0
  [ -n "$now" ] || now=$(date +%s)
  file="${SB_TMP:-${TMPDIR:-${TEMP:-/tmp}}}/brain-context-state-${sid}"
  [ -r "$file" ] || return 0
  {
    IFS=$'\t' read -r pct ts extra || return 0
    local trailing=
    if IFS= read -r trailing || [ -n "$trailing" ]; then return 0; fi
  } < "$file"
  [[ $pct =~ ^(0|[1-9][0-9]?|100)$ ]] || return 0
  [[ $ts =~ ^[1-9][0-9]{0,11}$ ]] && [[ $now =~ ^[1-9][0-9]{0,11}$ ]] || return 0
  [ -z "$extra" ] || return 0
  (( now >= ts && now - ts <= 60 )) || return 0
  file="${SB_TMP:-${TMPDIR:-${TEMP:-/tmp}}}/brain-context-pct-${sid}"
  [ -r "$file" ] || return 0
  {
    IFS= read -r legacy || return 0
    local trailing=
    if IFS= read -r trailing || [ -n "$trailing" ]; then return 0; fi
  } < "$file"
  [ "$legacy" = "$pct" ] || return 0
  SB_CONTEXT_PCT=$pct
  return 0
}
