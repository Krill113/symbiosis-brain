from __future__ import annotations

import io
import json
import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

from symbiosis_brain.adapters.opencode.protocol import ContextBlock, ContextResult, ContextRequest
from symbiosis_brain.adapters.opencode.worker import _service_factory, serve


def _request(request_id: str, session_id: str = "session-a") -> ContextRequest:
    return ContextRequest(
        request_id=request_id, session_id=session_id, generation=3,
        operation="bootstrap", cwd="/synthetic/project", origin="user",
        capability_revision=7, boundary="/synthetic", scope="sample",
    )


def test_hello_multiple_frames_duplicate_id_and_stdout_noise_are_framed() -> None:
    requests = [_request("same", "session-a"), _request("same", "session-b"), _request("next", "session-c")]
    source = b"".join(json.dumps(request.to_wire()).encode() + b"\n" for request in requests)
    output = io.BytesIO()

    def factory(role):
        assert role == "control"

        def handle(request):
            print("synthetic progress noise")
            return ContextResult(
                request_id=request.request_id, session_id=request.session_id,
                generation=request.generation, capability_revision=request.capability_revision,
                status="ok", scope=request.scope,
                blocks=[ContextBlock("welcome", "Привет", 1, "bootstrap")],
            )
        return handle

    assert serve(io.BytesIO(source), output, factory, role="control") == 0
    frames = [json.loads(line) for line in output.getvalue().splitlines()]
    assert frames[0] == {"version": 1, "kind": "hello", "ready": True, "role": "control"}
    assert frames[1]["sessionId"] == "session-a" and frames[1]["blocks"][0]["text"] == "Привет"
    assert frames[2]["status"] == "degraded" and frames[2]["diagnostics"] == ["duplicate-request-id"]
    assert frames[3]["sessionId"] == "session-c"


def test_wrong_service_context_is_rejected_without_session_metadata_bleed() -> None:
    requests = [_request("r1", "first"), _request("r2", "second")]
    source = b"".join(json.dumps(request.to_wire()).encode() + b"\n" for request in requests)
    output = io.BytesIO()

    def factory(_role):
        return lambda req: ContextResult(req.request_id, "wrong-session", req.generation,
                                          req.capability_revision, "ok", req.scope)

    serve(io.BytesIO(source), output, factory, role="retrieval")
    frames = [json.loads(line) for line in output.getvalue().splitlines()][1:]
    assert [frame["sessionId"] for frame in frames] == ["first", "second"]
    assert all(frame["diagnostics"] == ["service-result-context-mismatch"] for frame in frames)


def test_worker_subprocess_uses_utf8_jsonl_and_exits_at_eof() -> None:
    code = (
        "import sys; from symbiosis_brain.adapters.opencode.worker import serve; "
        "from symbiosis_brain.adapters.opencode.protocol import ContextResult,ContextBlock; "
        "factory=lambda role: lambda r: ContextResult(r.request_id,r.session_id,r.generation,"
        "r.capability_revision,'ok',r.scope,[ContextBlock('g','雪だるま',1,'bootstrap')]); "
        "raise SystemExit(serve(sys.stdin.buffer,sys.stdout.buffer,factory,role='control'))"
    )
    request = _request("utf8")
    completed = subprocess.run(
        [sys.executable, "-c", code], input=json.dumps(request.to_wire(), ensure_ascii=False).encode() + b"\n",
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=5, check=False,
    )
    assert completed.returncode == 0
    frames = [json.loads(line) for line in completed.stdout.splitlines()]
    assert frames[0]["kind"] == "hello"
    assert frames[1]["blocks"][0]["text"] == "雪だるま"


def test_module_cli_accepts_worker_role_vault_and_keeps_utf8_framing(tmp_path) -> None:
    vault = tmp_path / "雪 vault"
    vault.mkdir()
    request = replace(_request("cli"), operation="resources", scope=None)
    completed = subprocess.run(
        [sys.executable, "-m", "symbiosis_brain.adapters.opencode", "--worker", "--role", "control", "--vault", str(vault)],
        input=json.dumps(request.to_wire(), ensure_ascii=False).encode("utf-8") + b"\n",
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=5, check=False,
    )
    assert completed.returncode == 0
    assert b"\xe9\x9b\xaa vault" in completed.stdout
    frames = [json.loads(line) for line in completed.stdout.splitlines()]
    assert frames[0]["kind"] == "hello" and frames[0]["role"] == "control"
    assert frames[1]["resources"]["actionRulesFile"].endswith("action-rules.tsv")
    assert frames[1]["resources"]["mcpCommand"] == [
        sys.executable, "-c", "from symbiosis_brain.install_cli import main; main()", "serve", "--vault", str(vault),
    ]


def test_resources_reply_command_completes_public_mcp_initialize_handshake(tmp_path) -> None:
    vault = tmp_path / "synthetic brain vault"
    vault.mkdir()
    request = replace(_request("resources-handshake"), operation="resources", scope=None)
    resources_result = _service_factory(str(vault))("control")(request)
    assert resources_result.status == "ok" and resources_result.resources is not None
    assert resources_result.resources.python_exe == sys.executable
    assert resources_result.resources.skills_dir.endswith("skills")
    assert (Path(resources_result.resources.skills_dir) / "brain-init" / "SKILL.md").is_file()
    assert (Path(resources_result.resources.skills_dir) / "brain-save" / "SKILL.md").is_file()
    initialize = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
        "protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "synthetic-opencode-installer", "version": "1"},
    }}
    initialized = {"jsonrpc": "2.0", "method": "notifications/initialized"}
    completed = subprocess.run(
        list(resources_result.resources.mcp_command),
        input=json.dumps(initialize) + "\n" + json.dumps(initialized) + "\n",
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=20, check=False,
    )
    assert completed.returncode == 0, completed.stderr
    frames = [json.loads(line) for line in completed.stdout.splitlines() if line.startswith("{")]
    assert any(frame.get("id") == 1 and "result" in frame for frame in frames)


def test_control_resources_path_does_not_import_search_or_vector_stack(tmp_path) -> None:
    code = (
        "import sys; from symbiosis_brain.adapters.opencode.worker import _service_factory; "
        "from symbiosis_brain.adapters.opencode.protocol import ContextRequest; "
        "handler=_service_factory(sys.argv[1])('control'); "
        "req=ContextRequest('r','s',0,'resources','/synthetic/project','user',0,boundary='/synthetic'); "
        "handler(req); forbidden={'symbiosis_brain.search','symbiosis_brain.storage','numpy','fastembed'}; "
        "assert not (forbidden & set(sys.modules))"
    )
    completed = subprocess.run(
        [sys.executable, "-c", code, str(tmp_path)], capture_output=True,
        text=True, timeout=5, check=False, env=os.environ.copy(),
    )
    assert completed.returncode == 0, completed.stderr
