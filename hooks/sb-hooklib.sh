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
