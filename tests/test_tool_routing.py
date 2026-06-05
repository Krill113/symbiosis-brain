import json
import re

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
