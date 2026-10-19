"""Privacy-first normalization of OpenCode tool arguments for bounded recall."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

MAX_QUERY_CHARS = 4000
MAX_SOURCE_CHARS = 12000
_SECRET_MARKER = re.compile(
    r"(?i)(?:\b(?:authorization|bearer)\b|"
    r"\b(?:[a-z0-9]+[_-])*(?:api[_-]?key|token|password|passwd|secret|credential|client[_-]?secret)\b|"
    r"\bsetx\s+\S+\s+|"
    r"://[^:/\s]+:[^@/\s]+@)"
)
_PRIVATE_KEY = re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----", re.I)
_PRIVATE_KEY_BLOCK = re.compile(
    r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----[\s\S]*?(?:-----END (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----|$)", re.I
)
_SHELL_ALLOW = {
    "bash": re.compile(r"(?i)^(git\s+(?:status|diff|show|log|commit|push|merge|rebase|reset|cherry-pick|revert|tag)|(?:npm|pnpm|yarn|pip|uv)\s+(?:install|add|remove|uninstall)|docker\s+(?:push|deploy|run)|winget\s+(?:install|uninstall|upgrade)|claude\s+mcp\s+(?:add|remove)|gh\s+pr\s+(?:create|merge|close|edit)|\.?/.+\.(?:sh|ps1|py|bat|cmd))"),
    "powershell": re.compile(r"(?i)^(git\s+(?:status|diff|show|log|commit|push|merge|rebase|reset|cherry-pick|revert|tag)|(?:npm|pnpm|yarn|pip|uv)\s+(?:install|add|remove|uninstall)|docker\s+(?:push|deploy|run)|winget\s+(?:install|uninstall|upgrade)|claude\s+mcp\s+(?:add|remove)|gh\s+pr\s+(?:create|merge|close|edit)|\.?/.+\.(?:sh|ps1|py|bat|cmd))"),
}


@dataclass(frozen=True)
class HostContext:
    shell: str | None
    cwd: str
    boundary: str | None


@dataclass(frozen=True)
class NormalizedAction:
    tool: str
    query: str
    shell: str | None = None
    command: str | None = None


def _text(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _redact_line(line: str, *, patch: bool = False) -> str:
    if _PRIVATE_KEY.search(line):
        return "[REDACTED PATCH LINE]" if patch else "[REDACTED PRIVATE KEY]"
    if _SECRET_MARKER.search(line):
        return "[REDACTED PATCH LINE]" if patch else "[REDACTED]"
    return line


def _redact(text: str, *, patch: bool = False) -> str:
    # Scan full source before any size reduction. Added patch lines are redacted
    # as a unit if they contain a credential marker.
    text = _PRIVATE_KEY_BLOCK.sub("[REDACTED PRIVATE KEY]", text)
    lines = text.splitlines()
    out = []
    for line in lines:
        if patch and line.startswith("*** "):
            out.append(line)  # patch metadata is reduced to basenames downstream
            continue
        candidate = line[1:] if patch and line.startswith("+") and not line.startswith("+++") else line
        redacted = _redact_line(candidate, patch=patch and line.startswith("+") and not line.startswith("+++"))
        out.append(("+" if patch and line.startswith("+") and not line.startswith("+++") else "") + redacted)
    return "\n".join(out)


def _basename(value: Any) -> str:
    path = _text(value).replace("\\", "/").rstrip("/")
    return path.rsplit("/", 1)[-1][:160]


def _oversize(name: str) -> str:
    return f"{name} [oversize: source omitted]".strip()


def _bound_content(content: str, name: str, *, patch: bool = False) -> str:
    safe = _redact(content, patch=patch)
    if len(content) > MAX_SOURCE_CHARS or len(safe) > MAX_QUERY_CHARS * 3:
        return _oversize(name)
    return safe[:MAX_QUERY_CHARS]


def _patch_parts(patch: str) -> list[str]:
    files: list[tuple[str, list[str]]] = []
    current_name = "patch"
    current: list[str] = []
    for line in patch.splitlines():
        if line.startswith("*** ") and any(line.startswith(prefix) for prefix in ("*** Add File:", "*** Update File:", "*** Delete File:")):
            if current:
                files.append((current_name, current))
            current_name = line.split(":", 1)[-1].strip()
            current = []
        elif line.startswith("+") and not line.startswith("+++"):
            current.append(line)
    if current:
        files.append((current_name, current))
    # A small handful of file sections are enough to retain thematic signal;
    # downstream matching is one query, not one subprocess per file.
    return [f"{name}:\n" + "\n".join(lines[:80]) for name, lines in files[:6]]


def normalize_action(tool: str, args: Any, host: HostContext) -> NormalizedAction | None:
    if not isinstance(args, dict):
        return None
    name = tool.casefold() if isinstance(tool, str) else ""
    file_value = args.get("filePath", args.get("file_path", args.get("path", "")))
    basename = _basename(file_value)
    if name in {"edit", "write", "multiedit", "notebookedit"}:
        content = args.get("newString", args.get("new_string", args.get("content", args.get("new_source", ""))))
        if name == "multiedit":
            edits = args.get("edits", [])
            content = "\n".join(_text(e.get("newString", e.get("new_string"))) for e in edits if isinstance(e, dict))
        safe = _bound_content(_text(content), basename)
        return NormalizedAction(name, f"{basename} {safe}".strip())
    if name in {"apply_patch", "applypatch"}:
        # Inspect the complete source for credentials before retaining only a
        # small number of file sections/added lines.
        original = _text(args.get("patchText", args.get("patch", args.get("input", ""))))
        patch = _redact(original, patch=True)
        if len(original) > MAX_SOURCE_CHARS:
            return NormalizedAction("apply_patch", _oversize("patch"))
        parts = _patch_parts(patch)
        # `patch` was already scanned before section extraction; do not scan
        # projected filenames such as `secret.json` as if they were credentials.
        projected = "\n".join(parts)
        safe = _oversize("patch") if len(projected) > MAX_QUERY_CHARS * 3 else projected[:MAX_QUERY_CHARS]
        return NormalizedAction("apply_patch", safe)
    if name in {"task", "agent"}:
        body = _text(args.get("prompt", args.get("description", "")))
        safe = _bound_content(body, "task")
        return NormalizedAction(name, safe)
    if name in {"grep", "ripgrep", "search", "glob"}:
        query = " ".join(filter(None, (_text(args.get("pattern", args.get("query"))), _text(args.get("include")), basename)))
        return NormalizedAction(name, _bound_content(query, basename)) if query else None
    if name in {"bash", "shell", "powershell", "terminal"}:
        shell = host.shell if host.shell in {"bash", "powershell"} else None
        raw = _text(args.get("command"))
        if shell is None or not raw or not _SHELL_ALLOW[shell].search(raw.strip()):
            return None
        safe = _redact(raw)
        if len(raw) > MAX_SOURCE_CHARS:
            return NormalizedAction("shell", _oversize("command"), shell, "[OVERSIZE: command omitted]")
        safe = safe[:MAX_QUERY_CHARS]
        return NormalizedAction("shell", safe, shell, safe)
    return None
