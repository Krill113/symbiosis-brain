import json
import os
import subprocess
import sys
from pathlib import Path

HOOK = Path(__file__).parent.parent / "hooks" / "brain-save-trigger.py"


def _run(mode, stdin, env=None):
    e = os.environ.copy()
    e.update(env or {})
    return subprocess.run(
        [sys.executable, str(HOOK), mode],
        input=stdin, capture_output=True, text=True, encoding="utf-8", env=e,
    )


def test_stop_no_pct_returns_0(tmp_path, monkeypatch):
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    proc = _run("stop", json.dumps({"session_id": "s1"}))
    assert proc.returncode == 0


def test_stop_below_threshold_returns_0(tmp_path, monkeypatch):
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    (tmp_path / "brain-context-pct-s1").write_text("30", encoding="utf-8")
    proc = _run("stop", json.dumps({"session_id": "s1"}))
    assert proc.returncode == 0


def test_stop_at_40_fires_soft_zone(tmp_path, monkeypatch):
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    (tmp_path / "brain-context-pct-s1").write_text("42", encoding="utf-8")
    proc = _run("stop", json.dumps({"session_id": "s1"}))
    assert proc.returncode == 2
    assert "Контекст 42%" in proc.stderr
    # Trigger marker written
    triggered = (tmp_path / "brain-triggered-s1").read_text(encoding="utf-8")
    assert "40" in triggered


def test_stop_70_zone_serious_message(tmp_path, monkeypatch):
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    (tmp_path / "brain-context-pct-s1").write_text("75", encoding="utf-8")
    proc = _run("stop", json.dumps({"session_id": "s1"}))
    assert proc.returncode == 2
    assert "пора сохранять" in proc.stderr


def test_stop_90_last_chance_message(tmp_path, monkeypatch):
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    (tmp_path / "brain-context-pct-s1").write_text("92", encoding="utf-8")
    proc = _run("stop", json.dumps({"session_id": "s1"}))
    assert proc.returncode == 2
    assert "последний шанс" in proc.stderr


def test_stop_already_triggered_zone_no_refire(tmp_path, monkeypatch):
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    (tmp_path / "brain-context-pct-s1").write_text("45", encoding="utf-8")
    (tmp_path / "brain-triggered-s1").write_text("40\n", encoding="utf-8")
    proc = _run("stop", json.dumps({"session_id": "s1"}))
    assert proc.returncode == 0


def test_stop_delta_guard_skips_recent_save(tmp_path, monkeypatch):
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    (tmp_path / "brain-context-pct-s1").write_text("45", encoding="utf-8")
    (tmp_path / "brain-last-save-pct-s1").write_text("40", encoding="utf-8")  # delta=5 < 20
    proc = _run("stop", json.dumps({"session_id": "s1"}))
    assert proc.returncode == 0


def test_stop_save_later_skips_one_in_soft_zone(tmp_path, monkeypatch):
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    (tmp_path / "brain-context-pct-s1").write_text("45", encoding="utf-8")
    save_later = tmp_path / "brain-save-later-s1"
    save_later.write_text("", encoding="utf-8")
    proc = _run("stop", json.dumps({"session_id": "s1"}))
    assert proc.returncode == 0
    assert not save_later.exists(), "SAVE_LATER must be consumed"


def test_precompact_first_call_blocks_and_writes_pending(tmp_path, monkeypatch):
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    proc = _run("precompact", json.dumps({"session_id": "s1"}))
    assert proc.returncode == 2
    assert "Save memory?" in proc.stderr
    assert (tmp_path / "brain-precompact-s1").exists()
    assert (tmp_path / "brain-precompact-pending-s1").exists()


def test_precompact_second_call_passes_through(tmp_path, monkeypatch):
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    (tmp_path / "brain-precompact-s1").write_text("", encoding="utf-8")
    proc = _run("precompact", json.dumps({"session_id": "s1"}))
    assert proc.returncode == 0


def test_prompt_check_short_prompt_skips_recall(tmp_path, monkeypatch):
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    monkeypatch.setenv("SYMBIOSIS_BRAIN_RULES_ENABLED", "false")
    monkeypatch.setenv("SYMBIOSIS_BRAIN_VAULT", str(tmp_path))
    proc = _run("prompt-check", json.dumps({"session_id": "s1", "prompt": "ok"}))
    assert proc.returncode == 0
    assert "[memory:" not in proc.stdout


def test_prompt_check_slash_command_skips_recall(tmp_path, monkeypatch):
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    monkeypatch.setenv("SYMBIOSIS_BRAIN_RULES_ENABLED", "false")
    proc = _run("prompt-check", json.dumps({"session_id": "s1", "prompt": "/compact please run this"}))
    assert proc.returncode == 0
    assert "[memory:" not in proc.stdout


def test_prompt_check_pending_compact_relay(tmp_path, monkeypatch):
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    (tmp_path / "brain-precompact-pending-s1").write_text("", encoding="utf-8")
    monkeypatch.setenv("SYMBIOSIS_BRAIN_RULES_ENABLED", "false")
    monkeypatch.setenv("SYMBIOSIS_BRAIN_RECALL_ENABLED", "false")
    proc = _run("prompt-check", json.dumps({"session_id": "s1", "prompt": "hello world this is a long prompt"}))
    assert "Compaction was blocked" in proc.stdout
    # Pending consumed
    assert not (tmp_path / "brain-precompact-pending-s1").exists()


def test_prompt_check_rules_block_emitted_at_zone_crossing(tmp_path, monkeypatch):
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    (tmp_path / "brain-context-pct-s1").write_text("65", encoding="utf-8")
    monkeypatch.setenv("SYMBIOSIS_BRAIN_RECALL_ENABLED", "false")
    monkeypatch.setenv("SYMBIOSIS_BRAIN_RULES_ENABLED", "true")
    monkeypatch.setenv("SYMBIOSIS_BRAIN_RULES_ZONES", "30,60,85")
    monkeypatch.setenv("SYMBIOSIS_BRAIN_RULES_TURN_INTERVAL", "999")
    proc = _run("prompt-check", json.dumps({"session_id": "s1", "prompt": "hello world long enough prompt"}))
    assert "[rules — context 65%]" in proc.stdout


import threading


def test_concurrent_stop_hooks_produce_parseable_triggered(tmp_path, monkeypatch):
    """Five parallel `stop` hooks for the same session at 95% must leave
    `brain-triggered-<sid>` parseable (every line a valid int, no torn writes,
    no garbage). All three thresholds (40, 70, 90) must be recorded."""
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    (tmp_path / "brain-context-pct-s1").write_text("95", encoding="utf-8")

    results: list = []
    lock = threading.Lock()

    def worker():
        proc = subprocess.run(
            [sys.executable, str(HOOK), "stop"],
            input=json.dumps({"session_id": "s1"}),
            capture_output=True, text=True, encoding="utf-8",
            env={**os.environ, "TMPDIR": str(tmp_path)},
        )
        with lock:
            results.append(proc.returncode)

    threads = [threading.Thread(target=worker) for _ in range(5)]
    for t in threads: t.start()
    for t in threads: t.join(timeout=10)

    triggered = (tmp_path / "brain-triggered-s1").read_text(encoding="utf-8")
    lines = [ln.strip() for ln in triggered.splitlines() if ln.strip()]
    # Every recorded line must be a parseable integer (no torn writes)
    for ln in lines:
        assert ln.isdigit(), f"non-numeric line in triggered file: {ln!r}"
    parsed = {int(ln) for ln in lines}
    assert parsed >= {40, 70, 90}, f"missing thresholds: {parsed}"
