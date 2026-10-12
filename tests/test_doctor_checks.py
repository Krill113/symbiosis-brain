"""doctor must not print green over something dead.

Three states it could not see before: no compiled action rules, an empty vector
index, and a reindex lock nobody holds. Each was silent, and silence read as
health — on 2026-09-02 it printed All OK while semantic search was gone.

Every check here is paired with the state it is supposed to catch, and the
healthy case is asserted too: replacing a false "well" with a false "ill" would
be the same defect wearing the other sign.
"""
import json
import os
import sqlite3
import time
from pathlib import Path

import pytest

from symbiosis_brain import install_cli
from symbiosis_brain.action_rules import META_BASENAME
from symbiosis_brain.storage import Storage, vector_drift


def _index_dir(vault: Path) -> Path:
    d = vault / ".index"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _write_meta(vault: Path, *, total: int, compiled: int) -> None:
    (_index_dir(vault) / META_BASENAME).write_text(
        json.dumps({"rules_total": total, "rules_compiled": compiled}),
        encoding="utf-8")


class TestActionRules:
    @pytest.mark.parametrize("metadata,expected", [
        ({"rules_total": 0, "rules_compiled": 0}, 0),
        ({"rules_total": 0, "rules_compiled": 0, "skipped": []}, 0),
        ({"rules_total": 0, "rules_compiled": 0, "skipped": [{"id": "fixture", "reason": "invalid"}]}, 1),
        ({"rules_total": 0, "rules_compiled": 0, "unmatched_patterns": [{"id": "fixture"}]}, 1),
        ({"rules_total": 0, "rules_compiled": 0, "prompt_route_warnings": [{"id": "fixture"}]}, 1),
        ({"rules_total": 0, "rules_compiled": 0, "validation": "unavailable", "kept_previous": True}, 1),
    ])
    def test_no_declared_action_rules_respects_metadata_errors(self, tmp_path: Path, capsys, metadata, expected):
        (_index_dir(tmp_path) / META_BASENAME).write_text(json.dumps(metadata), encoding="utf-8")
        assert install_cli._report_action_rules(tmp_path) == expected
        out = capsys.readouterr().out
        if expected == 0:
            assert "no action rules declared" in out
            assert "NONE COMPILED" not in out
            assert "compile-action-rules" not in out
        else:
            assert "NONE COMPILED" in out
            assert "OK" not in out

    def test_zero_compiled_rules_is_an_issue(self, tmp_path: Path, capsys):
        """The state that used to print `✓ Action-recall OK`: hook wired,
        nothing to fire."""
        _write_meta(tmp_path, total=19, compiled=0)
        assert install_cli._report_action_rules(tmp_path) == 1
        assert "NONE COMPILED" in capsys.readouterr().out

    def test_missing_meta_is_an_issue(self, tmp_path: Path, capsys):
        _index_dir(tmp_path)
        assert install_cli._report_action_rules(tmp_path) == 1
        assert "NONE COMPILED" in capsys.readouterr().out

    def test_partial_compile_is_an_issue(self, tmp_path: Path, capsys):
        _write_meta(tmp_path, total=19, compiled=12)
        assert install_cli._report_action_rules(tmp_path) == 1
        assert "PARTIAL" in capsys.readouterr().out

    def test_unreadable_meta_says_so_and_is_not_an_issue(self, tmp_path: Path, capsys):
        """Cannot-read is reported as cannot-read. It is not counted as a fault
        — but it must not print OK either, which is the whole point."""
        (_index_dir(tmp_path) / META_BASENAME).write_text("{not json", encoding="utf-8")
        assert install_cli._report_action_rules(tmp_path) == 0
        out = capsys.readouterr().out
        assert "cannot read" in out
        assert "OK" not in out

    def test_healthy_is_reported_healthy(self, tmp_path: Path, capsys):
        _write_meta(tmp_path, total=19, compiled=19)
        assert install_cli._report_action_rules(tmp_path) == 0
        assert "OK (19 compiled)" in capsys.readouterr().out

    def test_partial_compile_names_each_skipped_rule(self, tmp_path: Path, capsys):
        """A refused rule stays refused on recompile, so a count alone sends the
        user to a command that changes nothing. Each rule, its reason and the
        file to fix must be on screen."""
        (_index_dir(tmp_path) / META_BASENAME).write_text(json.dumps({
            "rules_total": 3, "rules_compiled": 2,
            "skipped": [{"id": "fixture-rule", "reason": "powershell: no test_match vectors"}],
        }), encoding="utf-8")
        assert install_cli._report_action_rules(tmp_path) == 1
        out = capsys.readouterr().out
        assert "PARTIAL (2 of 3 compiled)" in out
        assert "fixture-rule: powershell: no test_match vectors" in out
        assert "tool-routing.local.json" in out

    def test_none_compiled_with_skipped_names_them_too(self, tmp_path: Path, capsys):
        (_index_dir(tmp_path) / META_BASENAME).write_text(json.dumps({
            "rules_total": 1, "rules_compiled": 0,
            "skipped": [{"id": "fixture-rule", "reason": "bad regex"}],
        }), encoding="utf-8")
        assert install_cli._report_action_rules(tmp_path) == 1
        out = capsys.readouterr().out
        assert "NONE COMPILED (0 of 1 compiled)" in out
        assert "fixture-rule: bad regex" in out


class TestCli:
    """Commands doctor names must exist on the CLI people actually have."""

    def test_doctor_advice_is_a_real_subcommand(self, tmp_path: Path, monkeypatch, capsys):
        calls = []
        monkeypatch.setattr(install_cli.action_rules, "compile_action_rules",
                            lambda v: calls.append(v))
        _write_meta(tmp_path, total=2, compiled=2)
        args = install_cli.build_parser().parse_args(
            ["compile-action-rules", "--vault", str(tmp_path)])
        assert args.func(args) == 0
        assert calls == [tmp_path]
        assert "OK (2 compiled)" in capsys.readouterr().out

    def test_compile_exit_code_keeps_a_refused_rule_visible(self, tmp_path: Path, monkeypatch):
        monkeypatch.setattr(install_cli.action_rules, "compile_action_rules", lambda v: None)
        _write_meta(tmp_path, total=2, compiled=1)
        args = install_cli.build_parser().parse_args(
            ["compile-action-rules", "--vault", str(tmp_path)])
        assert args.func(args) == 1

    def test_compile_missing_vault_is_an_error(self, tmp_path: Path):
        args = install_cli.build_parser().parse_args(
            ["compile-action-rules", "--vault", str(tmp_path / "absent")])
        assert args.func(args) == 1

    def test_version_flag(self, capsys):
        with pytest.raises(SystemExit) as exc:
            install_cli.build_parser().parse_args(["--version"])
        assert exc.value.code == 0
        out = capsys.readouterr().out
        assert out.startswith("symbiosis-brain ")
        assert out.strip() != "symbiosis-brain"


class TestVectorIndex:
    def _vault_with_notes(self, tmp_path: Path, n: int = 3) -> Path:
        storage = Storage(_index_dir(tmp_path) / "brain.db")
        for i in range(n):
            storage.upsert_note(path=f"w/n{i}.md", title=f"N{i}", content="body",
                                note_type="wiki", scope="global")
        storage.close()
        return tmp_path

    def test_empty_vector_index_is_an_issue(self, tmp_path: Path, capsys):
        """The 2026-09-02 state exactly: notes present, notes_vec empty, every
        surface green."""
        vault = self._vault_with_notes(tmp_path)
        conn = sqlite3.connect(_index_dir(vault) / "brain.db")
        conn.execute("CREATE TABLE IF NOT EXISTS notes_vec (rowid INTEGER PRIMARY KEY)")
        conn.commit()
        conn.close()

        assert install_cli._report_vector_index(vault) == 1
        assert "INCOMPLETE" in capsys.readouterr().out

    def test_complete_index_is_healthy(self, tmp_path: Path, capsys):
        vault = self._vault_with_notes(tmp_path, n=2)
        conn = sqlite3.connect(_index_dir(vault) / "brain.db")
        conn.execute("CREATE TABLE IF NOT EXISTS notes_vec (rowid INTEGER PRIMARY KEY)")
        conn.executemany("INSERT INTO notes_vec (rowid) VALUES (?)", [(1,), (2,)])
        conn.commit()
        conn.close()

        assert install_cli._report_vector_index(vault) == 0
        assert "OK (2 of 2 indexed)" in capsys.readouterr().out

    def test_a_real_sqlite_vec_table_is_counted(self, tmp_path: Path, capsys):
        """The regression that made this check lie.

        `notes_vec` is a sqlite-vec VIRTUAL table. A connection that has not
        loaded the extension gets the same error querying it as one where the
        table does not exist, so the first version of this check announced "no
        notes_vec table" against a live vault holding 1628 indexed notes. The
        plain-table tests above cannot catch that — only a real vec0 table can.
        """
        import sqlite_vec

        vault = self._vault_with_notes(tmp_path, n=2)
        conn = sqlite3.connect(_index_dir(vault) / "brain.db")
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
        conn.execute("CREATE VIRTUAL TABLE notes_vec USING vec0(embedding float[4])")
        conn.executemany(
            "INSERT INTO notes_vec (rowid, embedding) VALUES (?, ?)",
            [(1, sqlite_vec.serialize_float32([0.1, 0.2, 0.3, 0.4])),
             (2, sqlite_vec.serialize_float32([0.5, 0.6, 0.7, 0.8]))],
        )
        conn.commit()
        conn.close()

        assert install_cli._report_vector_index(vault) == 0
        assert "OK (2 of 2 indexed)" in capsys.readouterr().out

    def test_unreadable_index_is_not_reported_as_absent(self, tmp_path: Path, capsys):
        """A connection that cannot query notes_vec knows nothing about whether
        an index exists. The first version of this check said "no notes_vec
        table" against a vault holding 1628 of them, because a sqlite-vec
        virtual table raises the same error when the extension is not loaded as
        when the table is genuinely missing."""
        vault = self._vault_with_notes(tmp_path)   # no notes_vec at all
        assert install_cli._report_vector_index(vault) == 0
        out = capsys.readouterr().out
        assert "could not be read" in out
        assert "OK" not in out


class TestPidLiveness:
    def test_this_process_is_alive(self):
        assert install_cli._pid_is_alive(os.getpid()) is True

    def test_a_pid_that_cannot_exist_is_not_alive(self):
        """Deliberately NOT os.kill(pid, 0): on Windows that is TerminateProcess,
        so the POSIX one-liner would kill whatever it was asked about. If this
        test ever starts killing the runner, that idiom has crept back in."""
        assert install_cli._pid_is_alive(0x7FFFFFFE) is False

    def test_a_nonsense_pid_is_unknown_not_alive(self):
        assert install_cli._pid_is_alive(0) is None


class TestReindexLock:
    def _lockfile(self, vault: Path) -> Path:
        import hashlib
        import tempfile
        tag = hashlib.sha256(
            str(vault / ".index" / "brain.db").encode()).hexdigest()[:12]
        return Path(tempfile.gettempdir()) / f"sb-reindex-{tag}.lock"

    def test_the_durations_it_quotes_match_the_ones_search_enforces(self):
        """doctor restates search's two timeouts instead of importing them, so
        the command stays free of numpy. A restated number is one that can drift
        away from the original silently, and a message quoting the wrong
        threshold is worse than one quoting none: the reader would wait for a
        sweep that comes later, or delete a lock that was about to clear."""
        from symbiosis_brain import search

        assert install_cli._REINDEX_WAIT_MIN == search._REINDEX_LOCK_WAIT_S // 60
        assert install_cli._REINDEX_STALE_MIN == search._REINDEX_LOCK_STALE_S // 60

    def test_lock_held_by_a_dead_pid_is_an_issue(self, tmp_path: Path, capsys):
        """Every search waits on this lock before giving up, so one left behind
        is minutes of silence per query with nothing on screen to explain it."""
        lock = self._lockfile(tmp_path)
        lock.write_text(f"{0x7FFFFFFE}\n{int(time.time())}\n", encoding="utf-8")
        try:
            assert install_cli._report_reindex_lock(tmp_path) == 1
            out = capsys.readouterr().out
            assert "NOT running" in out
            assert str(lock) in out, "the message must name the file to delete"
        finally:
            lock.unlink(missing_ok=True)

    def test_lock_held_by_a_live_pid_is_not_an_issue(self, tmp_path: Path, capsys):
        """A reindex in progress is work, not a fault."""
        lock = self._lockfile(tmp_path)
        lock.write_text(f"{os.getpid()}\n{int(time.time())}\n", encoding="utf-8")
        try:
            assert install_cli._report_reindex_lock(tmp_path) == 0
            assert "live pid" in capsys.readouterr().out
        finally:
            lock.unlink(missing_ok=True)

    def test_no_lock_is_healthy(self, tmp_path: Path, capsys):
        self._lockfile(tmp_path).unlink(missing_ok=True)
        assert install_cli._report_reindex_lock(tmp_path) == 0
        assert "OK (not held)" in capsys.readouterr().out
