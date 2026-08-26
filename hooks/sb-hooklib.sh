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
