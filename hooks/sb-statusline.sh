#!/bin/bash
# Symbiosis Brain statusline wrapper.
# 1. Run pre-existing user statusline (if any) OR our base — its output is row 1.
# 2. Append our SB-line as row 2 (Claude Code renders each `echo`/`print` as a separate row,
#    per https://code.claude.com/docs/en/statusline#display-multiple-lines).
INPUT=$(cat)
DIR="$(cd "$(dirname "$0")" && pwd)"

if [ -n "$SYMBIOSIS_BRAIN_USER_STATUSLINE_CMD" ]; then
  echo "$INPUT" | eval "$SYMBIOSIS_BRAIN_USER_STATUSLINE_CMD" || true
else
  if [ -x "$DIR/sb-base-statusline.sh" ]; then
    echo "$INPUT" | bash "$DIR/sb-base-statusline.sh" || true
  fi
fi

echo "$INPUT" | bash "$DIR/sb-line.sh"
