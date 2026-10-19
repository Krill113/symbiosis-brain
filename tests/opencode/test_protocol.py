import json
from pathlib import Path

import pytest

from symbiosis_brain.adapters.opencode.protocol import (
    MAX_FRAME_BYTES,
    ProtocolError,
    parse_request,
    parse_request_frame,
    encode_frame,
)


def request(**overrides):
    value = {
        "version": 1,
        "requestId": "req-1",
        "sessionId": "session-1",
        "generation": 2,
        "operation": "bootstrap",
        "cwd": "/work/alpha",
        "boundary": "/work/alpha",
        "origin": "user",
        "capabilityRevision": 3,
        "capabilities": {"tools": [], "mcp": [], "skills": []},
    }
    value.update(overrides)
    return value


def test_parse_request_returns_typed_dto_and_copies_correlations():
    parsed = parse_request(request())
    assert parsed.request_id == "req-1"
    assert parsed.session_id == "session-1"
    assert parsed.generation == 2
    assert parsed.operation == "bootstrap"


@pytest.mark.parametrize(
    "changes",
    [
        {"version": 2},
        {"operation": "unknown"},
        {"generation": True},
        {"requestId": ""},
        {"requestId": None},
        {"origin": "system"},
        {"capabilities": {"tools": [], "mcp": "bad", "skills": []}},
    ],
)
def test_rejects_invalid_request_fields(changes):
    with pytest.raises(ProtocolError):
        parse_request(request(**changes))


def test_rejects_foreign_session_and_unsafe_scope_boundary():
    with pytest.raises(ProtocolError, match="session-mismatch"):
        parse_request(request(), expected_session_id="other")
    with pytest.raises(ProtocolError, match="cwd-outside-boundary"):
        parse_request(request(cwd="/elsewhere/file", boundary="/work/alpha"))
    no_boundary = request(scope="alpha")
    no_boundary.pop("boundary")
    with pytest.raises(ProtocolError, match="scope-without-boundary"):
        parse_request(no_boundary)


def test_optional_wire_fields_must_be_omitted_instead_of_explicit_null():
    with pytest.raises(ProtocolError, match="invalid-boundary"):
        parse_request(request(boundary=None))
    with pytest.raises(ProtocolError, match="invalid-query"):
        parse_request(request(query=None))
    with pytest.raises(ProtocolError, match="invalid-action-command"):
        parse_request(request(action={"tool": "shell", "command": None}))


def test_frame_limit_is_utf8_bytes_and_rejects_oversize():
    payload = request(query="я" * MAX_FRAME_BYTES)
    with pytest.raises(ProtocolError, match="frame-too-large"):
        parse_request_frame(json.dumps(payload, ensure_ascii=False).encode())


@pytest.mark.parametrize("raw", [b"{", b"\xff", b"[]"])
def test_malformed_json_gets_safe_protocol_error(raw):
    with pytest.raises(ProtocolError) as exc:
        parse_request_frame(raw)
    assert "exception" not in str(exc.value).lower()


def test_result_roundtrip_keeps_correlations_and_validates_resources():
    from symbiosis_brain.adapters.opencode.protocol import ContextResult, parse_result

    result = ContextResult(
        request_id="req-1",
        session_id="session-1",
        generation=2,
        capability_revision=3,
        status="ok",
        scope="alpha",
        blocks=[],
        diagnostics=[],
    )
    frame = encode_frame(result.to_wire())
    assert parse_result(frame, expected_session_id="session-1").request_id == "req-1"
    with pytest.raises(ProtocolError):
        parse_result(encode_frame({"version": 1, "kind": "result", "blocks": []}))


def test_shared_python_typescript_json_vectors():
    vectors = json.loads(Path(__file__).with_name("protocol-vectors.json").read_text(encoding="utf-8"))
    parsed = parse_request(vectors["request"])
    assert parsed.to_wire() == vectors["request"]
    from symbiosis_brain.adapters.opencode.protocol import parse_result

    frame = encode_frame(vectors["result"])
    assert parse_result(frame).to_wire() == vectors["result"]
    assert parse_result(encode_frame(vectors["richResult"]), expected_operation="resources").to_wire() == vectors["richResult"]


def test_resources_are_required_only_for_successful_resources_operation():
    from symbiosis_brain.adapters.opencode.protocol import parse_result

    base = {
        "version": 1, "kind": "result", "requestId": "r", "sessionId": "s",
        "generation": 0, "capabilityRevision": 0, "status": "ok", "scope": None,
        "blocks": [], "diagnostics": [],
    }
    with pytest.raises(ProtocolError, match="missing-resources"):
        parse_result(encode_frame(base), expected_operation="resources")
    degraded = {**base, "status": "degraded"}
    assert parse_result(encode_frame(degraded), expected_operation="resources").status == "degraded"


def test_shared_invalid_request_vectors_have_stable_python_reasons():
    vectors = json.loads(Path(__file__).with_name("protocol-vectors.json").read_text(encoding="utf-8"))
    for case in vectors["invalidRequests"]:
        payload = {**vectors["request"], **case["change"]}
        with pytest.raises(ProtocolError) as exc:
            parse_request(payload)
        assert exc.value.reason == case["reason"]


def test_shared_invalid_result_and_hello_vectors_fail_safely():
    vectors = json.loads(Path(__file__).with_name("protocol-vectors.json").read_text(encoding="utf-8"))
    from symbiosis_brain.adapters.opencode.protocol import parse_result, parse_worker_frame

    for case in vectors["invalidResults"]:
        payload = {**vectors["result"], **case["change"]}
        with pytest.raises(ProtocolError) as exc:
            parse_result(encode_frame(payload))
        assert exc.value.reason == case["reason"]
    for case in vectors["invalidHellos"]:
        with pytest.raises(ProtocolError) as exc:
            parse_worker_frame(encode_frame(case["frame"]))
        assert exc.value.reason == case["reason"]


@pytest.mark.parametrize(
    ("frame_name", "expected_reason"),
    [("request", "invalid-generation"), ("result", "invalid-generation"), ("hello", "invalid-worker-frame")],
)
def test_huge_integer_raw_frames_are_rejected_with_matching_safe_reasons(frame_name, expected_reason):
    vectors = json.loads(Path(__file__).with_name("protocol-vectors.json").read_text(encoding="utf-8"))
    from symbiosis_brain.adapters.opencode.protocol import MAX_FRAME_BYTES, parse_result, parse_worker_frame

    parsers = {"request": parse_request_frame, "result": parse_result, "hello": parse_worker_frame}
    digits = "9" * 5000
    raw = vectors["hugeIntegerFrameTemplates"][frame_name].replace("__DIGITS__", digits)
    assert len(raw.encode("utf-8")) < MAX_FRAME_BYTES
    with pytest.raises(ProtocolError) as exc:
        parsers[frame_name](raw)
    assert exc.value.reason == expected_reason


def test_shared_grep_exe_null_empty_whitespace_and_nonempty_policy():
    vectors = json.loads(Path(__file__).with_name("protocol-vectors.json").read_text(encoding="utf-8"))
    from symbiosis_brain.adapters.opencode.protocol import parse_result

    for case in vectors["grepExeCases"]:
        payload = json.loads(json.dumps(vectors["richResult"]))
        payload["resources"]["grepExe"] = case["value"]
        if case["valid"]:
            assert parse_result(encode_frame(payload), expected_operation="resources").resources.grep_exe == case["value"]
        else:
            with pytest.raises(ProtocolError) as exc:
                parse_result(encode_frame(payload), expected_operation="resources")
            assert exc.value.reason == case["reason"]
