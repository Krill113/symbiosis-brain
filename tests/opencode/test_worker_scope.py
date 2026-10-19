from __future__ import annotations

import json
from pathlib import Path

from symbiosis_brain.adapters.opencode.protocol import ContextRequest
from symbiosis_brain.adapters.opencode.worker import _service_factory


def test_resources_reply_exposes_scope_from_canonical_marker_resolver(tmp_path: Path) -> None:
    project = tmp_path / "synthetic-project"
    project.mkdir()
    (project / "CLAUDE.md").write_text("<!-- symbiosis-brain v1: scope=fixture-project -->\n", encoding="utf-8")
    vault = tmp_path / "synthetic-vault"
    vault.mkdir()
    request = ContextRequest(
        request_id="resources-scope", session_id="fixture-session", generation=0,
        operation="resources", cwd=str(project), boundary=str(tmp_path), origin="resume",
        capability_revision=0,
    )

    result = _service_factory(str(vault))("control")(request)

    assert result.status == "ok"
    assert result.scope == "fixture-project"
    assert result.resources is not None


def test_resources_reply_keeps_unknown_scope_bounded_to_global_mode(tmp_path: Path) -> None:
    project = tmp_path / "unmarked-project"
    project.mkdir()
    vault = tmp_path / "synthetic-vault"
    vault.mkdir()
    request = ContextRequest(
        request_id="resources-global", session_id="fixture-session", generation=0,
        operation="resources", cwd=str(project), boundary=None, origin="resume",
        capability_revision=0,
    )

    result = _service_factory(str(vault))("control")(request)

    assert result.status == "ok"
    assert result.scope is None


def test_routes_worker_returns_current_shell_action_descriptors(tmp_path: Path) -> None:
    project = tmp_path / "synthetic-project"
    project.mkdir()
    vault = tmp_path / "synthetic-vault"
    (vault / ".index").mkdir(parents=True)
    pattern = r"^git[[:space:]]+status([[:space:]].*)?$"
    hint = "Use the synthetic action fixture."
    (vault / "tool-routing.local.json").write_text(json.dumps([{
        "id": "synthetic-action",
        "class": "action",
        "hint": hint,
        "priority": 99,
        "expected_tool": "bash",
        "command_triggers": {"powershell": [{"re": pattern}]},
    }]), encoding="utf-8")
    (vault / ".index" / "action-rules.tsv").write_text(
        f"powershell\tsynthetic-action\t{pattern}\t{hint}\n", encoding="utf-8",
    )
    request = ContextRequest(
        request_id="routes-actions", session_id="fixture-session", generation=1,
        operation="routes", cwd=str(project), boundary=str(tmp_path), scope="probe", origin="resume",
        capability_revision=7, tools=("bash",),
    )

    result = _service_factory(str(vault))("control")(request)

    assert result.status == "ok"
    assert result.action_descriptors == [{
        "id": "synthetic-action", "shell": "powershell", "pattern": pattern,
        "hint": hint, "priority": 99, "capabilityRevision": 7,
    }]
