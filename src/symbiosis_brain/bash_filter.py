"""Bash whitelist matcher for pre-action recall hook (B1).

Returns True if the command matches any regex in the whitelist.
Matching is case-insensitive and anchored (regex authors prefix `^`).
"""
import re


def matches_whitelist(command: str, whitelist: list[str]) -> bool:
    """Check command against regex whitelist. Returns True on any match."""
    if not command or not whitelist:
        return False
    for pattern in whitelist:
        try:
            if re.match(pattern, command, re.IGNORECASE):
                return True
        except re.error:
            continue  # malformed regex in user config — skip silently
    return False
