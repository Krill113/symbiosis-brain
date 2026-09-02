#!/bin/bash
# Tests for the scope resolver in hooks/sb-hooklib.sh.
#
# The resolver exists because the SessionStart hook cannot hand its computed scope
# to the other hooks: CLAUDE_ENV_FILE is a Bash-tool preamble, not a hook environment.
# Every consumer therefore resolves the scope itself, and must never substitute a
# plausible-looking wrong value when it cannot.
set -e

HOOKS="$(cd "$(dirname "$0")/.." && pwd)/hooks"
. "$HOOKS/sb-hooklib.sh"

# The env tier must not leak in from the shell that runs the suite: this very
# variable is set for Bash-tool commands and would silently answer for tier 4.
unset SYMBIOSIS_BRAIN_SCOPE

WORK="$(mktemp -d)"
export TMPDIR="$WORK" TEMP="$WORK"
trap 'rm -rf "$WORK"' EXIT

pass=0
fail=0
t() {
  if [ "$2" = "PASS" ]; then echo "✓ $1"; pass=$((pass+1)); else echo "✗ $1 (got: '${3-}')"; fail=$((fail+1)); fi
}
eq() { # name expected actual
  if [ "$2" = "$3" ]; then t "$1" PASS; else t "$1" FAIL "$3"; fi
}

marker() { printf '<!-- symbiosis-brain v1: %s -->\n' "$1"; }

# ── marker body parsing ───────────────────────────────────────────────────────
sb_marker_scope "$(marker 'scope=alpha-net')"
eq "plain marker" "alpha-net" "$SB_SCOPE"

sb_marker_scope '<!-- symbiosis-brain v1: scope=alpha-net-->'
eq "no space before the closing delimiter" "alpha-net" "$SB_SCOPE"

sb_marker_scope '<!-- symbiosis-brain v1:scope=alpha-net -->'
eq "no space after the version colon" "alpha-net" "$SB_SCOPE"

sb_marker_scope "$(marker 'umbrella=alpha, scope=alpha-net')"
eq "umbrella key before scope" "alpha-net" "$SB_SCOPE"

sb_marker_scope "$(marker 'scope=alpha-net, status=draft')"
eq "trailing status key" "alpha-net" "$SB_SCOPE"

sb_marker_scope "$(printf 'x\n%s\ntext\n%s\n' "$(marker 'scope=first-one')" "$(marker 'scope=last-one')")"
eq "last marker wins" "last-one" "$SB_SCOPE"

# A malformed LAST marker must not silently fall back to an earlier valid one:
# a stale scope is exactly the plausible-wrong-value this resolver exists to avoid.
sb_marker_scope "$(printf '%s\n%s\n' "$(marker 'scope=first-one')" "$(marker 'umbrella=alpha')")"
eq "corrupt last marker does not reuse an earlier one" "" "$SB_SCOPE"

sb_marker_scope 'just some text, no marker at all'
eq "no marker" "" "$SB_SCOPE"

sb_marker_scope "$(marker 'scope=with spaces')"
eq "invalid scope value rejected" "" "$SB_SCOPE"

# ── directory walk ────────────────────────────────────────────────────────────
mkdir -p "$WORK/proj/sub/deep" "$WORK/nomarker/sub" "$WORK/bare/sub"
marker 'scope=alpha-net' > "$WORK/proj/CLAUDE.md"
printf '# no marker here\n' > "$WORK/nomarker/CLAUDE.md"

sb_scope_from_dir "$WORK/proj"
eq "marker in the directory itself" "alpha-net" "$SB_SCOPE"

sb_scope_from_dir "$WORK/proj/sub/deep"
eq "marker two levels up" "alpha-net" "$SB_SCOPE"

# A CLAUDE.md without a marker means "this project declares no scope" — the walk
# stops there instead of inheriting an unrelated ancestor's scope.
sb_scope_from_dir "$WORK/nomarker/sub"
eq "markerless CLAUDE.md stops the walk" "" "$SB_SCOPE"

sb_scope_from_dir "$WORK/bare/sub"
eq "no CLAUDE.md anywhere" "" "$SB_SCOPE"

sb_scope_from_dir ""
eq "empty directory argument" "" "$SB_SCOPE"

# Plain POSIX paths must survive separator normalisation untouched.
sb_scope_from_dir "$WORK/proj/sub"
eq "posix path is not mangled" "alpha-net" "$SB_SCOPE"

# A payload path may arrive with Windows separators (JSON-escaped or bare).
sb_scope_from_dir "${WORK//\//\\}\\proj\\sub"
eq "backslash separators are normalised" "alpha-net" "$SB_SCOPE"

# Depth cap: a marker further up than the cap is not found.
deep="$WORK/cap"; mkdir -p "$deep"
marker 'scope=too-far' > "$deep/CLAUDE.md"
for i in 1 2 3 4 5 6 7 8 9 10 11 12; do deep="$deep/d$i"; done
mkdir -p "$deep"
sb_scope_from_dir "$deep"
eq "walk stops at the depth cap" "" "$SB_SCOPE"

# ── taxonomy gating for inherited (ancestor) markers ──────────────────────────
VAULT_FIX="$WORK/vault"
mkdir -p "$VAULT_FIX/reference"
cat > "$VAULT_FIX/reference/scope-taxonomy.md" <<'TAX'
| Scope | Тип | Что хранит |
|---|---|---|
| `global` | cross-project | patterns |
| `alpha-net` | product | product notes |
| `alpha` | **зонтик** (cross-ecosystem only) | umbrella notes |
| `alpha-part` | umbrella-member (alpha) | member notes |
| `wsroot` | workspace-root | container folder |
TAX
export SYMBIOSIS_BRAIN_VAULT="$VAULT_FIX"

sb_scope_registered alpha-net && r=yes || r=no
eq "registered scope recognised" "yes" "$r"
sb_scope_registered nope-net && r=yes || r=no
eq "unregistered scope rejected" "no" "$r"

sb_scope_is_broad wsroot && r=yes || r=no
eq "workspace-root is broad" "yes" "$r"
sb_scope_is_broad alpha && r=yes || r=no
eq "umbrella is broad" "yes" "$r"
sb_scope_is_broad alpha-part && r=yes || r=no
eq "umbrella-member is not broad" "no" "$r"
sb_scope_is_broad alpha-net && r=yes || r=no
eq "product scope is not broad" "no" "$r"

# An ancestor whose marker names a workspace-root/umbrella scope must not be
# inherited by a project that declares nothing of its own.
mkdir -p "$WORK/ws/proj/sub"
marker 'scope=wsroot' > "$WORK/ws/CLAUDE.md"
sb_resolve_scope "{\"cwd\":\"$WORK/ws/proj/sub\"}" ""
eq "inherited workspace-root marker is refused" "" "$SB_SCOPE"

# The same marker in the directory the session actually sits in is honoured.
sb_resolve_scope "{\"cwd\":\"$WORK/ws\"}" ""
eq "own workspace-root marker is honoured" "wsroot" "$SB_SCOPE"

# Fail-open: with no taxonomy to consult, an inherited marker is kept.
SYMBIOSIS_BRAIN_VAULT="$WORK/absent" sb_resolve_scope "{\"cwd\":\"$WORK/ws/proj/sub\"}" ""
eq "no taxonomy: inherited marker kept" "wsroot" "$SB_SCOPE"

# ── the ladder ────────────────────────────────────────────────────────────────
unset SYMBIOSIS_BRAIN_SCOPE
sb_resolve_scope "{\"cwd\":\"$WORK/proj/sub\"}" "sid-1"
eq "tier 1: cwd marker" "alpha-net" "$SB_SCOPE"
eq "tier 1 source" "cwd" "$SB_SCOPE_SRC"

printf 'from-session\n' > "$WORK/brain-scope-sid-1"
sb_resolve_scope "{\"cwd\":\"$WORK/bare/sub\"}" "sid-1"
eq "tier 2: session marker file" "from-session" "$SB_SCOPE"
eq "tier 2 source" "session" "$SB_SCOPE_SRC"

: > "$WORK/brain-scope-sid-2"
SYMBIOSIS_BRAIN_SCOPE=from-env sb_resolve_scope "{\"cwd\":\"$WORK/bare/sub\"}" "sid-2"
eq "tier 3: env var when the session file is empty" "from-env" "$SB_SCOPE"
eq "tier 3 source" "env" "$SB_SCOPE_SRC"

sb_resolve_scope "{\"cwd\":\"$WORK/bare/sub\"}" "sid-missing"
eq "tier 4: unresolved" "" "$SB_SCOPE"
eq "tier 4 source" "none" "$SB_SCOPE_SRC"

sb_resolve_scope "" ""
eq "empty payload resolves to nothing" "" "$SB_SCOPE"

# The cwd tier wins over a stale session marker from an earlier directory.
printf 'stale-scope\n' > "$WORK/brain-scope-sid-3"
sb_resolve_scope "{\"cwd\":\"$WORK/proj\"}" "sid-3"
eq "cwd beats a stale session marker" "alpha-net" "$SB_SCOPE"

echo ""
echo "Results: $pass passed, $fail failed"
[ "$fail" -eq 0 ] || exit 1
