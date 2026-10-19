"""OpenCode adapter configuration, isolated from other provider adapters."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class AdapterConfig:
    """Resolved paths and bounds used by the adapter bridge."""

    vault: Path | None
    boundary: Path | None
    char_limit: int = 8000

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "AdapterConfig":
        values = os.environ if env is None else env
        vault_value = values.get("SYMBIOSIS_BRAIN_VAULT")
        boundary_value = values.get("OPENCODE_PROJECT_BOUNDARY")
        try:
            requested_limit = int(values.get("OPENCODE_BOOTSTRAP_CHAR_LIMIT", "8000"))
        except (TypeError, ValueError):
            requested_limit = 8000
        return cls(
            vault=Path(vault_value).expanduser() if vault_value else None,
            boundary=Path(boundary_value).expanduser() if boundary_value else None,
            char_limit=min(max(requested_limit, 256), 32000),
        )
