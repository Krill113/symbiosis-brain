"""Concrete counterexamples found in the independent staging review."""
import json
import os
import subprocess

import pytest
from tests.test_statusline_boundaries import run_raw, HOOKS, BASH
from tests.test_context_observation_consumers import _env


@pytest.mark.parametrize('name', ['Modèle', 'Модель', '模型', 'Model 🧠'])
def test_unicode_escape_matches_literal_name(tmp_path, name):
    payload = {'model': {'id': 'fixture', 'display_name': name}}
    literal = run_raw(tmp_path, json.dumps(payload, ensure_ascii=False))
    escaped = run_raw(tmp_path, json.dumps(payload, ensure_ascii=True))
    assert name in literal
    assert escaped == literal


@pytest.mark.skipif(os.name != 'nt', reason='Native Windows path regression')
def test_backslash_invocation_preserves_stop(tmp_path):
    (tmp_path / 'brain-context-pct-fixture').write_bytes(b'45\n')
    (tmp_path / 'brain-context-state-fixture').write_bytes(b'45\t1000\n')
    result = subprocess.run([BASH, str(HOOKS / 'brain-save-trigger.sh'), 'stop'],
                            input='{"session_id":"fixture"}', cwd=tmp_path,
                            env=_env(tmp_path), capture_output=True, text=True,
                            encoding='utf-8', timeout=10)
    assert result.returncode == 2
    assert '45%' in result.stderr


@pytest.mark.parametrize('record,legacy', [
    ('45\t1000\nunknown\t1000\n', '45\n'),
    ('45\t1000\n', '45\n99\n'),
    ('45\t1000\nextra', '45\n'),
])
def test_multiline_observation_never_counts_as_fresh(tmp_path, record, legacy):
    (tmp_path / 'brain-context-state-fixture').write_bytes(record.encode())
    (tmp_path / 'brain-context-pct-fixture').write_bytes(legacy.encode())
    result = subprocess.run([BASH, '-c', '. "$1"; sb_context_read fixture; printf "%s" "$SB_CONTEXT_PCT"',
                             '_', (HOOKS / 'sb-hooklib.sh').as_posix()], env=_env(tmp_path),
                            cwd=tmp_path, capture_output=True, text=True, encoding='utf-8', timeout=10)
    assert result.returncode == 0
    assert result.stdout == ''
