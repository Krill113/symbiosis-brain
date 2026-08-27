"""Provenance: who last wrote a note, and who asked for a retrieval.

Stage 2 slice 1 (CP-2) ships ONLY `client_id`. `written_by_value` and the
model bridge arrive in CP-4 and CP-5 (Р2) — do not anticipate them here.

The client label is SELF-REPORTED by the MCP client and is a good-faith mark,
not an authentication ([отчёт 02, риск 7]). The docs say so too.
"""
from __future__ import annotations

from datetime import date


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
# The writer is hooks/sb-export.sh and it appears only in CP-5; the reader below
# is a placeholder with the FINAL signature, so callers never change.
BRIDGE_PREFIX = "brain-model-"
BRIDGE_TTL_SECONDS = 900


def model_from_bridge(now: float | None = None) -> str:
    """Model slug from the status-line bridge, or `"unknown"` (I-14, §3.4).

    Nothing writes the bridge yet (CP-5 adds hooks/sb-export.sh), so the honest
    answer is `"unknown"` — the same literal the full implementation returns when
    the file is missing, stale, ambiguous or truncated. CP-5 replaces ONLY this
    body; the signature, the constants and every call site stay as they are.
    """
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
