#!/bin/bash
# Tests for hooks/brain-sync.sh — pull --rebase, conflict abort, alarm marker.
#
# The hook used to be push-only: a vault touched from a second machine diverged
# silently forever (lens A §A3). It now rebases first, aborts on any conflict it
# cannot resolve mechanically, and leaves a one-line alarm marker that SessionStart
# and the status line surface. It must still never block: always exit 0.
set -u

HOOKS="$(cd "$(dirname "$0")/.." && pwd)/hooks"
SYNC="$HOOKS/brain-sync.sh"

WORK="$(mktemp -d)"
export TMPDIR="$WORK/tmp" TEMP="$WORK/tmp"
mkdir -p "$TMPDIR"
MARKER="$TMPDIR/brain-sync-failed"
ERRLOG="$TMPDIR/brain-sync-errors.log"

FAILED=0
t() {
  if [ "$2" = "PASS" ]; then echo "PASS: $1"; else echo "FAIL: $1"; FAILED=$((FAILED + 1)); fi
}

git_id() {  # $1 = repo
  git -C "$1" config user.email "test@example.com"
  git -C "$1" config user.name "Test"
  git -C "$1" config commit.gpgsign false
}

# $1 = pair name, $2 = "union" to seed .gitattributes with `log.md merge=union`
setup_pair() {
  local name="$1" union="${2:-}"
  git init -q --bare "$WORK/$name.git"
  git -C "$WORK/$name.git" symbolic-ref HEAD refs/heads/main
  git clone -q "$WORK/$name.git" "$WORK/$name-seed" 2>/dev/null
  git_id "$WORK/$name-seed"
  printf 'l1\n' > "$WORK/$name-seed/log.md"
  [ "$union" = "union" ] && printf 'log.md merge=union\n' > "$WORK/$name-seed/.gitattributes"
  git -C "$WORK/$name-seed" add -A
  git -C "$WORK/$name-seed" commit -q -m init
  git -C "$WORK/$name-seed" push -q -u origin main
  git clone -q "$WORK/$name.git" "$WORK/$name-A" 2>/dev/null
  git clone -q "$WORK/$name.git" "$WORK/$name-B" 2>/dev/null
  git_id "$WORK/$name-A"
  git_id "$WORK/$name-B"
}

push_from_b() {  # $1 = pair, $2 = file, $3 = content
  printf '%s\n' "$3" > "$WORK/$1-B/$2"
  git -C "$WORK/$1-B" add -A
  git -C "$WORK/$1-B" commit -q -m "b: $2"
  git -C "$WORK/$1-B" push -q
}

run_sync() {  # $1 = vault dir, $2 = mode (default auto) -> RC
  SYMBIOSIS_BRAIN_VAULT="$1" bash "$SYNC" "${2:-auto}" >/dev/null 2>&1
  RC=$?
}

# ── (1) clean push ────────────────────────────────────────────────────────────
setup_pair p1
rm -f "$MARKER"
printf 'note A\n' > "$WORK/p1-A/a.md"
run_sync "$WORK/p1-A"
remote_files=$(git -C "$WORK/p1.git" ls-tree -r --name-only main)
if [ "$RC" = "0" ] && [ ! -f "$MARKER" ] && [[ "$remote_files" == *"a.md"* ]]; then
  t "clean push commits and pushes the vault" PASS
else
  t "clean push commits and pushes the vault (rc=$RC)" FAIL
fi

# ── (2) remote moved ahead -> rebase, then push ───────────────────────────────
setup_pair p2
rm -f "$MARKER"
push_from_b p2 b.md "note B"
printf 'note A2\n' > "$WORK/p2-A/a2.md"
run_sync "$WORK/p2-A"
remote_files=$(git -C "$WORK/p2.git" ls-tree -r --name-only main)
if [ "$RC" = "0" ] && [ ! -f "$MARKER" ] &&
   [[ "$remote_files" == *"a2.md"* ]] && [[ "$remote_files" == *"b.md"* ]]; then
  t "remote ahead -> rebase and push, no alarm" PASS
else
  t "remote ahead -> rebase and push, no alarm (rc=$RC)" FAIL
fi

# ── (3) real conflict -> abort, alarm, clean tree, still exit 0 ───────────────
setup_pair p3
rm -f "$MARKER" "$ERRLOG"
push_from_b p3 log.md "l1 from B"
printf 'l1 from A\n' > "$WORK/p3-A/log.md"
run_sync "$WORK/p3-A"
ok=1
[ "$RC" = "0" ] || { ok=0; echo "  rc=$RC (must be 0)"; }
grep -qE '^stage=conflict at=' "$MARKER" 2>/dev/null || { ok=0; echo "  marker missing or malformed"; }
[ "$(wc -l < "$MARKER" 2>/dev/null)" = "1" ] || { ok=0; echo "  marker must be exactly one line"; }
grep -q 'stage=conflict' "$ERRLOG" 2>/dev/null || { ok=0; echo "  errors log has no entry"; }
[ -d "$(git -C "$WORK/p3-A" rev-parse --git-path rebase-merge 2>/dev/null)" ] && { ok=0; echo "  rebase left in progress"; }
grep -q '<<<<<<<' "$WORK/p3-A/log.md" 2>/dev/null && { ok=0; echo "  conflict markers left in log.md"; }
[ -z "$(git -C "$WORK/p3-A" status --porcelain)" ] || { ok=0; echo "  working tree dirty after abort"; }
if [ "$ok" = "1" ]; then t "conflict -> abort + alarm marker, tree clean, rc 0" PASS; else t "conflict -> abort + alarm marker, tree clean, rc 0" FAIL; fi

# ── (4) a later successful sync clears the alarm ──────────────────────────────
git -C "$WORK/p3-A" reset -q --hard origin/main
printf 'note A3\n' > "$WORK/p3-A/a3.md"
run_sync "$WORK/p3-A"
if [ "$RC" = "0" ] && [ ! -f "$MARKER" ]; then
  t "successful sync clears the alarm marker" PASS
else
  t "successful sync clears the alarm marker (rc=$RC)" FAIL
fi

# ── (5) log.md merge=union resolves the same conflict without asking ──────────
setup_pair p5 union
rm -f "$MARKER"
push_from_b p5 log.md "l1
from B"
printf 'l1\nfrom A\n' > "$WORK/p5-A/log.md"
run_sync "$WORK/p5-A"
merged=$(git -C "$WORK/p5-A" show HEAD:log.md 2>/dev/null)
if [ "$RC" = "0" ] && [ ! -f "$MARKER" ] &&
   [[ "$merged" == *"from A"* ]] && [[ "$merged" == *"from B"* ]]; then
  t "log.md merge=union keeps both sides, no alarm" PASS
else
  t "log.md merge=union keeps both sides, no alarm (rc=$RC)" FAIL
fi

# ── (6) not a git vault -> silent no-op ───────────────────────────────────────
rm -f "$MARKER"
mkdir -p "$WORK/plain"
run_sync "$WORK/plain"
if [ "$RC" = "0" ] && [ ! -f "$MARKER" ]; then
  t "non-git vault is a silent no-op" PASS
else
  t "non-git vault is a silent no-op (rc=$RC)" FAIL
fi

# ── (7) lock already held -> second run does nothing, quietly ─────────────────
# SessionEnd has no matcher in _hooks_block, so it fires in EVERY window. Two
# windows closing together must not race inside the vault: `git rebase --abort`
# from the second process would kill the first process's in-flight rebase.
setup_pair p7
rm -f "$MARKER"
mkdir -p "$TMPDIR/brain-sync.lock"          # pretend another window holds the lock
printf 'note A7\n' > "$WORK/p7-A/a7.md"
head_before=$(git -C "$WORK/p7-A" rev-parse HEAD)
run_sync "$WORK/p7-A"
if [ "$RC" = "0" ] && [ ! -f "$MARKER" ] && \
   [ -n "$(git -C "$WORK/p7-A" status --porcelain)" ] && \
   [ "$head_before" = "$(git -C "$WORK/p7-A" rev-parse HEAD)" ]; then
  t "second run with the lock held is a quiet no-op" PASS
else
  t "second run with the lock held is a quiet no-op (rc=$RC)" FAIL
fi
rmdir "$TMPDIR/brain-sync.lock"

# ── (8) commit refused + remote moved ahead -> clean tree, alarm, still exit 0 ─
# The --autostash pop conflicts only when the commit at the top did NOT run. Force
# that with a refusing pre-commit hook: `git commit` fails, the changes stay staged,
# --autostash takes them, the rebase fast-forwards over a conflicting remote line,
# and the pop leaves `UU`. (An empty identity was tried first and rejected: `git
# stash` needs an author too, so --autostash itself fails with "Cannot autostash"
# before ever reaching a pull — measured on Git for Windows 2026-08-26.) Note `git
# pull` still exits 0 here — the fix relies on the separate `git ls-files
# --unmerged` check, not on the pull's exit code.
rm -f "$MARKER"
setup_pair p8
mkdir -p "$WORK/p8-A/.git/hooks"
printf '#!/bin/sh\nexit 1\n' > "$WORK/p8-A/.git/hooks/pre-commit"
chmod +x "$WORK/p8-A/.git/hooks/pre-commit"
push_from_b p8 log.md "l1
remote line"
printf 'l1\nlocal line\n' > "$WORK/p8-A/log.md"
run_sync "$WORK/p8-A"
if [ "$RC" = "0" ] && [ -f "$MARKER" ] && \
   grep -q '^stage=conflict' "$MARKER" && \
   [ -z "$(git -C "$WORK/p8-A" status --porcelain)" ] && \
   [ -n "$(git -C "$WORK/p8-A" stash list)" ]; then
  t "failed commit + diverged remote leaves a clean tree, an alarm and the stash" PASS
else
  t "failed commit + diverged remote leaves a clean tree, an alarm and the stash (rc=$RC)" FAIL
fi

rm -rf "$WORK"

echo ""
if [ "$FAILED" -eq 0 ]; then
  echo "All tests PASSED"
  exit 0
else
  echo "$FAILED test(s) FAILED"
  exit 1
fi
