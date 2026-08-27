"""Hook-path telemetry (Stage 2, CP-3): origin, --hook-started-at, locale.

Everything synthetic: made-up session ids, made-up transcript paths, tmp_path
vaults. HOME/USERPROFILE are redirected for every subprocess (§11.6) so the
suite never reads the developer's ~/.claude and never writes into it.
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

from symbiosis_brain import retrieval_log
from symbiosis_brain.__main__ import parse_hook_started_at

try:                                    # same dual import as test_action_rules.py:21-23
    from tests._bash_resolver import _bash
except ImportError:                     # pragma: no cover - layout-dependent
    from _bash_resolver import _bash


# ---------- Task 3.1: detect_origin (I-4, §2.5) ----------

def test_detect_origin_without_payload_is_unknown():
    assert retrieval_log.detect_origin(None) == "unknown"
    assert retrieval_log.detect_origin("not a dict") == "unknown"


def test_detect_origin_defaults_to_main(monkeypatch):
    monkeypatch.delenv("CLAUDE_CODE_CHILD_SESSION", raising=False)
    assert retrieval_log.detect_origin({"session_id": "s-1"}) == "main"


def test_detect_origin_env_signal_wins(monkeypatch):
    """Signal 1 is MEASURED: a bash process spawned by a subagent carries
    CLAUDE_CODE_CHILD_SESSION=1 [замер лида]. It is checked first."""
    monkeypatch.setenv("CLAUDE_CODE_CHILD_SESSION", "1")
    assert retrieval_log.detect_origin({"session_id": "s-1"}) == "subagent"


def test_detect_origin_ignores_other_values_of_the_env_signal(monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_CHILD_SESSION", "0")
    assert retrieval_log.detect_origin({}) == "main"


# ---------- Task 3.1, ADAPTED (deviation, see review/exec-CP-3.md): the CP-3
# preflight (review/preflight-step-b/README.md, done by the owner 2026-08-27,
# BEFORE this checkpoint started) measured signal 2 of §2.5 — a `subagents`
# segment inside `transcript_path` — and found it FALSE: transcript_path and
# session_id are ALWAYS the parent session's, even for a tool call made from
# inside a subagent (both an Agent-tool sub-agent and a workflow-subagent).
# Per 00-plan §2 Р6 an unconfirmed hypothesis is struck, not patched — but the
# SAME preflight measured the signal that actually works and named it under
# "Выводы для CP-3 (detect_origin)": a non-empty `agent_id` in the hook
# payload. `agent_type` is informational only; no DDL column exists for it and
# none is added on this checkpoint (00-plan forbids scope growth here). ----------

def test_detect_origin_uses_agent_id_from_the_payload(monkeypatch):
    monkeypatch.delenv("CLAUDE_CODE_CHILD_SESSION", raising=False)
    payload = {"session_id": "parent-sid", "agent_id": "aabbccdd1122",
              "agent_type": "workflow-subagent"}
    assert retrieval_log.detect_origin(payload) == "subagent"


def test_detect_origin_empty_or_missing_agent_id_is_main(monkeypatch):
    monkeypatch.delenv("CLAUDE_CODE_CHILD_SESSION", raising=False)
    assert retrieval_log.detect_origin({"agent_id": None}) == "main"
    assert retrieval_log.detect_origin({"agent_id": ""}) == "main"
    assert retrieval_log.detect_origin({"session_id": "parent-sid"}) == "main"


def test_detect_origin_struck_hypothesis_transcript_path_segment_is_ignored(
        monkeypatch):
    """The struck hypothesis, proven struck: a `transcript_path` carrying a
    `subagents` path segment must NOT flip origin on its own — measured false
    in the CP-3 preflight (review/preflight-step-b/README.md)."""
    monkeypatch.delenv("CLAUDE_CODE_CHILD_SESSION", raising=False)
    payload = {"session_id": "parent-sid",
              "transcript_path": "/tmp/projects/p/parent-sid/subagents/a1/x.jsonl"}
    assert retrieval_log.detect_origin(payload) == "main"


# ---------- Task 3.2: parse_hook_started_at (I-9) ----------

def test_parse_hook_started_at_accepts_whole_microseconds():
    # Момент берётся на секунду В ПРОШЛОМ: `time.time()` в тесте и `time.time()`
    # внутри парсера — два разных вызова, и «сейчас» первого может оказаться
    # позже «сейчас» второго на границе кванта часов. Окно I-9 отсекает будущее,
    # так что мигание тут заложено конструкцией, а не случайностью (замер на
    # тулчейне репо: 2 падения из 200 на целых микросекундах, 16 из 200 — ниже).
    micros = str(int((time.time() - 1) * 1_000_000))
    parsed = parse_hook_started_at(micros)
    assert parsed is not None
    assert abs(parsed - time.time()) < 5


def test_parse_hook_started_at_accepts_a_comma_as_decimal_mark():
    """A hook running under a ru/de locale would emit a comma if the bash
    substitution ever regressed; the parser must not turn that into exit 2."""
    secs = f"{time.time() - 1:.6f}".replace(".", ",")   # то же окно, см. тест выше
    parsed = parse_hook_started_at(secs)
    assert parsed is not None
    assert abs(parsed - time.time()) < 5


def test_parse_hook_started_at_treats_garbage_as_not_given():
    assert parse_hook_started_at("") is None
    assert parse_hook_started_at("   ") is None
    assert parse_hook_started_at("not-a-number") is None
    assert parse_hook_started_at("-5") is None
    assert parse_hook_started_at("0") is None


def test_parse_hook_started_at_rejects_values_outside_the_window():
    assert parse_hook_started_at(str(int((time.time() - 7200) * 1_000_000))) is None
    assert parse_hook_started_at(str(int((time.time() + 600) * 1_000_000))) is None


def test_parse_hook_started_at_never_raises():
    for raw in (None, "", "1e400", "nan", "inf", "1,2,3", "\x00"):
        parse_hook_started_at(raw)      # must not raise, value irrelevant


# ---------- §11.7: the comma-locale trap, in the pytest family ----------

def test_the_bash_idiom_eats_both_decimal_marks(tmp_path: Path):
    """§2.8 measured: EPOCHREALTIME prints the fraction with the CURRENT
    locale's decimal mark — a dot under C/en_US, a comma under ru/de. The
    substitution must eat BOTH, and this test proves it WITHOUT depending on
    which locales the machine happens to have generated: the same idiom is
    applied to a literal that already carries a comma. Setting LC_NUMERIC on a
    box where de_DE.UTF-8 was never generated changes nothing at all, and the
    old shape of this test was therefore green by construction there.

    The SHIPPED hook text is checked separately, in §6
    (`assert 'SB_T0=${EPOCHREALTIME/[.,]/}' in text`): that the idiom is right
    and that it reached the file are two different failures.

    Written in the pytest family on purpose (§11.7): tests/test-*.sh do not run
    on Windows at all (.github/workflows/test.yml:92-93), and Windows is where
    the owner lives.
    """
    bash = _bash()
    script = tmp_path / "t0.sh"
    script.write_text(
        'dot="1756200000.123456"\n'
        'comma="1756200000,123456"\n'
        'echo "${dot/[.,]/}"\n'
        'echo "${comma/[.,]/}"\n'
        "raw=${EPOCHREALTIME/[.,]/}\n"
        '[ -n "$raw" ] || raw="$(date +%s)000000"\n'
        'echo "$raw"\n',
        encoding="utf-8", newline="\n",
    )
    proc = subprocess.run([bash, str(script)], capture_output=True, text=True,
                          timeout=60)
    assert proc.returncode == 0, proc.stderr
    out = proc.stdout.split()
    assert out[0] == "1756200000123456"
    assert out[1] == "1756200000123456"     # запятая съедается так же, как точка
    assert out[2].isdigit(), f"separator leaked into the timestamp: {out[2]!r}"
    assert parse_hook_started_at(out[2]) is not None


def test_bash_under_a_real_comma_locale_still_emits_digits_only(tmp_path: Path):
    """Дополнительный прогон под НАСТОЯЩЕЙ локалью с запятой. Пропускается, если
    de_DE.UTF-8 в системе не сгенерирована — а это и обычный Ubuntu-раннер CI, и
    типичная машина разработчика: там bash печатает точку, и проверять нечего.
    `pytest.fail` вместо `skip` здесь ставить нельзя — он уронил бы Ubuntu-джоб
    на отсутствии локали, то есть на том, что к коду отношения не имеет.
    Детерминированную половину проверки несёт тест выше.
    """
    bash = _bash()
    env = {**os.environ, "LC_ALL": "", "LC_NUMERIC": "de_DE.UTF-8"}
    probe = tmp_path / "probe.sh"
    probe.write_text('printf %s "$EPOCHREALTIME"\n', encoding="utf-8", newline="\n")
    raw = subprocess.run([bash, str(probe)], capture_output=True, text=True,
                         timeout=60, env=env).stdout
    if "," not in raw:
        pytest.skip(f"de_DE.UTF-8 не даёт запятую в этой системе (сырое: {raw!r})")

    script = tmp_path / "t0.sh"
    script.write_text(
        "SB_T0=${EPOCHREALTIME/[.,]/}\n"
        '[ -n "$SB_T0" ] || SB_T0="$(date +%s)000000"\n'
        'printf %s "$SB_T0"\n',
        encoding="utf-8", newline="\n",
    )
    proc = subprocess.run([bash, str(script)], capture_output=True, text=True,
                          timeout=60, env=env)
    assert proc.returncode == 0, proc.stderr
    value = proc.stdout.strip()
    assert value.isdigit(), f"separator leaked into the timestamp: {value!r}"
    assert parse_hook_started_at(value) is not None


# ---------- Task 3.3: both trigger scripts pass the timestamp (I-10) ----------

def _hook_text(name: str) -> str:
    root = Path(__file__).resolve().parents[1]
    return (root / "hooks" / name).read_text(encoding="utf-8")


@pytest.mark.parametrize("name", ["brain-save-trigger.sh", "brain-pre-action-trigger.sh"])
def test_trigger_script_computes_and_forwards_the_timestamp(name):
    text = _hook_text(name)
    assert "SB_T0=${EPOCHREALTIME/[.,]/}" in text, (
        "the substitution must eat BOTH separators (I-10, §2.8)"
    )
    assert 'SB_T0="$(date +%s)000000"' in text, "bash 4 fallback missing"
    assert '--hook-started-at "$SB_T0"' in text


def test_save_trigger_forwards_the_timestamp_on_both_branches():
    """brain-save-trigger.sh calls the CLI twice — the uv branch and the plain
    PY_BIN branch (brain-save-trigger.sh:233-246). A flag on one of them only
    would make e2e_ms depend on whether uv happens to be on PATH."""
    text = _hook_text("brain-save-trigger.sh")
    assert text.count('--hook-started-at "$SB_T0"') == 2


@pytest.mark.parametrize("name", ["brain-save-trigger.sh", "brain-pre-action-trigger.sh"])
def test_trigger_script_still_parses(name):
    bash = _bash()
    root = Path(__file__).resolve().parents[1]
    proc = subprocess.run([bash, "-n", str(root / "hooks" / name)],
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
