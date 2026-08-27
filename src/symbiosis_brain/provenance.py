"""Provenance: who last wrote a note, and who asked for a retrieval.

Stage 2 slice 1 (CP-2) ships ONLY `client_id`. `written_by_value` and the
model bridge arrive in CP-4 and CP-5 (Р2) — do not anticipate them here.

The client label is SELF-REPORTED by the MCP client and is a good-faith mark,
not an authentication ([отчёт 02, риск 7]). The docs say so too.
"""
from __future__ import annotations

import os
import re
import time
from datetime import date
from pathlib import Path


def client_id(app) -> str:
    """'<name>/<version>' from the MCP `initialize` handshake, or
    'unknown/unknown'.

    Path verified in the installed SDK (§3.3):
      app.request_context.session.client_params.clientInfo
    `Server.request_context` is a property that raises LookupError outside a
    request (mcp/server/lowlevel/server.py:240-244), and `client_params` is
    None until `initialize` lands (mcp/server/session.py:107-108,168). Both
    are ordinary states, not failures, so they map to 'unknown/unknown'.
    """
    try:
        params = app.request_context.session.client_params
        if params is None:
            return "unknown/unknown"
        info = params.clientInfo
        name = getattr(info, "name", "") or "unknown"
        version = getattr(info, "version", "") or "unknown"
        return f"{name}/{version}"
    except (LookupError, AttributeError):
        return "unknown/unknown"


# --- Model bridge: status line -> server (Q2, §3.4, I-14/I-15) ----------------
# The writer is hooks/sb-export.sh (CP-5). The reader below has the FINAL
# signature, so callers never change.
BRIDGE_PREFIX = "brain-model-"
BRIDGE_TTL_SECONDS = 900

_SLUG_SEPARATORS = re.compile(r"[^a-z0-9]+")
_EPOCH = re.compile(r"[0-9]+")


def _bridge_dir() -> Path:
    """Temp dir the bridge files live in — the same `${TMPDIR:-${TEMP:-/tmp}}` chain
    the hooks use (`sb_tmp_dir`, hooks/sb-hooklib.sh:13-15; `_tmp_dir`,
    pre_action_config.py:18-25). Resolved per call, never cached at import: the test
    suite moves TMPDIR/TEMP with monkeypatch, and a module-level constant would
    freeze the developer's live temp dir into every run.
    """
    base = os.environ.get("TMPDIR") or os.environ.get("TEMP") or "/tmp"
    return Path(base)


def _slugify(value: str) -> str:
    """'Claude Opus 4.5' -> 'claude-opus-4-5' (I-14 п. 4). Empty when the name has
    no [a-z0-9] left at all."""
    return _SLUG_SEPARATORS.sub("-", value.strip().lower()).strip("-")


def _parse_bridge(text: str) -> str:
    """Strict parse of one bridge line: `"<model_id>\\t<display_name>\\t<epoch>\\n"`.

    Strict rather than best-effort, and that is the whole design (I-14 п. 2): the
    writer's printf cannot be atomic (a fork-free status line cannot mv a file into
    place, hooks/sb-hooklib.sh:4-9) and Claude Code cancels the in-flight status
    script on every event, so a truncated write leaves a line that is otherwise
    indistinguishable from a valid one — and would sign notes with half a model name.
    Exactly three TAB fields, exactly one trailing newline, integer timestamp.
    """
    if not text.endswith("\n") or text.count("\n") != 1:
        return "unknown"
    fields = text[:-1].split("\t")
    if len(fields) != 3:
        return "unknown"
    model_id, display_name, epoch = fields
    if not _EPOCH.fullmatch(epoch):
        return "unknown"
    return _slugify(model_id) or _slugify(display_name) or "unknown"


def _is_fresh(path: Path, now: float) -> bool:
    try:
        return now - path.stat().st_mtime <= BRIDGE_TTL_SECONDS
    except OSError:
        return False


def model_from_bridge(now: float | None = None) -> str:
    """Model slug written by the status line for THIS window, or `"unknown"`.

    Contract — §3.4, I-14, five steps, in this order:

    1. Our own file is `<tmp>/brain-model-<os.getppid()>`: the MCP server is a direct
       child of the claude.exe window (the same fact parent_watchdog.py:62 relies on),
       and `CLAUDE_PID` in the status line's environment is that window's PID. If the
       file exists and is younger than BRIDGE_TTL_SECONDS, parse it and return.
    2. If it exists but is STALE — return "unknown" and do NOT scan other windows. A
       file under our own key means the launcher is the standard one and the key is
       right, so the data is merely old; there is no reason to look into someone
       else's window.
    3. Only when there is no file under our key at all (a non-standard launcher: uv
       run, a shim) do we scan `brain-model-*` younger than the TTL. Exactly one
       candidate is used; zero, or two and more, give "unknown" — staying silent is
       more honest than signing a note with the neighbouring window's model.
    4. Slug from `model_id`, else from `display_name` (I-14 п. 4).
    5. Any error at all gives "unknown"; this function never raises. It sits on the
       note-writing path, and telemetry must never break a write (principle 1, §1.3).

    No cache: the file is ~40 bytes and is read a handful of times per session.
    """
    try:
        moment = time.time() if now is None else float(now)
        base = _bridge_dir()
        own = base / f"{BRIDGE_PREFIX}{os.getppid()}"
        try:
            own_mtime = own.stat().st_mtime
        except OSError:
            own_mtime = None
        if own_mtime is not None:
            if moment - own_mtime > BRIDGE_TTL_SECONDS:
                return "unknown"          # step 2 — no fallback scan
            return _parse_bridge(own.read_text(encoding="utf-8"))

        fresh: list[Path] = []
        for candidate in base.glob(f"{BRIDGE_PREFIX}*"):
            if not _is_fresh(candidate, moment):
                continue
            fresh.append(candidate)
            if len(fresh) > 1:
                return "unknown"          # two live windows -> silence
        if len(fresh) != 1:
            return "unknown"
        return _parse_bridge(fresh[0].read_text(encoding="utf-8"))
    except Exception:
        return "unknown"


def written_by_value(app, today: date | None = None) -> str:
    """`"<client>/<version> <model-slug> <YYYY-MM-DD>"` — I-12, §3.1.

    The field means WHO LAST WROTE the note through the product (client, model,
    date of that write), not who came up with it: all three writing tools stamp
    it, and brain_append/brain_patch rebuild the whole frontmatter, so an earlier
    signature is overwritten by design (§3.1). Both halves degrade to `unknown`
    on their own, so this never raises and the key is always written — a missing
    `written_by` means "the note predates provenance" (§1.2, B8).
    """
    day = today if today is not None else date.today()
    return f"{client_id(app)} {model_from_bridge()} {day.isoformat()}"
