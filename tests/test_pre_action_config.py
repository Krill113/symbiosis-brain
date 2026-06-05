"""Tests for pre_action_config loader (B1 hook)."""
import json
from pathlib import Path

import pytest

from symbiosis_brain.pre_action_config import PreActionConfig, load_config


def test_load_with_no_file_returns_defaults(tmp_path: Path):
    cfg = load_config(tmp_path / "nonexistent.json")
    assert cfg.enabled is True
    assert "Task" in cfg.matchers
    assert "Bash" in cfg.matchers
    assert cfg.hit_limit == 3
    assert "user" in cfg.excluded_note_types


def test_load_with_malformed_json_returns_defaults(tmp_path: Path):
    bad = tmp_path / "bad.json"
    bad.write_text("{not valid json")
    cfg = load_config(bad)
    assert cfg.enabled is True
    assert cfg.hit_limit == 3


def test_load_with_partial_keys_merges_with_defaults(tmp_path: Path):
    cfg_path = tmp_path / "cfg.json"
    cfg_path.write_text(json.dumps({"matchers": ["Task"], "hit_limit": 5}))
    cfg = load_config(cfg_path)
    assert cfg.matchers == ["Task"]
    assert cfg.hit_limit == 5
    assert cfg.enabled is True  # default kept
    assert "user" in cfg.excluded_note_types  # default kept


def test_load_with_disabled_returns_disabled(tmp_path: Path):
    cfg_path = tmp_path / "cfg.json"
    cfg_path.write_text(json.dumps({"enabled": False}))
    cfg = load_config(cfg_path)
    assert cfg.enabled is False


def test_load_with_custom_bash_whitelist(tmp_path: Path):
    cfg_path = tmp_path / "cfg.json"
    cfg_path.write_text(json.dumps({"bash_whitelist": [r"^terraform (apply|destroy)"]}))
    cfg = load_config(cfg_path)
    assert cfg.bash_whitelist == [r"^terraform (apply|destroy)"]


def test_load_with_extra_excluded_types(tmp_path: Path):
    cfg_path = tmp_path / "cfg.json"
    cfg_path.write_text(json.dumps({"excluded_note_types": ["user", "wiki"]}))
    cfg = load_config(cfg_path)
    assert "wiki" in cfg.excluded_note_types
    assert "user" in cfg.excluded_note_types


def test_load_with_unknown_key_keeps_defaults(tmp_path: Path):
    """Unknown config keys should be logged and ignored, defaults preserved."""
    cfg_path = tmp_path / "cfg.json"
    # Typo "enabeld" instead of "enabled" — should be ignored
    cfg_path.write_text(json.dumps({"enabeld": False, "hit_limit": 7}))
    cfg = load_config(cfg_path)
    assert cfg.enabled is True  # typo'd key ignored, default kept
    assert cfg.hit_limit == 7  # valid key applied
    assert not hasattr(cfg, "enabeld")


def test_load_with_type_mismatch_keeps_default(tmp_path: Path):
    """Type mismatches should be logged and ignored, defaults preserved."""
    cfg_path = tmp_path / "cfg.json"
    cfg_path.write_text(json.dumps({"hit_limit": "three"}))
    cfg = load_config(cfg_path)
    assert cfg.hit_limit == 3  # wrong type → default kept


def test_load_with_list_root_returns_defaults(tmp_path: Path):
    """Non-dict JSON root should return defaults gracefully."""
    cfg_path = tmp_path / "cfg.json"
    cfg_path.write_text(json.dumps([1, 2, 3]))
    cfg = load_config(cfg_path)
    assert cfg.enabled is True
    assert cfg.hit_limit == 3


# --- Stage-4 routing knobs ---

def test_routing_config_defaults():
    cfg = PreActionConfig()
    assert cfg.routing_enabled is True
    assert cfg.routing_mode == "decompose"
    assert cfg.routing_cap == 2
    assert cfg.routing_seen_ttl_seconds == 86400


def test_routing_config_json_override_and_unknown_ignored(tmp_path, monkeypatch):
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    p = tmp_path / "cfg.json"
    p.write_text(json.dumps({
        "routing_mode": "additive",
        "routing_cap": 3,
        "routing_enabled": False,
        "routing_mode_typo": "x",
        "recall_dedup_ttl_seconds": "nope"
    }), encoding="utf-8")
    cfg = load_config(p)
    assert cfg.routing_mode == "additive"
    assert cfg.routing_cap == 3
    assert cfg.routing_enabled is False
    assert cfg.recall_dedup_ttl_seconds == 120


def test_routing_local_path_under_vault(tmp_path):
    from symbiosis_brain.pre_action_config import routing_local_path, routing_default_path
    vault = tmp_path / "v"
    assert routing_local_path(vault) == vault / "tool-routing.local.json"
    assert routing_default_path().parent.name == "data"
    assert routing_default_path().parent.parent.name == "symbiosis_brain"
