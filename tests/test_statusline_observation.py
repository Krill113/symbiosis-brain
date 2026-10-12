"""Statusline observations must not turn unavailable data into current truth."""
import json
import os
from pathlib import Path
import subprocess

import pytest

from tests._bash_resolver import _find_bash

HOOKS = Path(__file__).resolve().parents[1] / "hooks"
BASH = _find_bash()
pytestmark = pytest.mark.skipif(BASH is None, reason="Bash is required")


def _run(tmp_path, payload):
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True, exist_ok=True)
    (home / ".claude" / "settings.json").write_text(
        '{"effortLevel":"xhigh"}', encoding="utf-8"
    )
    env = {
        key: value for key, value in os.environ.items()
        if not key.startswith(("SYMBIOSIS_", "SB_", "CLAUDE_", "ANTHROPIC_"))
    }
    env.update({
        "HOME": home.as_posix(), "USERPROFILE": str(home),
        "TMPDIR": tmp_path.as_posix(), "TEMP": str(tmp_path), "TMP": str(tmp_path),
        "BASH_ENV": "", "ENV": "", "SB_NOW": "1000",
    })
    result = subprocess.run(
        [BASH, (HOOKS / "sb-statusline.sh").as_posix()],
        input=json.dumps({"session_id": "observation-fixture", **payload}),
        capture_output=True, text=True, encoding="utf-8", env=env,
        cwd=tmp_path, timeout=10,
    )
    assert result.returncode == 0, result.stderr
    bridge = tmp_path / "brain-context-pct-observation-fixture"
    return result.stdout, bridge.read_text(encoding="utf-8").strip() if bridge.exists() else None


def test_context_ignores_rate_limit_order(tmp_path):
    output, bridge = _run(tmp_path, {
        "rate_limits": {"five_hour": {"used_percentage": 0}},
        "context_window": {"used_percentage": 28},
    })
    assert "28%" in output
    assert bridge == "28"


@pytest.mark.parametrize("context", [
    {"used_percentage": None}, {}, {"used_percentage": "28"},
    {"used_percentage": -1}, {"used_percentage": 101},
    {"used_percentage": 0, "current_usage": {
        "input_tokens": 0, "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 0, "output_tokens": 0,
    }},
])
def test_unavailable_context_invalidates_previous_observation(tmp_path, context):
    _run(tmp_path, {"context_window": {"used_percentage": 45}})
    output, bridge = _run(tmp_path, {"context_window": context})
    assert "ctx:?" in output
    assert bridge in (None, ""), "old percentage must not remain current"


def test_rounded_zero_with_positive_usage_is_valid(tmp_path):
    output, bridge = _run(tmp_path, {"context_window": {
        "used_percentage": 0, "current_usage": {"input_tokens": 100},
    }})
    assert "0%" in output
    assert bridge == "0"


def test_percentage_can_decrease_after_compaction(tmp_path):
    _run(tmp_path, {"context_window": {"used_percentage": 31}})
    output, bridge = _run(tmp_path, {"context_window": {"used_percentage": 4}})
    assert "4%" in output
    assert bridge == "4"


def test_runtime_effort_overrides_global_default(tmp_path):
    output, _ = _run(tmp_path, {
        "model": {"id": "fixture-model", "display_name": "Fixture"},
        "effort": {"level": "medium"},
    })
    assert "Fixture (medium)" in output
    assert "xhigh" not in output


def test_missing_effort_does_not_use_settings(tmp_path):
    output, _ = _run(tmp_path, {"model": {"id": "fixture-model"}})
    assert "fixture-model" in output
    assert "xhigh" not in output


def test_nested_and_quoted_model_decoys_are_not_selected(tmp_path):
    output, _ = _run(tmp_path, {
        "decoy": {"display_name": "Wrong", "effort": {"level": "max"}},
        "quoted": '"model":{"display_name":"Wrong"}',
        "model": {"id": "fixture-model", "display_name": "Right"},
        "effort": {"level": "low"},
    })
    assert "Right (low)" in output
    assert "Wrong" not in output
