import json
import re
from pathlib import Path

import symbiosis_brain.tool_routing as tr


def _r(i, p):
    return tr.Route(id=i, cls="augment", triggers=[re.compile("X")], hint="h", priority=p)


def test_three_match_keep_two():
    m = tr.match_routes("X", [_r("a", 80), _r("b", 70), _r("c", 60)], cap=2)
    assert [r.id for r in m] == ["a", "b"]


def test_snippet_cap(tmp_path, monkeypatch):
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    r = tr.Route(id="x", cls="augment", triggers=[re.compile("a")], hint="h")
    tr.append_route_fired(
        "sess",
        [r],
        monotonic_turn=3,
        routing_mode="decompose",
        rules_emitted=False,
        prompt="a" * 200,
    )
    line = json.loads(
        (tmp_path / "brain-route-events-sess.jsonl").read_text(encoding="utf-8").strip()
    )
    assert len(line["prompt_snippet"]) == 60 and line["monotonic_turn"] == 3


# --- contract: route_fired JSONL line carries the locked event shape ---
def test_route_fired_event_shape(tmp_path, monkeypatch):
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    r = tr.Route(
        id="x", cls="augment", triggers=[re.compile("a")], hint="h",
        expected_tool="WebSearch", observable=False,
    )
    tr.append_route_fired(
        "sess", [r], monotonic_turn=7, routing_mode="additive",
        rules_emitted=True, prompt="hello",
    )
    line = json.loads(
        (tmp_path / "brain-route-events-sess.jsonl").read_text(encoding="utf-8").strip()
    )
    assert set(line) == {
        "session_id", "ts", "monotonic_turn", "event", "route_id",
        "expected_tool", "observable", "routing_mode", "rules_emitted",
        "prompt_snippet",
    }
    assert line["event"] == "route_fired"
    assert line["route_id"] == "x"
    assert line["expected_tool"] == "WebSearch"
    assert line["routing_mode"] == "additive"
    assert line["rules_emitted"] is True
    # ts is timezone-aware ISO-8601 (string) — same format as
    # __main__._append_route_events so the single events log has one ts shape.
    assert isinstance(line["ts"], str)
    from datetime import datetime
    parsed_ts = datetime.fromisoformat(line["ts"])
    assert parsed_ts.tzinfo is not None


# --- cap is deterministic by priority DESC then catalog order ---
def test_cap_tie_break_by_catalog_order():
    routes = [_r("a", 70), _r("b", 70), _r("c", 70)]
    m = tr.match_routes("X", routes, cap=2)
    assert [r.id for r in m] == ["a", "b"]


# --- fail-open: a bad regex skips ONLY that route, others still load ---
def test_bad_regex_skips_only_that_route(tmp_path):
    path = tmp_path / "tool-routing.json"
    path.write_text(
        json.dumps([
            {"id": "good", "triggers": [{"re": "ok"}], "hint": "h"},
            {"id": "bad", "triggers": [{"re": "(unclosed"}], "hint": "h"},
        ]),
        encoding="utf-8",
    )
    ids = {r.id for r in tr.load_routes(default_path=path)}
    assert ids == {"good"}


# --- fail-open: missing catalog → empty list, no raise ---
def test_missing_catalog_empty(tmp_path):
    assert tr.load_routes(default_path=tmp_path / "nope.json") == []


# --- fail-open: malformed JSON → empty list, no raise ---
def test_bad_json_empty(tmp_path):
    path = tmp_path / "tool-routing.json"
    path.write_text("{not json", encoding="utf-8")
    assert tr.load_routes(default_path=path) == []


# --- {"routes":[...]} wrapper is tolerated alongside the bare array ---
def test_routes_wrapper_tolerated(tmp_path):
    path = tmp_path / "tool-routing.json"
    path.write_text(
        json.dumps({"routes": [{"id": "w", "triggers": [{"re": "x"}], "hint": "h"}]}),
        encoding="utf-8",
    )
    assert [r.id for r in tr.load_routes(default_path=path)] == ["w"]


# --- local override merges by id: add + disable ---
def test_local_override_add_and_disable(tmp_path):
    default = tmp_path / "tool-routing.json"
    default.write_text(
        json.dumps([
            {"id": "keep", "triggers": [{"re": "k"}], "hint": "h"},
            {"id": "drop", "triggers": [{"re": "d"}], "hint": "h"},
        ]),
        encoding="utf-8",
    )
    (tmp_path / "tool-routing.local.json").write_text(
        json.dumps([
            {"id": "drop", "disabled": True},
            {"id": "added", "triggers": [{"re": "a"}], "hint": "local"},
        ]),
        encoding="utf-8",
    )
    ids = {r.id for r in tr.load_routes(vault=tmp_path, default_path=default)}
    assert ids == {"keep", "added"}


# --- when-gate: cold MCP roster (None) keeps a gated route silent ---
def test_gate_silent_when_roster_cold():
    r = tr.Route(
        id="g", cls="augment", triggers=[re.compile("x")], hint="h",
        when="serena-present",
    )
    assert tr.match_routes("x", [r], roster=None) == []
    assert [m.id for m in tr.match_routes("x", [r], roster={"serena"})] == ["g"]


# --- when-gate: substring match against roster (duckduckgo, not ddg) ---
def test_gate_substring_match():
    r = tr.Route(
        id="g", cls="augment", triggers=[re.compile("x")], hint="h",
        when="duckduckgo-present",
    )
    assert [m.id for m in tr.match_routes("x", [r], roster={"duckduckgo"})] == ["g"]
    assert tr.match_routes("x", [r], roster={"serena"}) == []


# --- dedup_augment: augment routes seen once per session, supersede kept ---
def test_dedup_augment(tmp_path, monkeypatch):
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    aug = tr.Route(id="a", cls="augment", triggers=[re.compile("x")], hint="h")
    sup = tr.Route(id="s", cls="supersede", triggers=[re.compile("x")], hint="h")
    first = tr.dedup_augment([aug, sup], "sess")
    assert {r.id for r in first} == {"a", "s"}
    # second turn: augment already seen → dropped; supersede always kept
    second = tr.dedup_augment([aug, sup], "sess")
    assert [r.id for r in second] == ["s"]


# --- route_hints serializer shape ---
def test_route_hints_shape():
    r = tr.Route(id="x", cls="supersede", triggers=[re.compile("x")], hint="do it")
    assert tr.route_hints([r]) == [{"id": "x", "class": "supersede", "hint": "do it"}]


# ──────────────────────────────────────────────────────────────────────────────
# Data-backed tests (Task 4) — require tool-routing.json to exist
# ──────────────────────────────────────────────────────────────────────────────

def test_seven_compile():
    """Seed catalog must load exactly 7 compiled routes."""
    assert len(tr.load_routes(vault=None)) == 7


def test_no_angle_placeholder():
    """No <angle> placeholders in any trigger regex."""
    from pathlib import Path
    raw = tr._as_route_list(
        json.loads(Path(tr._DEFAULT_JSON).read_text(encoding="utf-8"))
    )
    for route in raw:
        for t in route["triggers"]:
            assert "<" not in t["re"] and ">" not in t["re"], route["id"]
            re.compile(t["re"])


def test_named_symbol_fires():
    """R2 (serena-symbol-work) fires when prompt contains a named symbol token."""
    m = tr.match_routes(
        "переименуй FooBar в BarBaz",
        tr.load_routes(vault=None),
        roster={"serena"},
    )
    assert any(r.id == "serena-symbol-work" for r in m)


def test_bare_this_silent():
    """R2 (serena-symbol-work) must NOT fire on pronouns — requires a symbol token."""
    m = tr.match_routes(
        "переименуй это",
        tr.load_routes(vault=None),
        roster={"serena"},
    )
    assert not any(r.id == "serena-symbol-work" for r in m)


def test_disable_and_add(tmp_path):
    """Local override: disable an existing route, add a new one."""
    (tmp_path / "tool-routing.local.json").write_text(
        json.dumps([
            {"id": "playwright-escalation", "disabled": True},
            {
                "id": "civilbridge-live",
                "class": "augment",
                "priority": 90,
                "triggers": [{"re": "civil3d", "flags": "i"}],
                "hint": "local",
            },
        ]),
        encoding="utf-8",
    )
    ids = {r.id for r in tr.load_routes(vault=tmp_path)}
    assert "playwright-escalation" not in ids and "civilbridge-live" in ids


def test_silent_when_roster_none():
    """R1 (web-research-dual-engine) stays silent when roster is None (cold MCP)."""
    m = tr.match_routes(
        "поищи в сети про uv lockfile",
        tr.load_routes(vault=None),
        roster=None,
    )
    assert not any(r.id == "web-research-dual-engine" for r in m)


def test_fires_when_present():
    """R1 (web-research-dual-engine) fires when duckduckgo is in the roster."""
    m = tr.match_routes(
        "поищи в сети про uv lockfile",
        tr.load_routes(vault=None),
        roster={"duckduckgo"},
    )
    assert any(r.id == "web-research-dual-engine" for r in m)


def test_latest_ruff():
    """R6 (version-date-from-registry) fires first and R1 is absent for version queries."""
    m = tr.match_routes(
        "latest version of ruff",
        tr.load_routes(vault=None),
        roster={"duckduckgo"},
    )
    assert m and m[0].id == "version-date-from-registry"
    assert not any(r.id == "web-research-dual-engine" for r in m)


# --- Stage 4b local.json routes (fixture-backed gate/match tests) ---
_FIXTURE = Path(__file__).parent / "fixtures" / "stage4b-local-routes.json"


def _routes_with_fixture(tmp_path):
    """load_routes merging the shipped default with the stage4b fixture as local.json."""
    (tmp_path / tr._LOCAL_BASENAME).write_text(
        _FIXTURE.read_text(encoding="utf-8"), encoding="utf-8"
    )
    return tr.load_routes(vault=tmp_path)


def test_stage4b_fixture_routes_compile(tmp_path):
    # Explicit JSON-escape + regex validation: catches a mis-escaped trigger at
    # the JSON level BEFORE load_routes' fail-open silently drops the route.
    raw = json.loads(_FIXTURE.read_text(encoding="utf-8"))
    assert isinstance(raw, list) and len(raw) == 3
    for route in raw:
        for trig in route["triggers"]:
            re.compile(trig["re"])  # raises on bad regex / mis-escape
    routes = _routes_with_fixture(tmp_path)
    ids = {r.id for r in routes}
    assert {"serena-code-work", "civil3d-bridge-analysis", "debug-civil3d-plugin"} <= ids


def test_serena_symbol_and_code_work_are_disjoint(tmp_path):
    # Verified against the live engine (2026-06-08): the two serena routes never
    # both fire on one prompt, so ROUTING_CAP=2 never has to choose between them.
    routes = _routes_with_fixture(tmp_path)
    rename = [r.id for r in tr.match_routes("переименуй FooBar везде", routes, roster={"serena"})]
    assert "serena-symbol-work" in rename and "serena-code-work" not in rename
    structure = [r.id for r in tr.match_routes(
        "покажи структуру класса PipeNetworkManager", routes, roster={"serena"})]
    assert "serena-code-work" in structure and "serena-symbol-work" not in structure


def test_serena_code_work_fires_with_serena_silent_without(tmp_path):
    routes = _routes_with_fixture(tmp_path)
    prompt = "покажи структуру класса PipeNetworkManager"
    assert any(r.id == "serena-code-work"
               for r in tr.match_routes(prompt, routes, roster={"serena"}))
    # serena absent (cold roster) → silent (fail-closed)
    assert not any(r.id == "serena-code-work"
                   for r in tr.match_routes(prompt, routes, roster=None))


def test_serena_code_work_does_not_collide_with_debugging(tmp_path):
    routes = _routes_with_fixture(tmp_path)
    # A pure-debugging prompt must NOT trigger serena-code-work.
    m = tr.match_routes("почему падает тест", routes, roster={"serena"})
    assert not any(r.id == "serena-code-work" for r in m)


def test_civil3d_route_requires_scope_and_bridge(tmp_path):
    routes = _routes_with_fixture(tmp_path)
    prompt = "снуп handle 1A2B в чертеже"
    assert any(r.id == "civil3d-bridge-analysis"
               for r in tr.match_routes(prompt, routes,
                                        scope="civil3d", roster={"civil3d-bridge"}))
    # wrong scope → silent
    assert not any(r.id == "civil3d-bridge-analysis"
                   for r in tr.match_routes(prompt, routes,
                                            scope="global", roster={"civil3d-bridge"}))
    # bridge absent → silent
    assert not any(r.id == "civil3d-bridge-analysis"
                   for r in tr.match_routes(prompt, routes,
                                            scope="civil3d", roster={"serena"}))


def test_debug_route_requires_vs_mcp(tmp_path):
    routes = _routes_with_fixture(tmp_path)
    prompt = "поставь breakpoint в CreateProfileViewsRun"
    matched = tr.match_routes(prompt, routes, roster={"vs-mcp"})
    assert any(r.id == "debug-civil3d-plugin" for r in matched)
    dbg = next(r for r in matched if r.id == "debug-civil3d-plugin")
    assert dbg.chain == ["systematic-debugging", "vs-mcp", "civil3d-bridge"]
    # vs-mcp absent → silent
    assert not any(r.id == "debug-civil3d-plugin"
                   for r in tr.match_routes(prompt, routes, roster=None))
