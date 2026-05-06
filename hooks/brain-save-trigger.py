"""Symbiosis Brain — proactive save trigger (Python parity).

Modes:
  stop          — context-threshold check, exit 2 to block
  precompact    — last-chance blocker before /compact
  prompt-check  — UserPromptSubmit composer (A1 recall + A2 rules + pending)

Mirrors hooks/brain-save-trigger.sh exactly.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path
from symbiosis_brain.atomic_write import atomic_write_text

# Force UTF-8 on stdout/stderr — Windows defaults to CP1251 which crashes on 🧠 / Cyrillic.
# Claude Code invokes this hook in a non-TTY context where Python's default encoding
# follows locale; explicit reconfigure is portable across all platforms.
for _stream in (sys.stdout, sys.stderr):
    if _stream.encoding and _stream.encoding.lower() not in ("utf-8", "utf8"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError):
            pass  # Older Python or non-reconfigurable stream — best-effort

THRESHOLDS = (40, 70, 90)
DELTA_GUARD = 20


def _tmp_dir() -> Path:
    return Path(os.environ.get("TMPDIR") or os.environ.get("TEMP") or "/tmp")


def _read_session_id(stdin_data: dict) -> str:
    return stdin_data.get("session_id") or "default"


def _read_pct(session_id: str) -> int | None:
    f = _tmp_dir() / f"brain-context-pct-{session_id}"
    try:
        return int(f.read_text(encoding="utf-8").strip())
    except (FileNotFoundError, ValueError):
        return None


def _read_int(path: Path, default: int = 0) -> int:
    try:
        return int(path.read_text(encoding="utf-8").strip())
    except (FileNotFoundError, ValueError):
        return default


DEFAULT_RULES_TEXT = (
    "Отвечай tight, без воды. Большие чтения / поиск / анализ — делегируй субагентам.\n"
    "Доступно: brain_search/brain_read (память), Serena (find_symbol/replace_symbol_body), "
    "субагенты (Explore/general-purpose)."
)

CONFIRMATION_RE = re.compile(r"^(да|нет|ок|yes|no|ok|continue|продолжай)$", re.IGNORECASE)


def _gist_recall(prompt: str, scope: str, vault: str, top_k: int) -> str:
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "symbiosis_brain", "search-gist",
             "--vault", vault, "--query", prompt, "--scope", scope, "--limit", str(top_k)],
            # Timeout 12s: cold-start ~9.6s (fastembed import + sync + search),
            # warm ~3.6s. Per-prompt fresh process. Daemon/pre-warm: A-v2 spec.
            capture_output=True, text=True, timeout=12,
        )
        if proc.returncode != 0:
            return ""
        data = json.loads(proc.stdout or "[]")
        if not data:
            return ""
        lines = [f"[memory: {len(data)} hits, scope={scope}]"]
        for n in data:
            lines.append(f"- {n['path']} — {n.get('gist','')}")
        return "\n".join(lines)
    except (subprocess.TimeoutExpired, Exception):
        return ""


def cmd_stop(data: dict) -> int:
    if data.get("stop_hook_active"):
        return 0
    session_id = _read_session_id(data)
    pct = _read_pct(session_id)
    if pct is None:
        return 0

    last_save_pct = _read_int(_tmp_dir() / f"brain-last-save-pct-{session_id}")
    delta = pct - last_save_pct
    save_later_file = _tmp_dir() / f"brain-save-later-{session_id}"
    triggered_file = _tmp_dir() / f"brain-triggered-{session_id}"

    triggered = set()
    if triggered_file.exists():
        triggered = {l.strip() for l in triggered_file.read_text(encoding="utf-8").splitlines() if l.strip()}

    # Highest crossed threshold not yet triggered
    for t in reversed(THRESHOLDS):
        if pct < t:
            continue
        if str(t) in triggered:
            continue
        # Delta-guard except at 90+
        if t < 90 and delta < DELTA_GUARD:
            return 0
        # SAVE_LATER one-shot in soft zone (40-70)
        if 40 <= t < 70 and save_later_file.exists():
            save_later_file.unlink()
            return 0
        # Mark all crossed thresholds (atomic write to avoid torn lines under
        # concurrent stop hooks within the same session).
        new_marks = [str(mark) for mark in THRESHOLDS
                     if mark <= pct and str(mark) not in triggered]
        triggered.update(new_marks)
        if new_marks:
            sorted_marks = sorted(triggered, key=lambda x: int(x))
            atomic_write_text(triggered_file, "".join(f"{m}\n" for m in sorted_marks))
        # Zone-based message
        if t < 70:
            msg = f"🧠 Контекст {pct}%, delta +{delta}%. Сохрани если есть что — или скажи SAVE_LATER чтобы пропустить раз."
        elif t < 90:
            msg = f"🧠 Контекст {pct}% — пора сохранять знание, скоро /compact."
        else:
            msg = f"🧠 Контекст {pct}% — последний шанс сохранить перед авто-компакцией."
        print(msg, file=sys.stderr)
        return 2
    return 0


def cmd_precompact(data: dict) -> int:
    session_id = _read_session_id(data)
    precompact_file = _tmp_dir() / f"brain-precompact-{session_id}"
    if not precompact_file.exists():
        precompact_file.touch()
        (_tmp_dir() / f"brain-precompact-pending-{session_id}").touch()
        print("🧠 Save memory? Type any message to trigger brain-save. Just /compact again to skip.",
              file=sys.stderr)
        return 2
    return 0


def cmd_prompt_check(data: dict) -> int:
    session_id = _read_session_id(data)
    prompt = data.get("prompt") or ""
    scope = os.environ.get("SYMBIOSIS_BRAIN_SCOPE", "global")

    pending_block = ""
    pending = _tmp_dir() / f"brain-precompact-pending-{session_id}"
    if pending.exists():
        pending.unlink()
        pending_block = "🧠 Compaction was blocked. Run brain-save to preserve knowledge, then tell user to repeat /compact."

    recall_enabled = os.environ.get("SYMBIOSIS_BRAIN_RECALL_ENABLED", "true") == "true"
    recall_top_k = int(os.environ.get("SYMBIOSIS_BRAIN_RECALL_TOP_K", "5"))
    skip_chars = int(os.environ.get("SYMBIOSIS_BRAIN_RECALL_SKIP_SHORT_CHARS", "15"))
    rules_enabled = os.environ.get("SYMBIOSIS_BRAIN_RULES_ENABLED", "true") == "true"
    rules_zones = [int(z) for z in os.environ.get("SYMBIOSIS_BRAIN_RULES_ZONES", "30,60,85").split(",") if z]
    rules_turn = int(os.environ.get("SYMBIOSIS_BRAIN_RULES_TURN_INTERVAL", "10"))
    rules_text = os.environ.get("SYMBIOSIS_BRAIN_RULES_TEXT", DEFAULT_RULES_TEXT)
    vault = os.environ.get("SYMBIOSIS_BRAIN_VAULT", "")

    # A1 memory recall
    memory_block = ""
    skip_recall = False
    if not recall_enabled or len(prompt) < skip_chars or prompt.startswith("/"):
        skip_recall = True
    elif CONFIRMATION_RE.match(prompt.strip()):
        skip_recall = True
    if not skip_recall and vault:
        memory_block = _gist_recall(prompt, scope, vault, recall_top_k)

    # A2 rules
    rules_block = ""
    if rules_enabled:
        pct = _read_pct(session_id) or 0
        shown_file = _tmp_dir() / f"brain-rules-shown-{session_id}"
        turn_file = _tmp_dir() / f"brain-rules-turn-counter-{session_id}"
        turns = _read_int(turn_file) + 1
        shown = set()
        if shown_file.exists():
            shown = {l.strip() for l in shown_file.read_text(encoding="utf-8").splitlines() if l.strip()}

        crossed = [z for z in rules_zones if pct >= z]
        highest_crossed = max(crossed) if crossed else -1
        crossed_shown = [z for z in crossed if str(z) in shown]
        highest_shown = max(crossed_shown) if crossed_shown else -1
        zone_hit = highest_crossed > highest_shown

        if zone_hit:
            new_zones = [str(z) for z in crossed if str(z) not in shown]
            shown.update(new_zones)
            if new_zones:
                sorted_zones = sorted(shown, key=lambda x: int(x))
                atomic_write_text(shown_file, "".join(f"{z}\n" for z in sorted_zones))
        if zone_hit or turns >= rules_turn:
            rules_block = f"[rules — context {pct}%]\n{rules_text}"
            atomic_write_text(turn_file, "0")
        else:
            atomic_write_text(turn_file, str(turns))

    # Compose
    blocks = [b for b in (memory_block, rules_block, pending_block) if b]
    if blocks:
        print("<system-reminder>")
        print("\n\n".join(blocks))
        print("</system-reminder>")
    return 0


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "stop"
    try:
        raw = sys.stdin.read()
        data = json.loads(raw) if raw.strip() else {}
    except Exception:
        data = {}
    if mode == "stop":
        sys.exit(cmd_stop(data))
    elif mode == "precompact":
        sys.exit(cmd_precompact(data))
    elif mode == "prompt-check":
        sys.exit(cmd_prompt_check(data))
    sys.exit(0)


if __name__ == "__main__":
    main()
