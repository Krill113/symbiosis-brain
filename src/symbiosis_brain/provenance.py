"""Good-faith client and executor signatures, not authentication."""
from __future__ import annotations

import re
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


_BRAIN_WRITE_TOOLS = frozenset(
    f"mcp__{server}__{tool}"
    for server in ("symbiosis-brain", "symbiosis_brain")
    for tool in ("brain_write", "brain_append", "brain_patch", "brain_rotate_handoffs")
)


def is_brain_write_tool(tool_name: str) -> bool:
    """Recognize the four writing MCP calls under the supported server aliases."""
    return tool_name in _BRAIN_WRITE_TOOLS


def written_by_value(
    app, today: date | None = None, *, model: object = None, role: object = None,
) -> str:
    """Last editor: client/version, supplied model, date and optional actor role.

    Adapter values take precedence before reaching this boundary. Missing or
    malformed self-reports degrade independently; provenance never blocks a write.
    """
    day = today if today is not None else date.today()
    model_name = "unknown"
    if isinstance(model, str):
        normalized = re.sub(r"[^A-Za-z0-9._:/@+-]", "-", model.strip())
        model_name = re.sub(r"-+", "-", normalized)[:80] or "unknown"
    stamp = f"{client_id(app)} {model_name} {day.isoformat()}"
    if isinstance(role, str) and role in ("lead", "colleague", "subagent"):
        stamp += f" · {role}"
    return stamp
