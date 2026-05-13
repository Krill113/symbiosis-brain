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
