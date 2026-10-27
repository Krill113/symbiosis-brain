"""Synthetic executor-local Claude transcript signature contracts."""
import importlib
import json
from pathlib import Path

import pytest


def _adapter():
    return importlib.import_module("symbiosis_brain.adapters.claude.provenance")


def _assistant(model, tool_id=None):
    return {"type": "assistant", "message": {"model": model, "content": [
        {"type": "tool_use", "id": tool_id, "name": "synthetic_tool", "input": {}}
    ] if tool_id else [{"type": "text", "text": "Synthetic reply"}]}}


def _write(path, records):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
    return path


def test_exact_tool_use_wins_over_latest_assistant(tmp_path):
    path = _write(tmp_path / "session.jsonl", [
        _assistant("model-exact", "tool-1"), _assistant("model-latest", "tool-2"),
    ])
    assert _adapter().model_from_transcript({"transcript_path": str(path), "tool_use_id": "tool-1"}) == "model-exact"


def test_missing_exact_id_uses_latest_valid_assistant(tmp_path):
    path = _write(tmp_path / "session.jsonl", [
        _assistant("older"), _assistant("latest"), {"type": "user", "message": {"model": "not-assistant"}},
        _assistant(None), _assistant("  "), _assistant(["invalid"]),
    ])
    assert _adapter().model_from_transcript({"transcript_path": str(path), "tool_use_id": "missing"}) == "latest"


@pytest.mark.parametrize("agent_id", ["", "../escape", "child/escape", "child\\escape", ".", "..", 7, [], None])
def test_invalid_child_id_never_borrows_parent(tmp_path, agent_id):
    path = _write(tmp_path / "parent.jsonl", [_assistant("parent-model")])
    assert _adapter().model_from_transcript({"transcript_path": str(path), "agent_id": agent_id}) is None


def test_missing_child_never_reads_parent_or_meta(tmp_path):
    path = _write(tmp_path / "parent.jsonl", [_assistant("parent-model")])
    meta = tmp_path / "parent/subagents/agent-child.meta.json"
    meta.parent.mkdir(parents=True)
    meta.write_text(json.dumps({"model": "meta-model"}), encoding="utf-8")
    assert _adapter().model_from_transcript({"transcript_path": str(path), "agent_id": "child"}) is None


def test_sessions_children_and_model_switch_stay_isolated(tmp_path):
    first = _write(tmp_path / "first.jsonl", [_assistant("parent-first")])
    second = _write(tmp_path / "second.jsonl", [_assistant("parent-second")])
    child_a = _write(tmp_path / "first/subagents/agent-child-a.jsonl", [_assistant("child-a")])
    _write(tmp_path / "first/subagents/agent-child-b.jsonl", [_assistant("child-b")])
    _write(tmp_path / "second/subagents/agent-child-a.jsonl", [_assistant("other-child-a")])
    cases = [(first, None, "parent-first"), (second, None, "parent-second"),
             (first, "child-a", "child-a"), (first, "child-b", "child-b"),
             (second, "child-a", "other-child-a")]
    for path, child, expected in cases:
        payload = {"transcript_path": str(path)}
        if child:
            payload["agent_id"] = child
        assert _adapter().model_from_transcript(payload) == expected
    _write(child_a, [_assistant("switched-model")])
    assert _adapter().model_from_transcript({"transcript_path": str(first), "agent_id": "child-a"}) == "switched-model"


@pytest.mark.parametrize("text,expected", [
    ("", None), ("{", None), ('{"type":"assistant"}\n', None),
    (json.dumps(_assistant("partial-only")), None),
    (json.dumps(_assistant("complete")) + "\n" + '{"type":', "complete"),
    ("not json\n" + json.dumps(_assistant("complete")) + "\n", "complete"),
])
def test_truncated_and_malformed_records_fail_open(tmp_path, text, expected):
    path = tmp_path / "session.jsonl"
    path.write_text(text, encoding="utf-8")
    assert _adapter().model_from_transcript({"transcript_path": str(path)}) == expected


@pytest.mark.parametrize("payload", [{}, {"transcript_path": None}, {"transcript_path": 5}, {"transcript_path": ""}])
def test_absent_transcript_path_is_unknown(payload):
    assert _adapter().model_from_transcript(payload) is None


def test_missing_and_unreadable_files_fail_open(tmp_path, monkeypatch):
    path = tmp_path / "missing.jsonl"
    assert _adapter().model_from_transcript({"transcript_path": str(path)}) is None
    def denied(*args, **kwargs):
        raise PermissionError("synthetic denial")
    monkeypatch.setattr(Path, "open", denied)
    assert _adapter().model_from_transcript({"transcript_path": str(path)}) is None


@pytest.mark.parametrize(("prefix", "expected"), [(b"older record\n", "exact-model"), (b"partial", "latest-model")])
def test_tail_boundary_preserves_only_complete_first_record(tmp_path, prefix, expected):
    first = (json.dumps(_assistant("exact-model", "exact-id")) + "\n").encode()
    last = (json.dumps(_assistant("latest-model")) + "\n").encode()
    padding = b"x" * (1024 * 1024 - len(first) - len(last) - 1) + b"\n"
    tail = first + padding + last
    assert len(tail) == 1024 * 1024
    path = tmp_path / "boundary.jsonl"
    path.write_bytes(prefix + tail)
    assert _adapter().model_from_transcript({"transcript_path": str(path), "tool_use_id": "exact-id"}) == expected


def test_tail_is_bounded_single_read_and_drops_partial_first_record(tmp_path, monkeypatch):
    path = tmp_path / "large.jsonl"
    prefix = json.dumps(_assistant("outside-tail", "exact-old")) + "\n"
    path.write_bytes(prefix.encode() + b'x' * (1024 * 1024 + 100) + b'\n' +
                     (json.dumps(_assistant("tail-model")) + "\n").encode())
    opened = []
    reads = []
    real_open = Path.open
    class RecordingFile:
        def __init__(self, file):
            self.file = file
        def __enter__(self):
            return self
        def __exit__(self, *args):
            self.file.close()
        def __getattr__(self, name):
            return getattr(self.file, name)
        def read(self, size=-1):
            reads.append(size)
            return self.file.read(size)
    def recording_open(file, *args, **kwargs):
        opened.append(file)
        return RecordingFile(real_open(file, *args, **kwargs))
    monkeypatch.setattr(Path, "open", recording_open)
    assert _adapter().model_from_transcript({"transcript_path": str(path), "tool_use_id": "exact-old"}) == "tail-model"
    assert opened == [path]
    assert reads == [1, 1024 * 1024]


@pytest.mark.parametrize("tool", ["brain_write", "brain_append", "brain_patch", "brain_rotate_handoffs"])
@pytest.mark.parametrize("server", ["symbiosis-brain", "symbiosis_brain"])
def test_pre_tool_update_copies_complete_args_overriding_only_model(tmp_path, tool, server):
    path = _write(tmp_path / "session.jsonl", [_assistant("executor-model", "current")])
    args = {"path": "demo/note.md", "body": "Synthetic", "role": "colleague", "model": "self-report", "nested": {"keep": [1, 2]}}
    payload = {"tool_name": f"mcp__{server}__{tool}", "tool_input": args, "transcript_path": str(path), "tool_use_id": "current"}
    update = _adapter().pre_tool_update(payload)
    assert update == {**args, "model": "executor-model"}
    assert update is not args
    assert payload["tool_input"] == args and args["model"] == "self-report"


@pytest.mark.parametrize("tool", ["Edit", "mcp__other__brain_write", "mcp__symbiosis-brain__brain_read", "prefix_brain_write"])
def test_unrelated_tools_untouched(tmp_path, tool):
    path = _write(tmp_path / "session.jsonl", [_assistant("executor")])
    assert _adapter().pre_tool_update({"tool_name": tool, "tool_input": {}, "transcript_path": str(path)}) is None


def test_unavailable_model_preserves_self_report(tmp_path):
    args = {"model": "self-report", "role": "lead"}
    assert _adapter().pre_tool_update({"tool_name": "mcp__symbiosis-brain__brain_write", "tool_input": args, "transcript_path": str(tmp_path / "missing.jsonl")}) is None
    assert args == {"model": "self-report", "role": "lead"}
