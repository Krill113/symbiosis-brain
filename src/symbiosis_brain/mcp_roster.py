"""A shared, content-keyed cache for `claude mcp list` (plan A-2, 2026-09-04).

`claude mcp list` health-checks every configured server, which means it STARTS
every stdio server — including a second `symbiosis-brain serve` against the
live vault, whose start is not read-only when the index drifted. The
SessionStart hook ran it once per session (and again on a compaction after 60
minutes); on 04.09 a crashed terminal restarted five sessions and produced five
full polls in 2.5 minutes. The roster itself only feeds three `*-present`
routing gates — the reason for this cache is the bursts, not the gates.

One answer is shared between the sessions of one cwd, keyed on an EXPLICIT
snapshot of what the answer depends on, stored field by field so that a miss
names the field that moved (FIELDS below): cwd, the config dir, `claude
--version`, and the CONTENT — never the mtime — of every configuration layer we
can see. The TTL (a day, owner's decision 2026-09-04) insures only what we
cannot see: remote servers, managed config, whatever else the command consults.

NOT in the key, on purpose, and an UNVERIFIED ASSUMPTION: launch flags
(`--mcp-config`, `--strict-mcp-config`), interactivity, the parent's command
line. `claude mcp list` runs as a child of the hook; it inherits environment
and cwd, not the parent's argv, so a field it cannot see cannot change its
answer — it could only cause misses. Nobody has yet started a session with
`--mcp-config` and looked at the roster it produced. If that shows the flags
do reach the child, they go back into the key.

Known and accepted limits: `publish` snapshots AFTER the poll (~7 s), so a
config change inside that window pairs the old answer with the new key until
the TTL; the project entry is looked up by cwd, so if Claude Code keys it by
git root, a session started in a subdirectory sees it as absent; `.claude.json`
caught mid-write reads as unparsable and costs one extra poll.

Contract kept: the per-session file `brain-mcp-roster-<sid>` and its reader
`tool_routing._roster_set` do not change — a hit copies the shared answer under
the session's name. A roster without a single server line is never published,
shared or per-session: `_roster_set` would read it as "no roster" anyway, and a
shared copy would carry that emptiness to every new session for a day.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Callable, Optional

from symbiosis_brain.pre_action_config import _debug_log, _tmp_dir

EXIT_HIT = 0
"""The session has its roster: adopted from the shared answer (check) or
published from a fresh poll (publish)."""
EXIT_MISS = 10
"""check: no usable shared answer — the hook polls."""
EXIT_REJECTED = 11
"""publish: the poll printed no server line — the hook drops it. Any OTHER
non-zero code (python missing, a crash, `timeout`'s 124) the hook treats as
before this cache existed: poll, publish the per-session file directly."""

TTL_S = 24 * 3600

FIELDS: tuple[str, ...] = (
    "cwd",
    "config_dir",
    "claude_version",
    "claude_json.mcpServers",
    "claude_json.project.mcpServers",
    "claude_json.project.disabledMcpServers",
    "claude_json.project.enabledMcpjsonServers",
    "claude_json.project.disabledMcpjsonServers",
    "mcp_json",
    "settings.user.mcp",
    "settings.project.mcp",
    "settings.local.mcp",
    "plugins",
)
_PROJECT_KEYS = ("mcpServers", "disabledMcpServers", "enabledMcpjsonServers",
                 "disabledMcpjsonServers")
ABSENT = "absent"
_MISSING = object()

SESSION_PREFIX = "brain-mcp-roster-"
SHARED_PREFIX = "brain-mcp-shared-"
"""Deliberately NOT under SESSION_PREFIX: the hook sweeps `brain-mcp-roster-*`
at 60 minutes, and the shared answer is meant to live a day."""

_MSYS_DRIVE = re.compile(r"^/([A-Za-z])(/.*)?$")


# --- paths ---------------------------------------------------------------------

def _from_msys(p: str) -> str:
    """`/c/Repos/x` -> `C:/Repos/x` on Windows. MSYS converts a POSIX path
    argument before it reaches a native program; this covers the times it
    does not (MSYS_NO_PATHCONV, a value read from the environment)."""
    if os.name == "nt":
        m = _MSYS_DRIVE.match(p)
        if m:
            return f"{m.group(1).upper()}:{m.group(2) or '/'}"
    return p


def _fold(p: str) -> str:
    """One spelling per directory: case folded and forward slashes on Windows,
    where `.claude.json` stores `C:/Repos/x` and bash hands us `c:\\repos\\x`."""
    q = os.path.normcase(os.path.normpath(p))
    return q.replace("\\", "/") if os.name == "nt" else q


def _home(env: dict) -> Path:
    """The home `claude` itself uses. It is node: libuv's os.homedir() reads
    USERPROFILE on Windows and HOME elsewhere — Path.home() of THIS process is
    not the question, the environment we were handed is."""
    for var in (("USERPROFILE", "HOME") if os.name == "nt" else ("HOME",)):
        v = (env.get(var) or "").strip()
        if v:
            return Path(_from_msys(v))
    return Path.home()


def session_path(tmp: Path, session_id: str) -> Path:
    return Path(tmp) / f"{SESSION_PREFIX}{session_id}"


def shared_paths(tmp: Path, cwd: str) -> tuple[Path, Path]:
    """(answer, key) for one cwd. The slug is the folded real path, hashed, so
    two spellings of one directory share, and a path never lands in a name."""
    slug = hashlib.sha256(_fold(os.path.realpath(_from_msys(cwd))).encode("utf-8")).hexdigest()[:12]
    base = Path(tmp) / f"{SHARED_PREFIX}{slug}"
    return base, base.with_name(base.name + ".key")


# --- the snapshot --------------------------------------------------------------

def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:16]


def _digest_obj(obj) -> str:
    if obj is None:
        return ABSENT
    return _digest(json.dumps(obj, sort_keys=True, ensure_ascii=False,
                              separators=(",", ":")).encode("utf-8"))


def _file_token(path: Path) -> str:
    """A whole file that is entirely MCP configuration: its bytes decide."""
    try:
        return _digest(path.read_bytes())
    except FileNotFoundError:
        return ABSENT
    except OSError as e:
        return f"unreadable:{type(e).__name__}"


def _load(path: Path):
    """(parsed JSON, None) — or (None, token) when there is nothing to parse.
    The token of a broken file is its hash, so editing it still moves the key."""
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return None, ABSENT
    except OSError as e:
        return None, f"unreadable:{type(e).__name__}"
    try:
        return json.loads(raw.decode("utf-8-sig", errors="replace")), None
    except ValueError:
        return None, "unparsable:" + _digest(raw)


def _mcp_keys(path: Path) -> str:
    """Only the MCP-related top-level keys of a settings file: `theme` or a
    permission rule must not throw away a day's worth of roster."""
    obj, token = _load(path)
    if obj is None:
        return token
    if not isinstance(obj, dict):
        return _digest_obj(obj)
    sub = {k: v for k, v in obj.items() if "mcp" in k.lower()}
    return _digest_obj(sub) if sub else ABSENT


def _project_entry(projects, cwd: str) -> Optional[dict]:
    if not isinstance(projects, dict):
        return None
    wanted = {_fold(cwd), _fold(os.path.realpath(cwd))}
    for k, v in projects.items():
        if isinstance(k, str) and _fold(_from_msys(k)) in wanted:
            return v if isinstance(v, dict) else {}
    return None


def _claude_version(env: dict) -> str:
    """`claude --version`, 0.09 s measured. Resolved through shutil.which so
    Windows finds claude.exe or a .cmd shim the way a shell would."""
    exe = shutil.which("claude", path=env.get("PATH"))
    if not exe:
        return "unavailable"
    try:
        out = subprocess.run([exe, "--version"], capture_output=True,
                             encoding="utf-8", errors="replace", timeout=2)
    except (OSError, subprocess.SubprocessError):
        return "unavailable"
    return out.stdout.strip() or f"exit:{out.returncode}"


def snapshot(cwd: str, env: dict, *,
             version_probe: Optional[Callable[[dict], str]] = None) -> dict:
    """Every input `claude mcp list` is known to depend on, as FIELDS-ordered
    {field: short token}. Values are digests of CONTENT, never mtimes: the
    config file is rewritten for a hundred reasons that change no server."""
    cwd = _from_msys(cwd)
    home = _home(env)
    override = (env.get("CLAUDE_CONFIG_DIR") or "").strip()
    config_dir = Path(_from_msys(override)) if override else home / ".claude"
    claude_json = (config_dir if override else home) / ".claude.json"

    snap = dict.fromkeys(FIELDS, ABSENT)
    snap["cwd"] = _fold(os.path.realpath(cwd))
    snap["config_dir"] = _fold(str(config_dir))
    snap["claude_version"] = (version_probe or _claude_version)(env)

    cfg, token = _load(claude_json)
    if cfg is None or not isinstance(cfg, dict):
        for f in FIELDS:
            if f.startswith("claude_json."):
                snap[f] = token or _digest_obj(cfg)
    else:
        snap["claude_json.mcpServers"] = _digest_obj(cfg.get("mcpServers"))
        entry = _project_entry(cfg.get("projects"), cwd)
        for k in _PROJECT_KEYS:
            snap[f"claude_json.project.{k}"] = (
                _digest_obj(entry.get(k)) if entry is not None else ABSENT)

    project = Path(cwd)
    snap["mcp_json"] = _file_token(project / ".mcp.json")
    snap["settings.user.mcp"] = _mcp_keys(config_dir / "settings.json")
    snap["settings.project.mcp"] = _mcp_keys(project / ".claude" / "settings.json")
    snap["settings.local.mcp"] = _mcp_keys(project / ".claude" / "settings.local.json")
    snap["plugins"] = _file_token(config_dir / "plugins" / "installed_plugins.json")
    return snap


def compare(a: dict, b: dict) -> Optional[str]:
    """The first field, in FIELDS order, on which the two snapshots differ —
    a field present on one side only counts. None when they agree."""
    for f in FIELDS:
        if a.get(f, _MISSING) != b.get(f, _MISSING):
            return f
    return None


# --- check / publish -------------------------------------------------------------

def _has_server_line(text: str) -> bool:
    """Mirrors tool_routing._roster_set exactly (a line containing ": "), which
    is not ours to change; a test pins the two together."""
    return any(": " in ln for ln in text.splitlines())


def _atomic_write(path: Path, data: bytes) -> None:
    """tmp in the same directory + os.replace: a concurrent reader sees the
    old file or the new one, never half of one."""
    tmp = path.with_name(f"{path.name}.tmp.{os.getpid()}")
    try:
        tmp.write_bytes(data)
        os.replace(tmp, path)
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def _miss(reason: str) -> tuple[int, str]:
    _debug_log(f"mcp_roster: miss {reason}")
    return EXIT_MISS, reason


def check(session_id: str, cwd: str, env: dict, *, tmp: Optional[Path] = None,
          version_probe: Optional[Callable[[dict], str]] = None) -> tuple[int, str]:
    """Adopt the shared answer for this session if it is fresh and its key
    matches the current snapshot; otherwise say why not.

    Order of reads matters: the key first, the answer second — publish writes
    them the other way round, so a reader whose key matches finds the answer
    that belongs to it (or a newer one)."""
    tmp = Path(tmp) if tmp is not None else _tmp_dir()
    shared, key_path = shared_paths(tmp, cwd)
    try:
        age = time.time() - shared.stat().st_mtime
        stored = json.loads(key_path.read_bytes().decode("utf-8", errors="replace"))
    except FileNotFoundError:
        return _miss("absent")
    except (OSError, ValueError):
        return _miss("key-unreadable")
    if not isinstance(stored, dict):
        return _miss("key-unreadable")
    if age > TTL_S:
        return _miss("ttl")
    moved = compare(stored, snapshot(cwd, env, version_probe=version_probe))
    if moved is not None:
        return _miss(moved)
    try:
        _atomic_write(session_path(tmp, session_id), shared.read_bytes())
    except OSError:
        return _miss("adopt-failed")
    _debug_log("mcp_roster: hit")
    return EXIT_HIT, "hit"


def publish(session_id: str, cwd: str, roster: str, env: dict, *,
            tmp: Optional[Path] = None,
            version_probe: Optional[Callable[[dict], str]] = None) -> int:
    """Store one fresh poll: the session's own file, the shared answer, and —
    LAST — the key that makes the answer findable. Nothing at all when the
    poll printed no server line."""
    if not _has_server_line(roster):
        _debug_log("mcp_roster: rejected — the poll printed no server line")
        return EXIT_REJECTED
    tmp = Path(tmp) if tmp is not None else _tmp_dir()
    data = roster.encode("utf-8")
    _atomic_write(session_path(tmp, session_id), data)
    shared, key_path = shared_paths(tmp, cwd)
    key = json.dumps(snapshot(cwd, env, version_probe=version_probe),
                     ensure_ascii=False, indent=1).encode("utf-8")
    _atomic_write(shared, data)
    _atomic_write(key_path, key)
    _debug_log("mcp_roster: published")
    return EXIT_HIT


def run(argv: list[str]) -> int:
    """`python -m symbiosis_brain mcp-roster check|publish …` — the hook's side."""
    p = argparse.ArgumentParser(prog="symbiosis-brain mcp-roster")
    sub = p.add_subparsers(dest="cmd", required=True)
    for name in ("check", "publish"):
        s = sub.add_parser(name)
        s.add_argument("--session-id", required=True)
        s.add_argument("--cwd", required=True)
        if name == "publish":
            s.add_argument("--from", dest="source", required=True,
                           help="file holding the `claude mcp list` output")
    a = p.parse_args(argv)
    env = dict(os.environ)
    if a.cmd == "check":
        return check(a.session_id, a.cwd, env)[0]
    text = Path(a.source).read_bytes().decode("utf-8", errors="replace")
    return publish(a.session_id, a.cwd, text, env)
