"""Symbiosis Brain — SessionStart hook (Python parity).

Mirrors hooks/brain-session-start.sh exactly. Layer 1: dumb basename →
kebab-case scope. Skill brain-init handles marker-based override.

Cross-platform: works on Windows native (no bash needed), macOS, Linux.
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

# Force UTF-8 on stdout/stderr — Windows defaults to CP1251 which crashes on 🧠 / Cyrillic.
# Claude Code invokes this hook in a non-TTY context where Python's default encoding
# follows locale; explicit reconfigure is portable across all platforms.
for _stream in (sys.stdout, sys.stderr):
    if _stream.encoding and _stream.encoding.lower() not in ("utf-8", "utf8"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError):
            pass  # Older Python or non-reconfigurable stream — best-effort


def normalize_scope(raw):
    """Port of bash normalize_scope. Tested for parity."""
    if not raw:
        return ""
    s = str(raw)
    # camelCase boundaries: insert dash between lower/digit and Upper
    s = re.sub(r"([a-z0-9])([A-Z])", r"\1-\2", s)
    # ABCService → ABC-Service: dash between Upper and Upper-followed-by-lower
    s = re.sub(r"([A-Z])([A-Z][a-z])", r"\1-\2", s)
    s = s.lower()
    # Separators → dash
    s = re.sub(r"[._ \t]+", "-", s)
    # Drop non-alnum-dash
    s = re.sub(r"[^a-z0-9-]", "", s)
    # Collapse multi-dashes, strip edges
    s = re.sub(r"-+", "-", s).strip("-")
    return s


TOOL_ROSTER = ("Available tools: brain_search/brain_read/brain_write (memory), "
               "Serena (find_symbol/replace_symbol_body), subagents (Explore/general-purpose), "
               "screenshot.")


def _tmp_dir() -> Path:
    """Cross-platform temp dir matching bash hook (used /tmp on POSIX, %TEMP% on Windows)."""
    return Path(os.environ.get("TMPDIR") or os.environ.get("TEMP") or "/tmp")


def _clean_session_flags(session_id: str) -> None:
    if not session_id:
        return
    tmp = _tmp_dir()
    for name in (
        f"brain-triggered-{session_id}",
        f"brain-precompact-{session_id}",
        f"brain-precompact-pending-{session_id}",
        f"brain-last-save-pct-{session_id}",
        f"brain-save-later-{session_id}",
        f"brain-rules-shown-{session_id}",
        f"brain-rules-turn-counter-{session_id}",
    ):
        try:
            (tmp / name).unlink()
        except FileNotFoundError:
            pass
    # Mark current session
    try:
        (tmp / "brain-current-session").write_text(session_id, encoding="utf-8")
    except OSError:
        pass


def main():
    # Read stdin JSON (Claude Code passes hook input as JSON)
    try:
        raw = sys.stdin.read()
        data = json.loads(raw) if raw.strip() else {}
    except Exception:
        data = {}
    session_id = data.get("session_id") or ""

    vault = os.environ.get("SYMBIOSIS_BRAIN_VAULT", "")
    cwd = Path.cwd()
    scope = normalize_scope(cwd.name) or "global"

    env_file = os.environ.get("CLAUDE_ENV_FILE")
    if env_file:
        with open(env_file, "a", encoding="utf-8") as f:
            f.write(f'export SYMBIOSIS_BRAIN_VAULT="{vault}"\n')
            f.write(f'export SYMBIOSIS_BRAIN_SCOPE="{scope}"\n')
            if session_id:
                f.write(f'export CLAUDE_SESSION_ID="{session_id}"\n')

    # L0: critical facts
    cf_path = Path(vault) / "CRITICAL_FACTS.md" if vault else None
    if cf_path and cf_path.exists():
        print("=== Symbiosis Brain ===")
        print(cf_path.read_text(encoding="utf-8"))
        print()

    # Tool roster
    print(TOOL_ROSTER)
    print()

    print(f"[scope: {scope}]")

    _clean_session_flags(session_id)
    return 0


if __name__ == "__main__":
    main()
