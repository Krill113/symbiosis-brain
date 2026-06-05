"""Tests for recall_dedup.SeenStore (Stage 1 — session-scoped recall dedup)."""
from pathlib import Path

from symbiosis_brain.recall_dedup import SeenStore, _seen_path


def test_seenstore_marks_and_detects(tmp_path: Path):
    store = SeenStore("sess1", ttl_seconds=120, base_dir=tmp_path, now=1000.0)
    assert not store.is_seen("a")
    store.record(["a", "b"])
    # reopen → persisted across instances
    store2 = SeenStore("sess1", ttl_seconds=120, base_dir=tmp_path, now=1001.0)
    assert store2.is_seen("a") and store2.is_seen("b")
    assert not store2.is_seen("c")


def test_seenstore_ttl_prunes_old(tmp_path: Path):
    SeenStore("sess1", ttl_seconds=120, base_dir=tmp_path, now=1000.0).record(["a"])
    # 121s later → 'a' is outside the TTL window
    later = SeenStore("sess1", ttl_seconds=120, base_dir=tmp_path, now=1121.0)
    assert not later.is_seen("a")
    # within the window it is still seen
    within = SeenStore("sess1", ttl_seconds=120, base_dir=tmp_path, now=1119.0)
    assert within.is_seen("a")


def test_seenstore_session_keyed(tmp_path: Path):
    SeenStore("sessA", ttl_seconds=120, base_dir=tmp_path, now=1000.0).record(["a"])
    other = SeenStore("sessB", ttl_seconds=120, base_dir=tmp_path, now=1000.0)
    assert not other.is_seen("a")  # independent file per session_id


def test_seenstore_corrupt_file_resets(tmp_path: Path):
    p = _seen_path("sessC", tmp_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("{not json", encoding="utf-8")
    store = SeenStore("sessC", ttl_seconds=120, base_dir=tmp_path, now=1000.0)
    assert not store.is_seen("a")  # corrupt → empty, no crash
    store.record(["a"])  # still functional
    assert SeenStore("sessC", ttl_seconds=120, base_dir=tmp_path, now=1001.0).is_seen("a")


def test_seenstore_sanitizes_session_id(tmp_path: Path):
    # session_id is external input (PreToolUse JSON) — must not escape base_dir
    store = SeenStore("../../evil/x y", ttl_seconds=120, base_dir=tmp_path, now=1000.0)
    store.record(["a"])
    files = list(tmp_path.glob("brain-recall-seen-*.json"))
    assert len(files) == 1
    assert files[0].parent == tmp_path  # stayed inside base_dir


def test_seenstore_record_empty_paths_noop(tmp_path: Path):
    store = SeenStore("sessE", ttl_seconds=120, base_dir=tmp_path, now=1000.0)
    store.record(["", None])  # falsy paths ignored, no crash
    assert not store.is_seen("")


def test_seenstore_reaps_dead_session_files(tmp_path: Path):
    """Files from dead sessions (mtime past the grace window) are reaped on
    construction; fresh sibling files are kept. Bounds temp-dir growth."""
    import os
    now = 2_000_000_000.0
    dead = tmp_path / "brain-recall-seen-deadsess-aaaaaaaa.json"
    dead.write_text('{"x": 1000.0}', encoding="utf-8")
    os.utime(dead, (now - 7200, now - 7200))  # 2h old → orphan
    fresh = tmp_path / "brain-recall-seen-freshsess-bbbbbbbb.json"
    fresh.write_text('{"y": 1000.0}', encoding="utf-8")
    os.utime(fresh, (now - 60, now - 60))  # 1min old → keep
    SeenStore("mysession", ttl_seconds=120, base_dir=tmp_path, now=now)  # triggers reap
    assert not dead.exists()  # reaped
    assert fresh.exists()  # kept


def test_prefix_namespaces_file(tmp_path):
    SeenStore('sess', base_dir=tmp_path, prefix='brain-route-seen-').record(['serena-symbol-work'])
    assert _seen_path('sess', tmp_path, 'brain-route-seen-').exists()
    assert not _seen_path('sess', tmp_path).exists()
