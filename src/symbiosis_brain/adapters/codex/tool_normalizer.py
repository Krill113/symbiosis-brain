"""Privacy-preserving Codex tool-call normalization for recall queries."""
from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping, Sequence
from typing import Any

SECRET_KEYS = {
    "authorization",
    "api_key",
    "apikey",
    "connection_string",
    "connection_strings",
    "credentials",
    "database_url",
    "password",
    "private_key",
    "secret",
    "token",
    "access_token",
    "refresh_token",
    "cookie",
    "cookies",
    "headers",
}

_REDACTED = "[REDACTED]"
_SECRET_COMMAND_MARKER = "secret-bearing command [REDACTED]"
_SECRET_PATCH_LINE_MARKER = "[REDACTED SECRET LINE]"
_PATCH_SCAN_MAX_CHARS = 1_000_000
_SECRET_KEY_PARTS = {
    "authorization",
    "apikey",
    "cookie",
    "cookies",
    "credentials",
    "headers",
    "password",
    "secret",
    "token",
}
_SECRET_KEY_PAIRS = {
    ("api", "key"),
    ("connection", "string"),
    ("connection", "strings"),
    ("database", "url"),
    ("private", "key"),
}
_TEXT_ASSIGNMENT_RE = re.compile(
    r"""(?ix)
    (?<![a-z0-9])
    (?P<sigil>\$env:|\$)?
    (?P<key_quote>["']?)
    (?P<key>[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*)
    (?P=key_quote)
    (?P<separator>\s*(?:=|:)\s*)
    (?P<scheme>(?:bearer|basic)\s+)?
    (?P<value>
        "(?:[^"\\]|\\.)*" |
        '(?:[^'\\]|\\.)*' |
        [^\s"';,&|}\]]+
    )
    """
)
_SET_ITEM_ENV_RE = re.compile(
    r"""(?ix)
    (?P<prefix>\bset-item\s+(?:-path\s+)?env:)
    (?P<key>[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*)
    (?P<separator>\s+(?:-value\s+)?)
    (?P<value>
        "(?:[^"\\]|\\.)*" |
        '(?:[^'\\]|\\.)*' |
        [^\s;&|]+
    )
    """
)
_SET_ITEM_COMMAND_RE = re.compile(
    r"(?im)(?<![a-z0-9_-])set-item\b[^\r\n;|&]*"
)
_SETX_RE = re.compile(
    r"""(?ix)
    (?P<prefix>\bsetx(?:\.exe)?\s+)
    (?P<key>[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*)
    (?P<separator>\s+)
    (?P<value>
        "(?:[^"\\]|\\.)*" |
        '(?:[^'\\]|\\.)*' |
        [^\s;&|]+
    )
    """
)
_SETX_COMMAND_RE = re.compile(r"(?im)(?<![a-z0-9_-])setx(?:\.exe)?\b[^\r\n;|&]*")
_COMMAND_TOKEN_RE = re.compile(
    r""""(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*'|[^\s]+"""
)
_AUTH_HEADER_FLAG_RE = re.compile(
    r"""(?ix)
    (?P<prefix>
        (?<![a-z0-9])
        (?:-H|--header)
        \s*
        (?:proxy-)?authorization
        \s*:\s*
        (?:bearer|basic)\s+
    )
    (?P<value>
        "(?:[^"\\]|\\.)*" |
        '(?:[^'\\]|\\.)*' |
        [^\s;&|]+
    )
    """
)
_COOKIE_FLAG_RE = re.compile(
    r"""(?ix)
    (?P<prefix>--cookie(?:-jar)?\s+)
    (?:"[^"]*"|'[^']*'|[^\s]+)
    """
)
_SETX_PASSWORD_MARKER_RE = re.compile(
    r"(?is)(?<![a-z0-9_-])setx(?:\.exe)?\b.*?(?:\A|\s)/p(?=[:=\s]|$)"
)


def _cap(text: str, max_chars: int) -> str:
    if type(max_chars) is not int or max_chars <= 0:
        return ""
    return text[:max_chars]


def _collapse(text: str) -> str:
    return " ".join(text.split())


def _key_parts(key: str) -> list[str]:
    with_camel_boundaries = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", key)
    return [
        part
        for part in re.split(r"[^a-z0-9]+", with_camel_boundaries.casefold())
        if part
    ]


def _is_secret_key(key: str) -> bool:
    parts = _key_parts(key)
    if not parts:
        return False
    if any(part in _SECRET_KEY_PARTS for part in parts):
        return True
    return any(pair in _SECRET_KEY_PAIRS for pair in zip(parts, parts[1:]))


def _contains_secret_marker(text: str) -> bool:
    """Conservatively classify text without parsing shell quoting or syntax."""

    folded = text.casefold()
    if any(marker in folded for marker in ("authorization", "bearer", "basic")):
        return True
    parts = _key_parts(text)
    if any(part in _SECRET_KEY_PARTS for part in parts):
        return True
    if any(pair in _SECRET_KEY_PAIRS for pair in zip(parts, parts[1:])):
        return True
    return _SETX_PASSWORD_MARKER_RE.search(text) is not None


def _safe_patch_line(line: str) -> str:
    if _contains_secret_marker(line):
        return _SECRET_PATCH_LINE_MARKER
    return line


def _unquote_token(token: str) -> str:
    if len(token) >= 2 and token[0] == token[-1] and token[0] in {"'", '"'}:
        return token[1:-1]
    return token


def _replace_token_spans(
    command: str,
    tokens: list[re.Match[str]],
    indexes: set[int],
) -> str:
    for index in sorted(indexes, reverse=True):
        token = tokens[index]
        command = command[: token.start()] + _REDACTED + command[token.end() :]
    return command


def _redact_set_item_command(match: re.Match[str]) -> str:
    command = match.group(0)
    tokens = list(_COMMAND_TOKEN_RE.finditer(command))
    path_key: str | None = None
    value_index: int | None = None
    for index in range(1, len(tokens)):
        token = _unquote_token(tokens[index].group(0))
        option = token.casefold()
        if option in {"-path", "-literalpath"} and index + 1 < len(tokens):
            path = _unquote_token(tokens[index + 1].group(0))
            if path.casefold().startswith("env:"):
                path_key = path[4:]
        elif option == "-value" and index + 1 < len(tokens):
            value_index = index + 1
        elif option.startswith("env:"):
            path_key = token[4:]

    if path_key is None or value_index is None or not _is_secret_key(path_key):
        return command
    return _replace_token_spans(command, tokens, {value_index})


def _redact_setx_command(match: re.Match[str]) -> str:
    command = match.group(0)
    tokens = list(_COMMAND_TOKEN_RE.finditer(command))
    replacements: set[int] = set()
    positional: list[int] = []
    option_values = {"/s", "/u", "/p"}
    no_value_options = {"/m"}
    index = 1
    while index < len(tokens):
        token = _unquote_token(tokens[index].group(0)).casefold()
        if token in option_values:
            if index + 1 < len(tokens):
                if token == "/p":
                    replacements.add(index + 1)
                index += 2
                continue
        elif token in no_value_options:
            index += 1
            continue
        positional.append(index)
        index += 1

    for position, token_index in enumerate(positional[:-1]):
        key = _unquote_token(tokens[token_index].group(0))
        if _is_secret_key(key):
            replacements.add(positional[position + 1])
            break
    return _replace_token_spans(command, tokens, replacements)


def _redact_text(text: str) -> str:
    """Redact common command/patch secret forms without retaining raw values."""

    redacted = _COOKIE_FLAG_RE.sub(
        lambda match: f"{match.group('prefix')}\"{_REDACTED}\"",
        text,
    )
    redacted = _SET_ITEM_COMMAND_RE.sub(_redact_set_item_command, redacted)
    redacted = _SETX_COMMAND_RE.sub(_redact_setx_command, redacted)

    def replace_command_value(match: re.Match[str]) -> str:
        if not _is_secret_key(match.group("key")):
            return match.group(0)
        return (
            f"{match.group('prefix')}{match.group('key')}"
            f"{match.group('separator')}{_REDACTED}"
        )

    redacted = _SET_ITEM_ENV_RE.sub(replace_command_value, redacted)
    redacted = _SETX_RE.sub(replace_command_value, redacted)
    redacted = _AUTH_HEADER_FLAG_RE.sub(
        lambda match: f"{match.group('prefix')}{_REDACTED}",
        redacted,
    )

    def replace_assignment(match: re.Match[str]) -> str:
        if not _is_secret_key(match.group("key")):
            return match.group(0)
        return (
            f"{match.group('sigil') or ''}"
            f"{match.group('key_quote')}{match.group('key')}"
            f"{match.group('key_quote')}"
            f"{match.group('separator')}{_REDACTED}"
        )

    return _TEXT_ASSIGNMENT_RE.sub(replace_assignment, redacted)


def _json_safe(value: Any, seen: set[int]) -> Any:
    if isinstance(value, str):
        return _redact_text(value)
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, Mapping):
        identity = id(value)
        if identity in seen:
            return "[CYCLE]"
        seen.add(identity)
        try:
            items: list[tuple[str, Any]] = []
            for raw_key, raw_value in value.items():
                key = str(raw_key)
                if _is_secret_key(key):
                    items.append((key, _REDACTED))
                else:
                    items.append((key, _json_safe(raw_value, seen)))
            return {key: item for key, item in sorted(items, key=lambda pair: pair[0])}
        finally:
            seen.remove(identity)
    if isinstance(value, Sequence) and not isinstance(
        value, (str, bytes, bytearray, memoryview)
    ):
        identity = id(value)
        if identity in seen:
            return "[CYCLE]"
        seen.add(identity)
        try:
            return [_json_safe(item, seen) for item in value]
        finally:
            seen.remove(identity)
    if isinstance(value, (bytes, bytearray, memoryview)):
        return "[BINARY]"
    return f"[{type(value).__name__}]"


def stable_redacted_json(value: Any, *, max_chars: int) -> str:
    """Serialize deterministically after recursively removing secret values."""

    safe = _json_safe(value, set())
    rendered = json.dumps(
        safe,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return _cap(_collapse(rendered), max_chars)


def normalize_apply_patch(patch: str, *, max_chars: int) -> str:
    """Return bounded file paths and added text from a Codex patch envelope."""

    if (
        not isinstance(patch, str)
        or not patch
        or len(patch) > _PATCH_SCAN_MAX_CHARS
    ):
        return ""
    lines = patch.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    if not lines or lines[0].strip() != "*** Begin Patch":
        return ""

    parts: list[str] = []
    kind: str | None = None
    saw_change = False
    saw_addition = False
    saw_move = False
    block_closed = False
    ended_at: int | None = None

    def current_valid() -> bool:
        if kind == "add":
            return saw_addition
        if kind == "update":
            return saw_change or saw_move
        return kind == "delete"

    for index, line in enumerate(lines[1:], start=1):
        if line == "*** End Patch":
            if kind is not None and not current_valid():
                return ""
            ended_at = index
            break

        header_kind: str | None = None
        header_prefix = ""
        for candidate, prefix in (
            ("add", "*** Add File: "),
            ("update", "*** Update File: "),
            ("delete", "*** Delete File: "),
        ):
            if line.startswith(prefix):
                header_kind = candidate
                header_prefix = prefix
                break
        if header_kind is not None:
            if kind is not None and not current_valid():
                return ""
            path = line[len(header_prefix) :].strip()
            if not path:
                return ""
            parts.append(path)
            kind = header_kind
            saw_change = False
            saw_addition = False
            saw_move = False
            block_closed = False
            continue

        if line.startswith("*** Move to: "):
            if kind != "update" or saw_move or saw_change or block_closed:
                return ""
            path = line[len("*** Move to: ") :].strip()
            if not path:
                return ""
            parts.append(path)
            saw_move = True
            continue

        if kind is None:
            return ""
        if line == "*** End of File":
            if kind != "update" or not saw_change or block_closed:
                return ""
            block_closed = True
            continue
        if block_closed:
            return ""
        if kind == "delete":
            return ""
        if kind == "add":
            if not line.startswith("+"):
                return ""
            parts.append(_safe_patch_line(line[1:]))
            saw_addition = True
            continue
        if line.startswith("@@"):
            saw_change = True
            continue
        if not line.startswith(("+", "-", " ")):
            return ""
        saw_change = True
        if line.startswith("+"):
            parts.append(_safe_patch_line(line[1:]))
            saw_addition = True

    if ended_at is None or any(line.strip() for line in lines[ended_at + 1 :]):
        return ""
    normalized = _collapse(" ".join(parts))
    return _cap(normalized, max_chars)


def _agent_text(tool_input: Mapping[str, Any]) -> str:
    parts: list[str] = []
    primary = tool_input.get("message") or tool_input.get("prompt")
    if isinstance(primary, str):
        parts.append(primary)
    items = tool_input.get("items")
    if isinstance(items, Sequence) and not isinstance(items, (str, bytes, bytearray)):
        for item in items:
            if (
                isinstance(item, Mapping)
                and item.get("type") == "text"
                and isinstance(item.get("text"), str)
            ):
                parts.append(item["text"])
    return " ".join(parts)


def normalize_tool_call(
    tool_name: str,
    tool_input: Mapping[str, Any],
    *,
    max_chars: int,
) -> str | None:
    """Build a recall query without logging or retaining unredacted arguments."""

    if not isinstance(tool_name, str) or not isinstance(tool_input, Mapping):
        return None
    if tool_name in {"Bash", "PowerShell"}:
        command = tool_input.get("command")
        raw = command if isinstance(command, str) else ""
        if _contains_secret_marker(raw):
            return _cap(_SECRET_COMMAND_MARKER, max_chars)
        return _cap(_collapse(raw), max_chars)
    if tool_name == "apply_patch":
        patch = tool_input.get("patch")
        raw = patch if isinstance(patch, str) else ""
        return normalize_apply_patch(raw, max_chars=max_chars)
    if tool_name in {"spawn_agent", "Agent"}:
        return _cap(_collapse(_redact_text(_agent_text(tool_input))), max_chars)

    rendered = stable_redacted_json(tool_input, max_chars=max_chars)
    if tool_name.startswith("mcp__"):
        return _cap(_collapse(f"{tool_name} {rendered}"), max_chars)
    return rendered
