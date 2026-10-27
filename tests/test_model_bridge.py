"""Statusline regression: no model bridge; context/state/rate and UI survive.

Synthetic models and session IDs only. Bash is resolved explicitly on Windows.
"""
import json
import os
import pathlib
import subprocess
import time

import pytest

from symbiosis_brain import provenance

try:
    from tests._bash_resolver import _bash  # noqa: E402
except ImportError:  # pragma: no cover - pytest prepend mode
    from _bash_resolver import _bash  # noqa: E402

HOOKS = pathlib.Path(__file__).resolve().parents[1] / "hooks"
HOOKLIB = HOOKS / "sb-hooklib.sh"
EXPORT = HOOKS / "sb-export.sh"
SESSION_START = HOOKS / "brain-session-start.sh"

MODEL_JSON = json.dumps({
    "session_id": "s9",
    "model": {"id": "test-model-9", "display_name": "Test Model 9"},
    "used_percentage": 11,
})
NO_MODEL_JSON = json.dumps({"session_id": "s9", "used_percentage": 11})


def _run_export(sb_tmp: pathlib.Path, statusline_json: str, claude_pid=None):
    """Source sb-hooklib.sh + sb-export.sh exactly the way sb-statusline.sh:24-25 does."""
    env = dict(os.environ)
    env["TMPDIR"] = str(sb_tmp)
    env["TEMP"] = str(sb_tmp)
    env["SB_STATUSLINE_INPUT"] = statusline_json
    env.pop("CLAUDE_PID", None)
    env.pop("SYMBIOSIS_BRAIN_RATE_LIMITS_FILE", None)
    if claude_pid is not None:
        env["CLAUDE_PID"] = str(claude_pid)
    return subprocess.run(
        [_bash(), "-c", '. "$1"; . "$2"', "sb-export-test",
         HOOKLIB.as_posix(), EXPORT.as_posix()],
        env=env, capture_output=True, text=True, encoding="utf-8",
    )


def _bridge_files(sb_tmp: pathlib.Path):
    return sorted(p.name for p in sb_tmp.glob("brain-model-*"))


@pytest.mark.parametrize("pid", [424242, None])
@pytest.mark.parametrize("payload", [MODEL_JSON, NO_MODEL_JSON])
def test_export_no_longer_writes_model_files(tmp_path, pid, payload):
    proc = _run_export(tmp_path, payload, claude_pid=pid)
    assert proc.returncode == 0, proc.stderr
    assert _bridge_files(tmp_path) == []


def test_context_state_and_rate_exports_are_unchanged(tmp_path):
    payload = json.dumps({
        "session_id": "s9",
        "model": {"id": "test-model-9", "display_name": "Test Model 9"},
        "context_window": {"used_percentage": 11},
        "rate_limits": {"five_hour": {"used_percentage": 23, "resets_at": 123456}, "seven_day": {"used_percentage": 45}},
    })
    proc = _run_export(tmp_path, payload, claude_pid=424242)
    assert proc.returncode == 0, proc.stderr
    assert (tmp_path / "brain-context-pct-s9").read_text(encoding="utf-8").strip() == "11"
    state = (tmp_path / "brain-context-state-s9").read_text(encoding="utf-8").strip().split("\t")
    assert state[0] == "11" and state[1].isdigit()
    rate = json.loads((tmp_path / "claude-rate-limits.json").read_text(encoding="utf-8"))
    assert rate == {"five_hour_pct": 23, "resets_at": 123456, "seven_day_pct": 45, "ts": int(state[1])}
    assert _bridge_files(tmp_path) == []


def test_status_observation_still_exposes_model_for_ui(tmp_path):
    env = dict(os.environ, SB_STATUSLINE_INPUT=MODEL_JSON, TMPDIR=str(tmp_path), TEMP=str(tmp_path))
    proc = subprocess.run(
        [_bash(), "-c", '. "$1"; . "$2"; printf "%s\\t%s" "$SB_MODEL_ID" "$SB_MODEL_NAME"', "model-ui-test", HOOKLIB.as_posix(), EXPORT.as_posix()],
        env=env, capture_output=True, text=True, encoding="utf-8",
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == "test-model-9\tTest Model 9"


def test_export_stays_fork_free(tmp_path):
    text = EXPORT.read_text(encoding="utf-8")
    block = text.split("# 1. Context", 1)[1]
    for forbidden in ("$(", "`", "| ", "jq", "python", "sed ", "grep ", "cut "):
        assert forbidden not in block, forbidden
