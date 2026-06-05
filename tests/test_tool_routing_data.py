"""Data-integrity tests for the shipped tool-routing.json seed catalog."""
import json
import re

from symbiosis_brain.pre_action_config import routing_default_path
import symbiosis_brain.tool_routing as tr

SEEN_IDS = {
    "web-research-dual-engine",
    "version-date-from-registry",
    "powershell-on-windows",
    "playwright-escalation",
    "systematic-debugging",
    "catalog-discovery",
    "serena-symbol-work",
}
MATCHER_SET = {"Task", "Edit", "Write", "MultiEdit", "NotebookEdit", "Bash"}


def test_default_tool_routing_loads_and_compiles():
    path = routing_default_path()
    assert path.exists(), f"shipped catalog missing at {path}"
    routes = tr._as_route_list(json.loads(path.read_text(encoding="utf-8")))
    ids = {r["id"] for r in routes}
    assert ids == SEEN_IDS
    for r in routes:
        assert r["class"] in ("augment", "supersede")
        assert isinstance(r["priority"], int)
        for trig in r["triggers"]:
            re.compile(trig["re"])
            assert "<" not in trig["re"] and ">" not in trig["re"]
        exp = r.get("expected_tool")
        assert bool(r.get("observable")) == (exp in MATCHER_SET)


def test_load_routes_count():
    """load_routes() must return exactly 7 compiled Route objects."""
    routes = tr.load_routes(vault=None)
    assert len(routes) == 7, f"expected 7, got {len(routes)}: {[r.id for r in routes]}"


def test_all_triggers_compile():
    """Every trigger regex in the catalog must compile without error."""
    path = routing_default_path()
    raw = tr._as_route_list(json.loads(path.read_text(encoding="utf-8")))
    for route in raw:
        for trig in route["triggers"]:
            try:
                re.compile(trig["re"])
            except re.error as e:
                raise AssertionError(f"regex compile error in {route['id']!r}: {e}") from e


def test_priority_ranks_registry_first():
    """Priority reconciliation: R6=80, R2=75, R1=70, R9=65, R7=60, R4=55, R8=50."""
    routes = tr._as_route_list(
        json.loads(routing_default_path().read_text(encoding="utf-8"))
    )
    pri = {r["id"]: r["priority"] for r in routes}
    assert pri["version-date-from-registry"] == 80
    assert pri["serena-symbol-work"] == 75
    assert pri["web-research-dual-engine"] == 70
    assert pri["systematic-debugging"] == 65
    assert pri["powershell-on-windows"] == 60
    assert pri["catalog-discovery"] == 55
    assert pri["playwright-escalation"] == 50
    # Registry (R6) ranks above web dual-engine (R1)
    assert pri["version-date-from-registry"] > pri["web-research-dual-engine"]


def test_r1_gate_is_duckduckgo_present():
    """R1 (web-research-dual-engine) MUST gate on duckduckgo-present, not ddg-present."""
    routes = tr._as_route_list(
        json.loads(routing_default_path().read_text(encoding="utf-8"))
    )
    r1 = next(r for r in routes if r["id"] == "web-research-dual-engine")
    assert r1.get("when") == "duckduckgo-present", (
        f"R1 gate is {r1.get('when')!r} — must be 'duckduckgo-present'"
    )
