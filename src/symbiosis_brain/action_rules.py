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

Fail-open everywhere: a rule with no test vectors passing, a rule whose
regex contains a tab (would corrupt the TSV), or grep being unavailable on
PATH — all skip that rule (or all rules) and record why in ``meta.json``.
Nothing here raises out of ``compile_action_rules``.
"""
from __future__ import annotations

import json
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
_TOOL_KEYS = ("bash", "powershell")
_GREP_TIMEOUT_SECONDS = 5


def _find_grep() -> Optional[str]:
    """Locate a ``grep`` executable. Tries PATH directly first, then derives
    a location from ``bash`` on PATH (Git-for-Windows ships grep alongside
    bash under the same root, but only bash is reliably on PATH)."""
    found = shutil.which("grep")
    if found:
        return found
    bash = shutil.which("bash")
    if not bash:
        return None
    bash_path = Path(bash).resolve()
    candidates = [
        bash_path.parent / "grep.exe",
        bash_path.parent / "grep",
        bash_path.parent.parent / "usr" / "bin" / "grep.exe",
        bash_path.parent.parent / "usr" / "bin" / "grep",
        bash_path.parent / "usr" / "bin" / "grep.exe",
        bash_path.parent / "usr" / "bin" / "grep",
    ]
    for cand in candidates:
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


def _validate_pattern(
    grep_path: str, pattern: str, matches: list[str], nomatches: list[str]
) -> tuple[bool, str]:
    import os
    import tempfile

    fd, tmp_name = tempfile.mkstemp(suffix=".re")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(pattern + "\n")
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
) -> tuple[list[tuple[str, str, str]], Optional[str]]:
    """Returns (rows, skip_reason). rows is a list of (toolkey, regex, id)
    for each tool side that validated; skip_reason is set (rows empty) when
    the whole rule is dropped."""
    rid = raw.get("id")
    if not rid or not isinstance(rid, str):
        return [], "missing or invalid id"
    cmd_triggers = raw.get("command_triggers")
    if not isinstance(cmd_triggers, dict):
        return [], "no command_triggers"
    test_match = raw.get("test_match") or {}
    test_nomatch = raw.get("test_nomatch") or {}
    if not isinstance(test_match, dict):
        test_match = {}
    if not isinstance(test_nomatch, dict):
        test_nomatch = {}

    if grep_path is None:
        return [], "grep unavailable on PATH — cannot validate ERE rules"

    rows: list[tuple[str, str, str]] = []
    for toolkey in _TOOL_KEYS:
        trigs = cmd_triggers.get(toolkey)
        if not trigs:
            continue
        for trig in trigs:
            pattern = trig.get("re") if isinstance(trig, dict) else None
            if not pattern or not isinstance(pattern, str):
                return [], f"{toolkey}: trigger missing 're'"
            if "\t" in pattern:
                return [], f"{toolkey}: regex contains a tab character"
            ok, reason = _validate_pattern(
                grep_path,
                pattern,
                list(test_match.get(toolkey) or []),
                list(test_nomatch.get(toolkey) or []),
            )
            if not ok:
                return [], f"{toolkey}: {reason}"
            rows.append((toolkey, pattern, rid))
    if not rows:
        return [], "command_triggers has no bash/powershell entries"
    return rows, None


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
            rows, skip_reason = _compile_one_rule(raw, grep_path)
        except Exception as e:  # fail-open: never let one bad rule abort the run
            rows, skip_reason = [], f"unexpected error: {e}"

        if skip_reason is not None:
            skipped.append({"id": str(rid), "reason": skip_reason})
            continue
        for toolkey, pattern, rule_id in rows:
            compiled_rows.append((priority, rule_id, toolkey, pattern, hint_escaped))

    # priority DESC, then id ASC (deterministic; first match in the hook wins)
    compiled_rows.sort(key=lambda row: (-row[0], row[1], row[2]))

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

    compiled_ids = {row[1] for row in compiled_rows}
    meta = {
        "compiled_at": datetime.now(timezone.utc).isoformat(),
        "rules_total": rules_total,
        "rules_compiled": len(compiled_ids),
        "skipped": skipped,
    }
    try:
        atomic_write_text(meta_path, json.dumps(meta, ensure_ascii=False, indent=2) + "\n")
    except OSError as e:
        _debug_log(f"action_rules: failed to write meta: {e}")

    return tsv_path
