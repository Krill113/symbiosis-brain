"""Action-rules compiler (Stage 1 action-recall).

Compiles ``class:"action"`` routes from the merged tool-routing catalog
(default ∪ vault-local override, see ``tool_routing.py``) into a flat TSV
consumed by the pure-bash PreToolUse matcher in
``hooks/brain-pre-action-trigger.sh``. That hook injects a warning at the
MOMENT a risky Bash/PowerShell command is about to run — before it runs —
without paying the uv/python cold-start cost.

Why ``grep -E`` and not Python's ``re``: these rules are POSIX ERE (they use
bracket expressions like ``[[:space:]]``). Python's ``re`` module SILENTLY
misparses ``[[:space:]]`` as a literal character class (``[``, ``:``, ``s``,
``p``, ``a``, ``c``, ``e``, ``]``) instead of "whitespace" — it compiles
without error but matches the wrong thing. So every regex is validated by
shelling out to the SAME ``grep -E`` the bash hook will run it through, using
each route's own ``test_match``/``test_nomatch`` vectors as the oracle.

Fail-open everywhere: a rule with no test_match vectors for a given tool side
(unvalidated — see below), a rule whose regex contains a tab/newline/CR
(would corrupt the TSV), an id outside ``[A-Za-z0-9._-]``, or grep being
unavailable on PATH — all skip that rule (or all rules) and record why in
``meta.json``. Nothing here raises out of ``compile_action_rules``.

A tool side (bash/powershell) with an empty or missing ``test_match`` list is
dropped rather than compiled: an unvalidated pattern that happens to be
overbroad would fire on unrelated commands, and because the hook exits on
the first hit, would silently suppress every other rule (and the normal
python recall) behind it.

Validation is RULE-level, not pattern-level. Every pattern of one tool side
goes into ONE temp ``.re`` file and the side is validated as their union —
exactly the semantics ``grep -Ef`` already gives at runtime, where the hook
fires when ANY row of the rule matches. Per-pattern validation used to demand
that EACH regex match EVERY vector, so a rule written as two honest triggers
was dropped and the only way to ship it was a hand-fused alternation with the
command anchor copy-pasted into each branch. A pattern that catches none of
its side's ``test_match`` vectors is reported in ``meta.json`` under
``unmatched_patterns`` — a warning, not a drop. Dropping stays reserved for
real failures: a ``test_match`` vector the union misses, a ``test_nomatch``
vector the union hits, missing vectors, and structural errors (which stay
per-pattern: a tab/newline/CR corrupts the TSV regardless of its siblings).
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import symbiosis_brain.tool_routing as tr
from symbiosis_brain.atomic_write import atomic_write_text
from symbiosis_brain.pre_action_config import _debug_log, routing_local_path

TSV_BASENAME = "action-rules.tsv"
META_BASENAME = "action-rules.meta.json"
# Per-toolkey fast-reject pattern file consumed by the pure-bash hook: all of
# that tool's patterns combined, for a single `grep -qEf` existence check
# before falling back to the row-by-row TSV loop (see compile_action_rules).
RE_BASENAME_FMT = "action-rules.{tool}.re"
_TOOL_KEYS = ("bash", "powershell")
_GREP_TIMEOUT_SECONDS = 5
# Route ids land unescaped in the hook's JSON stdout (only the hint is JSON-
# escaped) and also become filesystem-adjacent tokens (jsonl fields) — keep
# them to a safe, portable character set instead of escaping at emit time.
_ID_RE = re.compile(r"^[A-Za-z0-9._-]+$")


def _find_grep(which=None, environ=None, windows=None) -> Optional[str]:
    """Locate a ``grep`` executable. Tries PATH directly first, then derives
    a Git-for-Windows location from whatever IS on PATH.

    On Windows only ``git`` is reliably on PATH (``Git\\cmd``); ``bash`` and
    ``grep`` live under ``Git\\bin`` / ``Git\\usr\\bin`` which many
    environments — notably the MCP server process and PowerShell-launched
    python — never see. Deriving from ``git`` (and the standard install
    roots) is what keeps validation working there; without it every rule is
    "unvalidated" and the compile would produce nothing.

    Parameters are injectable for tests; ``None`` means the real thing.
    """
    import os

    if which is None:
        which = shutil.which
    if environ is None:
        environ = os.environ
    if windows is None:
        windows = os.name == "nt"

    found = which("grep")
    if found:
        return found
    if not windows:
        return None

    roots: list[Path] = []
    for exe in ("bash", "git"):
        hit = which(exe) or which(exe + ".exe")
        if hit:
            p = Path(hit).resolve()
            roots.append(p.parent)          # Git\bin  (bash) / Git\cmd (git)
            roots.append(p.parent.parent)   # Git\
    for var, sub in (
        ("ProgramFiles", ("Git",)),
        ("ProgramFiles(x86)", ("Git",)),
        ("LocalAppData", ("Programs", "Git")),
    ):
        base = environ.get(var)
        if base:
            roots.append(Path(base, *sub))

    seen: set[Path] = set()
    for root in roots:
        if root in seen:
            continue
        seen.add(root)
        for cand in (
            root / "grep.exe",
            root / "usr" / "bin" / "grep.exe",
            root / "bin" / "grep.exe",
            root / "grep",
            root / "usr" / "bin" / "grep",
        ):
            if cand.exists():
                return str(cand)
    return None


def _grep_matches_file(grep_path: str, pattern_file: str, command: str) -> Optional[bool]:
    """True if `command` matches the (single-line ERE) pattern stored in
    `pattern_file` under grep -E, False if not, None on any invocation error
    (bad regex, grep crash, timeout — all treated as "cannot validate").

    The pattern is read from a FILE (`-f`), never passed on argv (`-e`): on
    Windows, an MSYS-linked grep.exe re-tokenizes/path-translates argv it
    receives from a non-MSYS parent (e.g. a Python subprocess), so a pattern
    containing `/` sequences like `(^|[;&|]+).*(/|//)+` can silently stop
    matching what the SAME pattern matches when a shell hands it to grep
    directly. Feeding it through a file sidesteps that translation for both
    platforms and is otherwise semantically identical."""
    try:
        proc = subprocess.run(
            [grep_path, "-qEf", pattern_file],
            input=command.encode("utf-8", errors="replace"),
            capture_output=True,
            timeout=_GREP_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode == 0:
        return True
    if proc.returncode == 1:
        return False
    return None  # 2+ = grep-side error (e.g. malformed regex)


def _validate_side(
    grep_path: str, patterns: list[str], matches: list[str], nomatches: list[str]
) -> tuple[bool, str]:
    """Validate ONE tool side of a rule as the UNION of its patterns.

    All patterns go into a single temp ``.re`` file (one per line) and are fed
    to the SAME ``grep -Ef`` the hook runs, so "the side matched" means exactly
    what it means at runtime: any of the rule's patterns matched. Contract:
    every ``test_match`` vector must be caught by the union, no ``test_nomatch``
    vector may be. Returns ``(ok, reason)``.

    Replaces the old ``_validate_pattern``, which demanded that EACH pattern
    match EVERY vector. Side effect: grep forks for a side with N patterns and
    M+K vectors drop from ``N*(M+K)`` to ``M+K``.
    """
    import os
    import tempfile

    fd, tmp_name = tempfile.mkstemp(suffix=".re")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write("".join(p + "\n" for p in patterns))
        for cmd in matches:
            result = _grep_matches_file(grep_path, tmp_name, cmd)
            if result is None:
                return False, f"grep invocation error validating test_match {cmd!r}"
            if result is False:
                return False, f"test_match did not match: {cmd!r}"
        for cmd in nomatches:
            result = _grep_matches_file(grep_path, tmp_name, cmd)
            if result is None:
                return False, f"grep invocation error validating test_nomatch {cmd!r}"
            if result is True:
                return False, f"test_nomatch unexpectedly matched: {cmd!r}"
    except OSError as e:
        return False, f"failed to write pattern temp file: {e}"
    finally:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
    return True, ""


def _dead_patterns(
    grep_path: str, patterns: list[str], matches: list[str]
) -> list[str]:
    """Patterns of a side that caught NO ``test_match`` vector at all.

    Called ONLY after ``_validate_side`` succeeded, so the union is known good
    and this is pure diagnostics: without it, union validation would hide a
    typo in the second trigger behind a working first one — the rule compiles
    while half its patterns are dead.

    A single-pattern side is skipped outright: it passed the union check
    against a non-empty vector list, so it cannot be dead, and skipping keeps
    the common case (every live rule today) at zero extra forks.

    Never raises. A grep invocation error counts as "caught it" — we do not
    warn about a pattern we could not test.
    """
    if len(patterns) < 2:
        return []
    import os
    import tempfile

    dead: list[str] = []
    for pattern in patterns:
        fd, tmp_name = tempfile.mkstemp(suffix=".re")
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
                f.write(pattern + "\n")
            caught = False
            for cmd in matches:
                if _grep_matches_file(grep_path, tmp_name, cmd) is not False:
                    caught = True
                    break
            if not caught:
                dead.append(pattern)
        except OSError:
            pass  # cannot test this pattern -> do not warn about it
        finally:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
    return dead


def _escape_hint(hint: str) -> str:
    """Flatten to one line and JSON-escape, ready to paste straight inside
    a JSON string literal (the bash hook does no further processing)."""
    flat = " ".join(str(hint).split("\n"))
    flat = flat.replace("\r", " ")
    # json.dumps handles \, ", and all control chars (including any stray
    # tab) — strip the surrounding quotes it adds to get just the escaped
    # inner content for the TSV cell.
    return json.dumps(flat, ensure_ascii=False)[1:-1]


def _load_merged_raw(vault: Path) -> list[dict]:
    default = tr._read_json(tr._DEFAULT_JSON)
    local_path = routing_local_path(vault)
    local = tr._read_json(local_path) if local_path.exists() else None
    return tr._merge_raw(default, local)


def _compile_one_rule(
    raw: dict[str, Any], grep_path: Optional[str]
) -> tuple[list[tuple[str, str, str]], Optional[str], list[dict[str, str]]]:
    """Returns (rows, skip_reason, unmatched).

    ``rows`` is a list of (toolkey, regex, id) for each tool side that
    validated; ``skip_reason`` is set (rows empty) when the whole rule is
    dropped; ``unmatched`` lists ``{"id","tool","re"}`` for every pattern that
    caught none of its side's ``test_match`` vectors — a warning surfaced in
    ``meta.json``, never a reason to drop."""
    rid = raw.get("id")
    if not rid or not isinstance(rid, str):
        return [], "missing or invalid id", []
    if not _ID_RE.match(rid):
        return [], "id contains characters outside [A-Za-z0-9._-]", []
    cmd_triggers = raw.get("command_triggers")
    if not isinstance(cmd_triggers, dict):
        return [], "no command_triggers", []
    test_match = raw.get("test_match") or {}
    test_nomatch = raw.get("test_nomatch") or {}
    if not isinstance(test_match, dict):
        test_match = {}
    if not isinstance(test_nomatch, dict):
        test_nomatch = {}

    if grep_path is None:
        return [], "grep unavailable on PATH — cannot validate ERE rules", []

    rows: list[tuple[str, str, str]] = []
    unmatched: list[dict[str, str]] = []
    for toolkey in _TOOL_KEYS:
        trigs = cmd_triggers.get(toolkey)
        if not trigs:
            continue
        # Structural checks stay PER PATTERN and still drop the whole rule: a
        # tab/newline/CR corrupts the TSV no matter what its siblings match,
        # and a trigger without 're' is a malformed catalog entry. They run
        # BEFORE any grep so the reported reason names the real defect.
        patterns: list[str] = []
        for trig in trigs:
            pattern = trig.get("re") if isinstance(trig, dict) else None
            if not pattern or not isinstance(pattern, str):
                return [], f"{toolkey}: trigger missing 're'", []
            if any(c in pattern for c in ("\t", "\n", "\r")):
                return [], f"{toolkey}: regex contains a tab/newline/CR character", []
            patterns.append(pattern)
        if not patterns:
            continue
        match_vecs = list(test_match.get(toolkey) or [])
        if not match_vecs:
            # Docstring promise: a rule with no test vectors is unvalidated
            # and MUST be skipped — an empty test_match would otherwise let
            # `_validate_side`'s empty loop vacuously "pass" and compile an
            # unreviewed (possibly overbroad) pattern straight into the hook's
            # grep -f, silently swallowing every hit below it (the hook exits
            # after the first match).
            return [], f"{toolkey}: no test_match vectors — rule not validated", []
        ok, reason = _validate_side(
            grep_path,
            patterns,
            match_vecs,
            list(test_nomatch.get(toolkey) or []),
        )
        if not ok:
            return [], f"{toolkey}: {reason}", []
        for pattern in _dead_patterns(grep_path, patterns, match_vecs):
            unmatched.append({"id": rid, "tool": toolkey, "re": pattern})
        for pattern in patterns:
            rows.append((toolkey, pattern, rid))
    if not rows:
        return [], "command_triggers has no bash/powershell entries", []
    return rows, None, unmatched


def compile_action_rules(vault: Path) -> Path:
    """Compile class:"action" routes (default ∪ vault-local) into
    <vault>/.index/action-rules.tsv, writing a sibling meta.json summary.
    Always returns the TSV path — never raises."""
    vault = Path(vault)
    index_dir = vault / ".index"
    tsv_path = index_dir / TSV_BASENAME
    meta_path = index_dir / META_BASENAME

    rules_total = 0
    skipped: list[dict[str, str]] = []
    # Dead patterns across all rules: {"id","tool","re"}. Always emitted (empty
    # list when nothing is dead) so a reader never has to tell "no dead
    # patterns" apart from "compiled by a build that predates the key".
    unmatched_patterns: list[dict[str, str]] = []
    # (priority, id, toolkey, regex, hint_escaped)
    compiled_rows: list[tuple[int, str, str, str, str]] = []

    try:
        merged = _load_merged_raw(vault)
    except Exception as e:  # fail-open: a corrupt catalog yields zero rules
        _debug_log(f"action_rules: failed to load catalog: {e}")
        merged = []

    grep_path = _find_grep()

    for raw in merged:
        if not isinstance(raw, dict):
            continue
        cmd_triggers = raw.get("command_triggers")
        if not isinstance(cmd_triggers, dict) or not cmd_triggers:
            continue  # not an action-style route — irrelevant here
        rules_total += 1
        rid = raw.get("id") or "<missing-id>"
        try:
            priority = int(raw.get("priority", 50))
        except (TypeError, ValueError):
            priority = 50
        hint_escaped = _escape_hint(raw.get("hint", ""))

        try:
            rows, skip_reason, unmatched = _compile_one_rule(raw, grep_path)
        except Exception as e:  # fail-open: never let one bad rule abort the run
            rows, skip_reason, unmatched = [], f"unexpected error: {e}", []

        if skip_reason is not None:
            skipped.append({"id": str(rid), "reason": skip_reason})
            continue
        unmatched_patterns.extend(unmatched)
        for toolkey, pattern, rule_id in rows:
            compiled_rows.append((priority, rule_id, toolkey, pattern, hint_escaped))

    # priority DESC, then id ASC (deterministic; first match in the hook wins)
    compiled_rows.sort(key=lambda row: (-row[0], row[1], row[2]))

    if grep_path is None and tsv_path.exists():
        # Nothing could be validated, so nothing compiled. Overwriting the
        # previously compiled TSV with an empty one would silently disable
        # every action rule the next time brain_sync runs from a process whose
        # PATH lacks grep (seen on Windows: MCP server env has git, not grep).
        # Keep the last good artifacts and say so in meta.
        _debug_log("action_rules: grep unavailable — keeping previous compiled artifacts")
        meta = {
            "compiled_at": datetime.now(timezone.utc).isoformat(),
            "rules_total": rules_total,
            "rules_compiled": 0,
            "skipped": skipped,
            "validation": "unavailable",
            "kept_previous": True,
        }
        try:
            atomic_write_text(meta_path, json.dumps(meta, ensure_ascii=False, indent=2) + "\n")
        except OSError as e:
            _debug_log(f"action_rules: failed to write meta: {e}")
        return tsv_path

    lines = [
        "\t".join((toolkey, rule_id, pattern, hint_escaped))
        for _priority, rule_id, toolkey, pattern, hint_escaped in compiled_rows
    ]
    tsv_content = "\n".join(lines)
    if tsv_content:
        tsv_content += "\n"

    try:
        atomic_write_text(tsv_path, tsv_content)
    except OSError as e:
        _debug_log(f"action_rules: failed to write TSV: {e}")

    # Per-toolkey fast-reject pattern file: ALL of that tool's patterns
    # combined so the hook can ask "does anything match at all?" with ONE
    # `grep -qEf` fork instead of one fork per rule row. The overwhelming
    # majority of commands match nothing, so this turns the common case from
    # O(rule count) forks into O(1). On an actual hit (rare — it means a
    # warning is about to fire) the hook falls back to the existing per-row
    # loop over the TSV to identify *which* rule matched, unchanged: that
    # keeps rule identification on the exact same grep -E engine `_validate_
    # pattern` used to vet the pattern in the first place, rather than
    # inventing a second, potentially non-identical, matching path.
    per_tool_patterns: dict[str, list[str]] = {k: [] for k in _TOOL_KEYS}
    for _priority, _rule_id, toolkey, pattern, _hint_escaped in compiled_rows:
        per_tool_patterns[toolkey].append(pattern)

    for toolkey in _TOOL_KEYS:
        re_path = index_dir / RE_BASENAME_FMT.format(tool=toolkey)
        re_content = "".join(f"{pattern}\n" for pattern in per_tool_patterns[toolkey])
        try:
            atomic_write_text(re_path, re_content)
        except OSError as e:
            _debug_log(f"action_rules: failed to write {toolkey} fast-reject file: {e}")

    compiled_ids = {row[1] for row in compiled_rows}
    meta = {
        "compiled_at": datetime.now(timezone.utc).isoformat(),
        "rules_total": rules_total,
        "rules_compiled": len(compiled_ids),
        "skipped": skipped,
        "unmatched_patterns": unmatched_patterns,
    }
    try:
        atomic_write_text(meta_path, json.dumps(meta, ensure_ascii=False, indent=2) + "\n")
    except OSError as e:
        _debug_log(f"action_rules: failed to write meta: {e}")

    return tsv_path
