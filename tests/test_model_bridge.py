"""CP-5 / I-14 (слайс 3), I-15 — мост модели: строка состояния -> сервер.

Всё в pytest-семействе: bash вызывается через tests/_bash_resolver.py, потому что
tests/test-*.sh на Windows не гоняются вовсе (.github/workflows/test.yml:92-93),
а именно на Windows живёт разница путей TEMP (§11.7 спеки).

Синтетика: модель `Test Model 9` / `test-model-9`, вторая — `other-test-model-7`,
сессия — `s9`. Реальных идентификаторов здесь быть не должно.
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


def test_export_writes_the_bridge_under_claude_pid(tmp_path):
    proc = _run_export(tmp_path, MODEL_JSON, claude_pid=424242)
    assert proc.returncode == 0, proc.stderr
    assert _bridge_files(tmp_path) == ["brain-model-424242"]
    raw = (tmp_path / "brain-model-424242").read_bytes().decode("utf-8")
    assert raw.endswith("\n")
    model_id, display_name, epoch = raw[:-1].split("\t")
    assert (model_id, display_name) == ("test-model-9", "Test Model 9")
    assert epoch.isdigit() and abs(int(epoch) - int(time.time())) < 300


def test_export_without_claude_pid_falls_back_to_the_session_key(tmp_path):
    """Резерв для клиентов без CLAUDE_PID (§3.4): ключа по PID быть не должно."""
    proc = _run_export(tmp_path, MODEL_JSON, claude_pid=None)
    assert proc.returncode == 0, proc.stderr
    assert _bridge_files(tmp_path) == ["brain-model-sid-s9"]


def test_export_writes_nothing_without_a_model_block(tmp_path):
    proc = _run_export(tmp_path, NO_MODEL_JSON, claude_pid=424242)
    assert proc.returncode == 0, proc.stderr
    assert _bridge_files(tmp_path) == []


def test_export_stays_fork_free(tmp_path):
    """Контракт строки состояния (hooks/sb-hooklib.sh:4-9): никаких подстановок
    команд, пайпов и внешних бинарей в добавленном блоке."""
    text = EXPORT.read_text(encoding="utf-8")
    block = text.split("# 3.", 1)[1]
    for forbidden in ("$(", "`", "| ", "jq", "python", "sed ", "grep ", "cut "):
        assert forbidden not in block, forbidden


# ============================ читатель: I-14 п. 1-5 ==========================

OWN_PID = 424242


@pytest.fixture
def bridge_dir(tmp_path, monkeypatch):
    """Свой каталог моста на каждый тест: сессионная фикстура conftest двигает
    TMPDIR/TEMP на ОБЩИЙ каталог, куда могут попасть файлы других тестов, и тогда
    «ровно один свежий чужой файл» перестаёт быть детерминированным."""
    d = tmp_path / "bridge"
    d.mkdir()
    monkeypatch.setenv("TMPDIR", str(d))
    monkeypatch.setenv("TEMP", str(d))
    monkeypatch.setattr(os, "getppid", lambda: OWN_PID)
    return d


def _write_bridge(d: pathlib.Path, key: str, content: str, age_s: float = 0.0):
    """newline='' обязателен: без него Windows превратит \n в \r\n и строгий
    разбор честно скажет `unknown` там, где тест ждал модель."""
    p = d / key
    p.write_text(content, encoding="utf-8", newline="")
    if age_s:
        t = time.time() - age_s
        os.utime(p, (t, t))
    return p


VALID = "test-model-9\tTest Model 9\t1787768444\n"


def test_own_file_is_read_by_ppid(bridge_dir):
    _write_bridge(bridge_dir, f"brain-model-{OWN_PID}", VALID)
    assert provenance.model_from_bridge() == "test-model-9"


def test_own_fresh_file_wins_over_other_windows(bridge_dir):
    _write_bridge(bridge_dir, f"brain-model-{OWN_PID}", VALID)
    _write_bridge(bridge_dir, "brain-model-999999",
                  "other-test-model-7\tOther Test Model 7\t1787768444\n")
    assert provenance.model_from_bridge() == "test-model-9"


def test_stale_own_file_is_unknown_and_no_fallback_scan(bridge_dir):
    """I-14 п. 1: наличие файла с нашим ключом означает, что лаунчер стандартный и
    ключ верный, — значит данные просто устарели, и повода заглядывать в чужие окна
    нет. Чужой файл здесь СВЕЖИЙ и всё равно не берётся."""
    _write_bridge(bridge_dir, f"brain-model-{OWN_PID}", VALID,
                  age_s=provenance.BRIDGE_TTL_SECONDS + 60)
    _write_bridge(bridge_dir, "brain-model-999999",
                  "other-test-model-7\tOther Test Model 7\t1787768444\n")
    assert provenance.model_from_bridge() == "unknown"


def test_exactly_one_fresh_foreign_file_is_used(bridge_dir):
    """Своего файла нет вовсе — нестандартный лаунчер (uv run, шим)."""
    _write_bridge(bridge_dir, "brain-model-999999",
                  "other-test-model-7\tOther Test Model 7\t1787768444\n")
    assert provenance.model_from_bridge() == "other-test-model-7"


def test_two_fresh_foreign_files_are_unknown(bridge_dir):
    """Молчать честнее, чем подписать ноту моделью соседнего окна."""
    _write_bridge(bridge_dir, "brain-model-999999", VALID)
    _write_bridge(bridge_dir, "brain-model-888888",
                  "other-test-model-7\tOther Test Model 7\t1787768444\n")
    assert provenance.model_from_bridge() == "unknown"


def test_stale_foreign_files_do_not_count(bridge_dir):
    _write_bridge(bridge_dir, "brain-model-999999", VALID)
    _write_bridge(bridge_dir, "brain-model-888888",
                  "other-test-model-7\tOther Test Model 7\t1787768444\n",
                  age_s=provenance.BRIDGE_TTL_SECONDS + 60)
    assert provenance.model_from_bridge() == "test-model-9"


def test_no_files_at_all_is_unknown(bridge_dir):
    assert provenance.model_from_bridge() == "unknown"


def test_missing_temp_dir_is_unknown(tmp_path, monkeypatch):
    monkeypatch.setenv("TMPDIR", str(tmp_path / "does-not-exist"))
    monkeypatch.setenv("TEMP", str(tmp_path / "does-not-exist"))
    monkeypatch.setattr(os, "getppid", lambda: OWN_PID)
    assert provenance.model_from_bridge() == "unknown"


@pytest.mark.parametrize("content", [
    "test-model-9\tTest Model 9\t1787768444",        # оборвана: нет финального \n
    "test-model-9\tTest Model 9\n",                  # два поля
    "test-model-9\tTest Model 9\t1787768444\textra\n",  # четыре поля
    "test-model-9\tTest Model 9\tnot-a-number\n",    # третье поле не целое
    "test-model-9\tTest Mod",                        # обрыв на середине
    "",                                              # пусто
    "\n",                                            # только перевод строки
    "a\tb\t1\na\tb\t1\n",                            # две строки
])
def test_a_truncated_or_malformed_line_is_unknown(bridge_dir, content):
    """Запись неатомарна (fork-free строка состояния не может mv-нуть файл на
    место), а Claude Code отменяет её на каждом событии — значит усечённая строка
    неотличима от валидной, и разбор обязан быть строгим (I-14 п. 2)."""
    _write_bridge(bridge_dir, f"brain-model-{OWN_PID}", content)
    assert provenance.model_from_bridge() == "unknown"


def test_slug_falls_back_to_display_name(bridge_dir):
    """model_id пуст (клиент его не прислал) -> слаг из display_name (I-14 п. 4)."""
    _write_bridge(bridge_dir, f"brain-model-{OWN_PID}", "\tTest Model 9\t1787768444\n")
    assert provenance.model_from_bridge() == "test-model-9"


def test_slug_normalizes_punctuation_and_case(bridge_dir):
    _write_bridge(bridge_dir, f"brain-model-{OWN_PID}", "\tTest  Model  9.5 (Beta)\t1787768444\n")
    assert provenance.model_from_bridge() == "test-model-9-5-beta"


def test_slug_of_an_unslugifiable_name_is_unknown(bridge_dir):
    _write_bridge(bridge_dir, f"brain-model-{OWN_PID}", "\t---\t1787768444\n")
    assert provenance.model_from_bridge() == "unknown"


def test_reader_never_raises_when_the_temp_path_is_a_file(tmp_path, monkeypatch):
    """Fail-open: любая ошибка -> unknown, функция не бросает (I-14 п. 3)."""
    fake = tmp_path / "not-a-dir"
    fake.write_text("x", encoding="utf-8")
    monkeypatch.setenv("TMPDIR", str(fake))
    monkeypatch.setenv("TEMP", str(fake))
    monkeypatch.setattr(os, "getppid", lambda: OWN_PID)
    assert provenance.model_from_bridge() == "unknown"


def test_now_parameter_drives_the_ttl(bridge_dir):
    p = _write_bridge(bridge_dir, f"brain-model-{OWN_PID}", VALID)
    mtime = p.stat().st_mtime
    assert provenance.model_from_bridge(now=mtime + 10) == "test-model-9"
    assert provenance.model_from_bridge(
        now=mtime + provenance.BRIDGE_TTL_SECONDS + 10) == "unknown"


def test_writer_and_reader_agree_end_to_end(tmp_path, monkeypatch):
    """Единственный тест, где bash-писатель и python-читатель встречаются:
    ключ, разделители и TTL должны совпадать буквально."""
    d = tmp_path / "e2e"
    d.mkdir()
    proc = _run_export(d, MODEL_JSON, claude_pid=OWN_PID)
    assert proc.returncode == 0, proc.stderr
    monkeypatch.setenv("TMPDIR", str(d))
    monkeypatch.setenv("TEMP", str(d))
    monkeypatch.setattr(os, "getppid", lambda: OWN_PID)
    assert provenance.model_from_bridge() == "test-model-9"


def test_written_by_carries_the_model_from_the_bridge(tmp_path, monkeypatch):
    d = tmp_path / "wb"
    d.mkdir()
    _write_bridge(d, f"brain-model-{OWN_PID}", VALID)
    monkeypatch.setenv("TMPDIR", str(d))
    monkeypatch.setenv("TEMP", str(d))
    monkeypatch.setattr(os, "getppid", lambda: OWN_PID)
    monkeypatch.setattr(provenance, "client_id", lambda app: "testclient/9.9.9")
    from datetime import date
    assert provenance.written_by_value(None, today=date(2026, 1, 2)) == (
        "testclient/9.9.9 test-model-9 2026-01-02"
    )


def test_session_start_reaps_stale_bridge_files(tmp_path):
    """Оппортунистический GC: файлы моста мёртвых окон не должны копиться в TEMP
    и делать «ровно один свежий чужой файл» неверным навсегда."""
    import shutil

    sb = tmp_path / "sb"
    sb.mkdir()
    stub_dir = tmp_path / "stub"
    stub_dir.mkdir()
    # SessionStart прогревает MCP-роестр через `claude mcp list`, а тот
    # health-check'ает КАЖДЫЙ сервер, включая второй `symbiosis-brain serve` против
    # живого vault. No-op заглушка держит `command -v claude` истинным, не запуская
    # ничего настоящего (образец — tests/test_brain_save_trigger_routing.py:45-51).
    stub_claude = stub_dir / "claude"
    stub_claude.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    stub_claude.chmod(0o755)

    stale = _write_bridge(sb, "brain-model-111111", VALID, age_s=2 * 60 * 60)
    fresh = _write_bridge(sb, "brain-model-222222", VALID)
    stale_sid = _write_bridge(sb, "brain-model-sid-s9", VALID, age_s=2 * 60 * 60)

    env = dict(os.environ)
    env["TMPDIR"] = str(sb)
    env["TEMP"] = str(sb)
    env["PATH"] = str(stub_dir) + os.pathsep + env.get("PATH", "")
    env["SYMBIOSIS_BRAIN_RULES_ENABLED"] = "false"
    env.pop("SYMBIOSIS_BRAIN_VAULT", None)
    # .as_posix(): brain-session-start.sh derives its own dir from
    # ${BASH_SOURCE[0]%/*} to source sb-hooklib.sh; MSYS bash does not translate a
    # backslash-separated argv path, so %/* strips nothing and the source silently
    # fails (`|| exit 0`) before the GC block ever runs. A forward-slash path avoids
    # the whole class of bug (see _run_export above, same fix for the same reason).
    proc = subprocess.run(
        [_bash(), SESSION_START.as_posix()],
        input=json.dumps({"session_id": "s9"}),
        text=True, encoding="utf-8", env=env, capture_output=True,
    )
    assert proc.returncode == 0, proc.stderr

    assert not stale.exists(), "a two-hour-old bridge file must be reaped"
    assert not stale_sid.exists(), "the sid-keyed fallback must be reaped too"
    assert fresh.exists(), "a fresh bridge file must survive SessionStart"


def test_bridge_format_is_documented():
    """Формат моста документируется там же, где мост лимитов (hooks/README.md:72-99):
    иначе следующий читатель полезет разбирать его по коду хука."""
    readme = (HOOKS / "README.md").read_text(encoding="utf-8")
    assert "### Bridge 3" in readme
    assert "brain-model-<CLAUDE_PID>" in readme
    assert "brain-model-sid-" in readme
    assert "model_from_bridge" in readme
    assert "two status-line bridges" not in readme    # строка таблицы обновлена
