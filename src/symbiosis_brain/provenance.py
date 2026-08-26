"""Provenance: who last wrote a note, and who asked for a retrieval.

Stage 2 slice 1 (CP-2) ships ONLY `client_id`. `written_by_value` and the
model bridge arrive in CP-4 and CP-5 (Р2) — do not anticipate them here.

The client label is SELF-REPORTED by the MCP client and is a good-faith mark,
not an authentication ([отчёт 02, риск 7]). The docs say so too.
"""
from __future__ import annotations


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
