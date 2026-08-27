import os
from pathlib import Path

import pytest

from symbiosis_brain.atomic_write import atomic_write_text


class TestAtomicWriteText:
    def test_creates_new_file(self, tmp_path: Path):
        target = tmp_path / "note.md"
        atomic_write_text(target, "hello")
        assert target.read_text(encoding="utf-8") == "hello"

    def test_overwrites_existing(self, tmp_path: Path):
        target = tmp_path / "note.md"
        target.write_text("old", encoding="utf-8")
        atomic_write_text(target, "new")
        assert target.read_text(encoding="utf-8") == "new"

    def test_leaves_no_temp_file_on_success(self, tmp_path: Path):
        target = tmp_path / "note.md"
        atomic_write_text(target, "hello")
        leftovers = [p for p in tmp_path.iterdir() if p.name != "note.md"]
        assert leftovers == []

    def test_preserves_original_on_failure(self, tmp_path: Path, monkeypatch):
        target = tmp_path / "note.md"
        target.write_text("original", encoding="utf-8")

        def boom(*args, **kwargs):
            raise OSError("simulated failure")

        monkeypatch.setattr(os, "replace", boom)
        with pytest.raises(OSError):
            atomic_write_text(target, "new")
        assert target.read_text(encoding="utf-8") == "original"
        leftovers = [p for p in tmp_path.iterdir() if p.name != "note.md"]
        assert leftovers == []

    def test_creates_parent_dirs(self, tmp_path: Path):
        target = tmp_path / "nested" / "deep" / "note.md"
        atomic_write_text(target, "hi")
        assert target.read_text(encoding="utf-8") == "hi"

    def test_fsyncs_the_temp_file_before_replace(self, tmp_path: Path, monkeypatch):
        """A crash between write and rename must not leave a zero-filled target:
        the temp file is flushed and fsync-ed before os.replace (2026-08-27: a
        sudden reboot turned a 45 KB note into 45 KB of NUL bytes)."""
        import os as _os
        calls: list[str] = []
        real_fsync, real_replace = _os.fsync, _os.replace
        monkeypatch.setattr(_os, "fsync", lambda fd: (calls.append("fsync"), real_fsync(fd)) and None)
        monkeypatch.setattr(_os, "replace", lambda a, b: (calls.append("replace"), real_replace(a, b)) and None)
        target = tmp_path / "note.md"
        atomic_write_text(target, "body")
        assert target.read_text(encoding="utf-8") == "body"
        assert "fsync" in calls and calls.index("fsync") < calls.index("replace")
