import json

from symbiosis_brain.adapters.opencode.routing import load_action_descriptors, match_opencode_routes


def _catalog(vault, routes):
    (vault / "tool-routing.local.json").write_text(json.dumps(routes), encoding="utf-8")


def test_routes_use_actual_capabilities_and_unknown_when_fails_closed(tmp_path):
    vault = tmp_path / "vault"
    cwd = tmp_path / "work"
    vault.mkdir()
    cwd.mkdir()
    _catalog(vault, [
        {"id": "available", "class": "augment", "when": "mcp:search-present",
         "priority": 9, "triggers": [{"re": "find the report"}], "hint": "Use allowed search."},
        {"id": "unknown", "class": "augment", "when": "new-predicate",
         "priority": 10, "triggers": [{"re": "find the report"}], "hint": "Must not fire."},
        {"id": "disabled", "class": "augment", "when": "mcp:offline-present",
         "triggers": [{"re": "find the report"}], "hint": "Unavailable."},
        {"id": "permission-denied", "class": "augment", "when": "mcp:search-present",
         "expected_tool": "mcp__search__query", "triggers": [{"re": "find the report"}], "hint": "Not permitted."},
    ])
    blocks = match_opencode_routes("find the report", {"mcp": ["search"], "tools": [], "skills": []},
                                   "alpha", cwd, vault)
    assert [block.id for block in blocks] == ["route:available"]


def test_package_skill_and_catalog_gates_are_query_cwd_bound(tmp_path):
    vault = tmp_path / "vault"
    cwd = tmp_path / "project"
    other = tmp_path / "other"
    vault.mkdir()
    cwd.mkdir()
    other.mkdir()
    (cwd / ".claude" / "docs" / "catalog").mkdir(parents=True)
    (cwd / ".claude" / "skills" / "external-only").mkdir(parents=True)
    _catalog(vault, [
        {"id": "skill", "class": "augment", "when": "skill:packaged-guide-present",
         "triggers": [{"re": "how does it work"}], "hint": "Packaged only."},
        {"id": "external-only", "class": "augment", "when": "skill:external-only-present",
         "triggers": [{"re": "how does it work"}], "hint": "Must remain unavailable."},
        {"id": "catalog", "class": "augment", "when": "catalog-present",
         "triggers": [{"re": "how does it work"}], "hint": "Project catalog."},
    ])
    capabilities = {"tools": [], "mcp": [], "skills": ["packaged-guide"]}
    assert {b.id for b in match_opencode_routes("how does it work", capabilities, "alpha", cwd, vault)} == {"route:skill", "route:catalog"}
    assert {b.id for b in match_opencode_routes("how does it work", capabilities, "alpha", other, vault)} == {"route:skill"}


def test_action_descriptors_are_read_from_compiled_tsv_and_filtered_by_catalog(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    _catalog(vault, [
        {"id": "safe", "class": "action", "when": "mcp:deploy-present", "priority": 7,
         "command_triggers": {"bash": [{"re": "^deploy"}]}, "hint": "Only if available."},
        {"id": "off", "class": "action", "when": "mcp:offline-present",
         "command_triggers": {"bash": [{"re": "^off"}]}, "hint": "Disabled."},
    ])
    index = vault / ".index"
    index.mkdir()
    (index / "action-rules.tsv").write_text("bash\tsafe\t^deploy\tOnly if available.\nbash\toff\t^off\tDisabled.\n", encoding="utf-8")
    caps = {"tools": [], "mcp": ["deploy"], "skills": []}
    assert load_action_descriptors(vault, caps, "alpha") == [
        {"id": "safe", "shell": "bash", "pattern": "^deploy", "hint": "Only if available.",
         "priority": 7, "capabilityRevision": 0}
    ]


def test_unknown_namespace_fails_closed_with_diagnostic_for_prompt_and_action(tmp_path):
    vault = tmp_path / "vault"
    cwd = tmp_path / "project"
    vault.mkdir()
    cwd.mkdir()
    _catalog(vault, [
        {"id": "unknown-prompt", "class": "augment", "when": "unsupported:search-present",
         "triggers": [{"re": "find report"}], "hint": "Must stay off."},
        {"id": "unknown-action", "class": "action", "when": "unsupported:deploy-present",
         "command_triggers": {"bash": [{"re": "^deploy$"}]}, "hint": "Must stay off."},
    ])
    caps = {"mcp": ["search", "deploy"], "tools": [], "skills": [], "cwd": str(cwd)}
    diagnostics = []
    assert match_opencode_routes("find report", caps, "alpha", cwd, vault, diagnostics=diagnostics) == []
    assert any("unsupported:search-present" in item for item in diagnostics)
    index = vault / ".index"
    index.mkdir()
    (index / "action-rules.tsv").write_text("bash\tunknown-action\t^deploy$\tMust stay off.\n", encoding="utf-8")
    action_diagnostics = []
    assert load_action_descriptors(vault, caps, "alpha", diagnostics=action_diagnostics) == []
    assert any("unsupported:deploy-present" in item for item in action_diagnostics)


def test_literal_tsv_preserves_quoted_ere_and_json_escaped_hint(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    _catalog(vault, [{
        "id": "quoted", "class": "action", "priority": 11,
        "command_triggers": {"bash": [{"re": '"deploy"'}]}, "hint": 'Say "yes".',
    }])
    index = vault / ".index"
    index.mkdir()
    (index / "action-rules.tsv").write_text('bash\tquoted\t"deploy"\tSay \\\"yes\\\".\n', encoding="utf-8")
    assert load_action_descriptors(vault, {"mcp": [], "tools": [], "skills": []}, "alpha") == [
        {"id": "quoted", "shell": "bash", "pattern": '"deploy"', "hint": 'Say "yes".',
         "priority": 11, "capabilityRevision": 0}
    ]
