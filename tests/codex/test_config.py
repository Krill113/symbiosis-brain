"""Tests for Codex-only configuration loading."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from symbiosis_brain.adapters.codex.config import (
    CodexConfig,
    TrustedProject,
    load_codex_config,
    resolve_codex_home,
)
from symbiosis_brain.pre_action_config import PreActionConfig


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def test_legacy_trust_settings_are_accepted_but_have_no_policy_effect(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    codex_home = home / ".codex"
    _write_json(
        codex_home / "symbiosis-brain.json",
        {
            "bootstrap_visibility": "verbose",
            "trusted_projects": [
                {
                    "root": str(tmp_path / "missing-and-irrelevant"),
                    "allowed_scopes": ["demo"],
                    "allow_global": False,
                }
            ],
            "save_reminder_turns": 12,
        },
    )
    _write_json(
        home / ".claude" / "symbiosis-brain-pre-action.json",
        {"hit_limit": 4, "routing_enabled": False},
    )
    debug_log = tmp_path / "debug.log"
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.setenv("SYMBIOSIS_BRAIN_DEBUG_LOG", str(debug_log))

    cfg = load_codex_config(codex_home, {})

    assert cfg.bootstrap_visibility == "verbose"
    assert cfg.trusted_projects == ()
    assert cfg.save_reminder_turns == 12
    assert cfg.recall.hit_limit == 4
    assert cfg.recall.routing_enabled is False
    assert "ignored deprecated Codex config key: trusted_projects" in (
        debug_log.read_text(encoding="utf-8")
    )


def test_trusted_project_type_remains_import_compatible_without_authority(
    tmp_path: Path,
) -> None:
    legacy = TrustedProject(
        root=tmp_path,
        allowed_scopes=frozenset({"demo"}),
        allow_global=False,
    )

    assert legacy.root == tmp_path
    assert legacy.allowed_scopes == frozenset({"demo"})
    assert legacy.allow_global is False


def test_recall_inherits_only_harness_neutral_fields(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    _write_json(
        home / ".claude" / "symbiosis-brain-pre-action.json",
        {
            "enabled": False,
            "matchers": ["Never"],
            "bash_whitelist": [".*"],
            "excluded_note_types": ["user", "wiki"],
            "hit_limit": 9,
            "query_max_chars": 321,
            "recall_dedup_enabled": False,
            "recall_dedup_ttl_seconds": 22,
            "prompt_recall_dedup_ttl_seconds": 33,
            "routing_enabled": False,
            "routing_mode": "additive",
            "routing_cap": 4,
            "routing_seen_ttl_seconds": 55,
            "serena_advisory_enabled": False,
            "retrieval_log_enabled": False,
        },
    )
    monkeypatch.setattr(Path, "home", lambda: home)

    cfg = load_codex_config(home / ".codex", {})

    assert cfg.recall.excluded_note_types == ["user", "wiki"]
    assert cfg.recall.hit_limit == 9
    assert cfg.recall.query_max_chars == 321
    assert cfg.recall.recall_dedup_enabled is False
    assert cfg.recall.recall_dedup_ttl_seconds == 22
    assert cfg.recall.prompt_recall_dedup_ttl_seconds == 33
    assert cfg.recall.routing_enabled is False
    assert cfg.recall.routing_mode == "additive"
    assert cfg.recall.routing_cap == 4
    assert cfg.recall.routing_seen_ttl_seconds == 55
    defaults = PreActionConfig()
    assert cfg.recall.enabled is defaults.enabled
    assert cfg.recall.matchers == defaults.matchers
    assert cfg.recall.bash_whitelist == defaults.bash_whitelist
    assert cfg.recall.serena_advisory_enabled is defaults.serena_advisory_enabled
    assert cfg.recall.retrieval_log_enabled is defaults.retrieval_log_enabled


def test_recall_collections_are_copied_not_shared(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    _write_json(
        home / ".claude" / "symbiosis-brain-pre-action.json",
        {"excluded_note_types": ["user", "wiki"]},
    )
    monkeypatch.setattr(Path, "home", lambda: home)

    first = load_codex_config(home / ".codex", {})
    second = load_codex_config(home / ".codex", {})
    first.recall.excluded_note_types.append("decision")

    assert second.recall.excluded_note_types == ["user", "wiki"]


def test_defaults_and_default_codex_home(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", lambda: home)

    assert resolve_codex_home({}) == (home / ".codex").resolve()
    cfg = load_codex_config(environ={})

    assert cfg == CodexConfig(
        bootstrap_visibility="one-line",
        trusted_projects=(),
        handoff_stale_days=30,
        save_reminder_turns=10,
        status_line_policy="recommend-only",
        sync_mode="manual",
        sync_bash_path=None,
        recall=PreActionConfig(),
    )


def test_codex_home_environment_override_is_resolved(tmp_path: Path) -> None:
    configured = tmp_path / "parent" / ".." / "codex"
    assert resolve_codex_home({"CODEX_HOME": str(configured)}) == configured.resolve()


def test_malformed_codex_file_keeps_defaults_and_shared_recall(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    codex_home = home / ".codex"
    codex_home.mkdir(parents=True)
    (codex_home / "symbiosis-brain.json").write_text("{", encoding="utf-8")
    _write_json(
        home / ".claude" / "symbiosis-brain-pre-action.json",
        {"hit_limit": 8},
    )
    debug_log = tmp_path / "debug.log"
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.setenv("SYMBIOSIS_BRAIN_DEBUG_LOG", str(debug_log))

    cfg = load_codex_config(codex_home, {})

    assert cfg.bootstrap_visibility == "one-line"
    assert cfg.trusted_projects == ()
    assert cfg.recall.hit_limit == 8
    assert "Codex config load failed" in debug_log.read_text(encoding="utf-8")


@pytest.mark.parametrize("root", [[], "bad", 3, None])
def test_non_object_codex_config_keeps_defaults(
    root: object,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    codex_home = home / ".codex"
    _write_json(codex_home / "symbiosis-brain.json", root)
    debug_log = tmp_path / "debug.log"
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.setenv("SYMBIOSIS_BRAIN_DEBUG_LOG", str(debug_log))

    cfg = load_codex_config(codex_home, {})

    assert cfg.bootstrap_visibility == "one-line"
    assert "root is not an object" in debug_log.read_text(encoding="utf-8")


def test_unknown_keys_and_wrong_types_are_logged_and_ignored(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    codex_home = home / ".codex"
    _write_json(
        codex_home / "symbiosis-brain.json",
        {
            "bootstrap_visibility": True,
            "handoff_stale_days": "30",
            "save_reminder_turns": False,
            "status_line_policy": "replace",
            "sync_mode": "automatic",
            "sync_bash_path": 7,
            "surprise": "value",
        },
    )
    debug_log = tmp_path / "debug.log"
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.setenv("SYMBIOSIS_BRAIN_DEBUG_LOG", str(debug_log))

    cfg = load_codex_config(codex_home, {})
    log = debug_log.read_text(encoding="utf-8")

    assert cfg.bootstrap_visibility == "one-line"
    assert cfg.handoff_stale_days == 30
    assert cfg.save_reminder_turns == 10
    assert cfg.status_line_policy == "recommend-only"
    assert cfg.sync_mode == "manual"
    assert cfg.sync_bash_path is None
    assert "unknown Codex config key: surprise" in log
    assert "invalid Codex config value" in log


def test_absolute_sync_bash_path_is_normalized(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    codex_home = home / ".codex"
    executable = tmp_path / "tools" / "bash"
    _write_json(
        codex_home / "symbiosis-brain.json",
        {"sync_mode": "auto", "sync_bash_path": str(executable)},
    )
    monkeypatch.setattr(Path, "home", lambda: home)

    cfg = load_codex_config(codex_home, {})

    assert cfg.sync_mode == "auto"
    assert cfg.sync_bash_path == executable.resolve()


def test_environment_can_only_reduce_visibility_to_silent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    codex_home = home / ".codex"
    _write_json(
        codex_home / "symbiosis-brain.json",
        {
            "bootstrap_visibility": "verbose",
            "save_reminder_turns": 19,
            "status_line_policy": "disabled",
            "sync_mode": "auto",
            "sync_bash_path": str((tmp_path / "tools" / "bash").resolve()),
        },
    )
    monkeypatch.setattr(Path, "home", lambda: home)

    cfg = load_codex_config(
        codex_home,
        {
            "SYMBIOSIS_BRAIN_CODEX_VISIBILITY": "silent",
            "SYMBIOSIS_BRAIN_CODEX_SAVE_REMINDER_TURNS": "1",
            "SYMBIOSIS_BRAIN_CODEX_STATUS_LINE_POLICY": "recommend-only",
            "SYMBIOSIS_BRAIN_CODEX_SYNC_MODE": "manual",
            "SYMBIOSIS_BRAIN_CODEX_SYNC_BASH_PATH": "other",
            "SYMBIOSIS_BRAIN_CODEX_TRUSTED_PROJECTS": "[]",
        },
    )

    assert cfg.bootstrap_visibility == "silent"
    assert cfg.trusted_projects == ()
    assert cfg.save_reminder_turns == 19
    assert cfg.status_line_policy == "disabled"
    assert cfg.sync_mode == "auto"
    assert cfg.sync_bash_path == (tmp_path / "tools" / "bash").resolve()


@pytest.mark.parametrize("requested", ["one-line", "verbose", "loud", ""])
def test_environment_cannot_increase_or_rewrite_visibility(
    requested: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    codex_home = home / ".codex"
    _write_json(
        codex_home / "symbiosis-brain.json",
        {"bootstrap_visibility": "silent"},
    )
    monkeypatch.setattr(Path, "home", lambda: home)

    cfg = load_codex_config(
        codex_home,
        {"SYMBIOSIS_BRAIN_CODEX_VISIBILITY": requested},
    )

    assert cfg.bootstrap_visibility == "silent"
