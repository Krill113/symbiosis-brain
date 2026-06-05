"""Stage-4 tool-routing engine (C3). Fail-open everywhere.

Loads a package-default routing catalog (``data/tool-routing.json``, a BARE
top-level JSON array) merged with an optional per-vault override
(``tool-routing.local.json``) by ``id``, compiles each route's regex triggers,
evaluates a ``when``-gate (platform / skill / catalog / scope / MCP-roster), and
matches a prompt against the routes — capping the result to the top-K by
priority. Augment-class matches are session-deduped via a prefixed ``SeenStore``.
Tier-0 ``route_fired`` telemetry is appended as JSONL.

Every step is fail-open: a bad regex skips only THAT route, a missing/corrupt
catalog yields no routes, and any error in match/dedup/append degrades to a
silent no-op — nothing here ever raises into the hook path.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from symbiosis_brain.pre_action_config import _debug_log, _tmp_dir

_DEFAULT_JSON = Path(__file__).with_name("data") / "tool-routing.json"
_LOCAL_BASENAME = "tool-routing.local.json"
ROUTE_SEEN_PREFIX = "brain-route-seen-"
ROUTE_EVENTS_PREFIX = "brain-route-events-"
ROUTING_CAP = 2
_SNIPPET_MAX = 60
_OBSERVABLE_TOOLS = {"Task", "Edit", "Write", "MultiEdit", "NotebookEdit", "Bash"}


@dataclass
class Route:
    id: str
    cls: str
    triggers: list[re.Pattern]
    hint: str
    priority: int = 50
    when: Optional[str] = None
    expected_tool: Optional[str] = None
    observable: bool = False
    chain: list[str] = field(default_factory=list)
    trial: bool = False


def _compile_flags(flags: str) -> int:
    out = 0
    if "i" in (flags or ""):
        out |= re.IGNORECASE
    if "s" in (flags or ""):
        out |= re.DOTALL
    if "m" in (flags or ""):
        out |= re.MULTILINE
    return out


def _compile_route(raw: dict[str, Any]) -> Optional[Route]:
    rid = raw.get("id")
    if not rid or not isinstance(rid, str):
        return None
    pats: list[re.Pattern] = []
    for t in raw.get("triggers") or []:
        try:
            pats.append(re.compile(t["re"], _compile_flags(t.get("flags", ""))))
        except (re.error, KeyError, TypeError) as e:
            # Fail-open: a single bad regex skips THIS route only, others work.
            _debug_log(f"tool_routing: bad regex in route {rid!r}: {e}")
            return None
    if not pats:
        return None
    cls = raw.get("class", "augment")
    if cls not in ("augment", "supersede"):
        cls = "augment"
    return Route(
        id=rid,
        cls=cls,
        triggers=pats,
        hint=str(raw.get("hint", "")),
        priority=int(raw.get("priority", 50)),
        when=raw.get("when") or None,
        expected_tool=raw.get("expected_tool") or None,
        observable=bool(raw.get("observable", False)),
        chain=list(raw.get("chain") or []),
        trial=bool(raw.get("trial", False)),
    )


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError) as e:
        _debug_log(f"tool_routing: read failed {path.name}: {e}")
        return None


def _as_route_list(data: Any) -> list:
    """Accept BOTH a bare top-level array (canonical) and a {"routes":[...]}
    wrapper so the shipped data file, local overrides, and the /brain-tools
    skill output are all tolerated (Shared contracts)."""
    if isinstance(data, list):
        return data
    if isinstance(data, dict) and isinstance(data.get("routes"), list):
        return data["routes"]
    return []


def _merge_raw(default: Any, local: Any) -> list[dict]:
    """Merge default ∪ local by ``id`` preserving catalog order.

    Local override semantics: new id → add; ``{"id":X,"disabled":true}`` → drop;
    existing id + fields → shallow-update."""
    by_id: dict[str, dict] = {}
    order: list[str] = []
    for r in _as_route_list(default):
        if isinstance(r, dict) and r.get("id"):
            by_id[r["id"]] = dict(r)
            order.append(r["id"])
    for r in _as_route_list(local):
        if not isinstance(r, dict):
            continue
        rid = r.get("id")
        if not rid:
            continue
        if r.get("disabled") is True:
            by_id.pop(rid, None)
            continue
        if rid in by_id:
            by_id[rid].update(r)
        else:
            by_id[rid] = dict(r)
            order.append(rid)
    return [by_id[i] for i in order if i in by_id]


def load_routes(
    vault: Optional[Path] = None, default_path: Path = _DEFAULT_JSON
) -> list[Route]:
    default = _read_json(default_path)
    if not _as_route_list(default):
        _debug_log("tool_routing: default catalog missing/empty — routing silent")
        default = []
    local = None
    if vault is not None:
        lp = Path(vault) / _LOCAL_BASENAME
        if lp.exists():
            local = _read_json(lp)
    routes: list[Route] = []
    for raw in _merge_raw(default, local):
        r = _compile_route(raw)
        if r is None:
            continue
        if r.observable and r.expected_tool and r.expected_tool not in _OBSERVABLE_TOOLS:
            _debug_log(
                f"tool_routing: {r.id!r} observable but expected_tool "
                f"{r.expected_tool!r} not in matcher set"
            )
        routes.append(r)
    return routes


def _roster_set(session_id: str) -> Optional[set[str]]:
    if not session_id:
        return None
    p = _tmp_dir() / f"brain-mcp-roster-{session_id}"
    try:
        txt = p.read_text(encoding="utf-8")
    except OSError:
        return None
    names = {ln.strip().lower() for ln in txt.splitlines() if ln.strip()}
    return names or None


def _gate_token(token, *, roster, scope, vault) -> Optional[bool]:
    token = token.strip()
    if token == "platform:windows":
        return os.name == "nt" or "win" in os.environ.get("OSTYPE", "").lower()
    if token == "catalog-present":
        return Path(".claude/docs/catalog").is_dir()
    if token.startswith("scope:"):
        return (scope or "") == token.split(":", 1)[1]
    if token.startswith("skill:") and token.endswith("-present"):
        name = token[len("skill:") : -len("-present")]
        return (Path.home() / ".claude" / "skills" / name).is_dir()
    if token.endswith("-present"):
        name = token[: -len("-present")].split(":", 1)[0].lower()
        if roster is None:
            return None
        return any(name in r for r in roster)
    _debug_log(f"tool_routing: unknown when-token {token!r}")
    return None


def _when_ok(when, *, roster, scope, vault) -> bool:
    if not when:
        return True
    for tok in when.split("&"):
        res = _gate_token(tok, roster=roster, scope=scope, vault=vault)
        if res is None:
            # Undeterminable (e.g. cold MCP roster) → fail-closed for this route,
            # so a gated route stays silent rather than firing without its tool.
            _debug_log(f"tool_routing: gate undeterminable for {tok!r}")
            return False
        if res is False:
            return False
    return True


def match_routes(prompt, routes, *, roster=None, scope=None, vault=None, cap=ROUTING_CAP):
    if not prompt:
        return []
    fired = []
    for idx, r in enumerate(routes):
        if not any(p.search(prompt) for p in r.triggers):
            continue
        if not _when_ok(r.when, roster=roster, scope=scope, vault=vault):
            continue
        fired.append((idx, r))
    # Cap top-K: priority DESC, deterministic tie-break by catalog order (idx ASC).
    fired.sort(key=lambda t: (-t[1].priority, t[0]))
    return [r for _, r in fired[:cap]]


def route_hints(matched):
    return [{"id": r.id, "class": r.cls, "hint": r.hint} for r in matched]


def dedup_augment(matched, session_id, *, ttl_seconds=10 ** 9):
    aug = [r for r in matched if r.cls == "augment"]
    sup = [r for r in matched if r.cls == "supersede"]
    if not aug or not session_id:
        return matched
    try:
        from symbiosis_brain.recall_dedup import SeenStore

        seen = SeenStore(session_id, ttl_seconds=ttl_seconds, prefix=ROUTE_SEEN_PREFIX)
        fresh = [r for r in aug if not seen.is_seen(r.id)]
        seen.record(r.id for r in fresh)
    except Exception:
        # Fail-open: dedup is best-effort — on any error, keep all matches.
        return matched
    keep = {r.id for r in fresh} | {r.id for r in sup}
    return [r for r in matched if r.id in keep]


def append_route_fired(
    session_id, matched, *, monotonic_turn, routing_mode, rules_emitted, prompt
):
    if not matched or not session_id:
        return
    path = _tmp_dir() / f"{ROUTE_EVENTS_PREFIX}{session_id}.jsonl"
    snippet = (prompt or "")[:_SNIPPET_MAX]
    # Timezone-aware ISO-8601 — matches __main__._append_route_events so the
    # single brain-route-events log has ONE ts format (human-scannable Tier-0).
    ts = datetime.now(timezone.utc).isoformat()
    try:
        with path.open("a", encoding="utf-8") as f:
            for r in matched:
                f.write(
                    json.dumps(
                        {
                            "session_id": session_id,
                            "ts": ts,
                            "monotonic_turn": monotonic_turn,
                            "event": "route_fired",
                            "route_id": r.id,
                            "expected_tool": r.expected_tool,
                            "observable": r.observable,
                            "routing_mode": routing_mode,
                            "rules_emitted": rules_emitted,
                            "prompt_snippet": snippet,
                        },
                        ensure_ascii=False,
                    )
                    + "\n"
                )
    except OSError:
        pass
