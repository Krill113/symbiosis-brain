#!/bin/bash
# Symbiosis Brain statusline wrapper.
# 1. Run pre-existing user statusline (if any) OR our base — its output is row 1.
# 2. Append our SB-line as row 2 (Claude Code renders each `echo`/`print` as a separate row,
#    per https://code.claude.com/docs/en/statusline#display-multiple-lines).
#
# Performance contract: Claude Code debounces status line updates at 300ms and
# cancels the in-flight script when a new event arrives. On Windows/MSYS a child
# caught mid-fork by that cancel is stranded as a suspended orphan process, so
# this wrapper avoids forks entirely — stdin is read with a builtin, the path is
# derived with parameter expansion, and our own scripts are `source`d in-process
# instead of being piped into a fresh bash (~85ms saved per avoided process).
# A user-supplied command still runs as its own process: it is arbitrary code and
# must not be able to clobber our shell state or abort row 2 with `exit`.

IFS= read -r -d '' INPUT
DIR=${BASH_SOURCE[0]%/*}
[ "$DIR" = "${BASH_SOURCE[0]}" ] && DIR=.

export SB_STATUSLINE_INPUT="$INPUT"

if [ -n "$SYMBIOSIS_BRAIN_USER_STATUSLINE_CMD" ]; then
  eval "$SYMBIOSIS_BRAIN_USER_STATUSLINE_CMD" <<<"$INPUT" || true
elif [ -r "$DIR/sb-base-statusline.sh" ]; then
  source "$DIR/sb-base-statusline.sh" || true
fi

source "$DIR/sb-line.sh"
