"""Freshness is shared by the context bridge and its consumers."""
import os
from pathlib import Path
import subprocess

import pytest
from tests._bash_resolver import _find_bash

HOOKS = Path(__file__).resolve().parents[1] / 'hooks'
BASH = _find_bash()
pytestmark = pytest.mark.skipif(BASH is None, reason='Bash is required')


def _env(tmp_path):
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(('SB_', 'SYMBIOSIS_', 'CLAUDE_', 'ANTHROPIC_'))}
    env.update({'HOME': str(tmp_path), 'USERPROFILE': str(tmp_path),
                'TMPDIR': tmp_path.as_posix(), 'TMP': str(tmp_path),
                'TEMP': str(tmp_path), 'BASH_ENV': '', 'ENV': '', 'SB_NOW': '1000'})
    return env


@pytest.mark.parametrize('record,legacy,expected', [
    ('45\t1000\n', '45\n', '45'), ('0\t1000\n', '0\n', '0'),
    ('45\t940\n', '45\n', '45'), ('45\t939\n', '45\n', ''),
    ('45\t1001\n', '45\n', ''), ('45\tbad\n', '45\n', ''),
    ('45\t1000', '45\n', ''), ('45\t1000\n', '44\n', ''),
    ('unknown\t1000\n', '45\n', ''), ('101\t1000\n', '101\n', ''),
])
def test_observation_reader(tmp_path, record, legacy, expected):
    (tmp_path / 'brain-context-state-fixture').write_text(record, encoding='utf-8', newline='')
    (tmp_path / 'brain-context-pct-fixture').write_text(legacy, encoding='utf-8', newline='')
    result = subprocess.run(
        [BASH, '-c', '. "$1"; sb_context_read fixture; printf "%s" "$SB_CONTEXT_PCT"',
         '_', (HOOKS / 'sb-hooklib.sh').as_posix()],
        cwd=tmp_path, env=_env(tmp_path), capture_output=True,
        text=True, encoding='utf-8', timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert not result.stderr, result.stderr
    assert result.stdout == expected


@pytest.mark.parametrize('state,expected', [('45\t1000\n', '45'), ('45\t1\n', None), ('unknown\t1000\n', None)])
def test_save_marker_only_uses_fresh_observation(tmp_path, state, expected):
    (tmp_path / 'brain-context-pct-fixture').write_text('45\n', encoding='utf-8', newline='')
    (tmp_path / 'brain-context-state-fixture').write_text(state, encoding='utf-8', newline='')
    result = subprocess.run([BASH, (HOOKS / 'brain-save-marker.sh').as_posix()],
                            input='{"session_id":"fixture","tool_response":{"text":"Saved"}}',
                            env=_env(tmp_path), cwd=tmp_path, capture_output=True,
                            text=True, encoding='utf-8', timeout=10)
    assert result.returncode == 0
    marker = tmp_path / 'brain-last-save-pct-fixture'
    assert (marker.read_text(encoding='utf-8').strip() if marker.exists() else None) == expected


@pytest.mark.parametrize('state', ['45\t1\n', 'unknown\t1000\n'])
def test_stop_does_not_trigger_on_unavailable_observation(tmp_path, state):
    (tmp_path / 'brain-context-pct-fixture').write_text('45\n', encoding='utf-8', newline='')
    (tmp_path / 'brain-context-state-fixture').write_text(state, encoding='utf-8', newline='')
    result = subprocess.run([BASH, (HOOKS / 'brain-save-trigger.sh').as_posix(), 'stop'],
                            input='{"session_id":"fixture"}', env=_env(tmp_path),
                            cwd=tmp_path, capture_output=True, text=True, encoding='utf-8', timeout=10)
    assert result.returncode == 0
    assert not result.stdout and not result.stderr
