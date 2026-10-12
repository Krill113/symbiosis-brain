"""Regression inputs for statusline JSON boundaries and safe publication."""
import json
import os
from pathlib import Path
import subprocess

import pytest
from tests._bash_resolver import _find_bash

HOOKS = Path(__file__).resolve().parents[1] / 'hooks'
BASH = _find_bash()
pytestmark = pytest.mark.skipif(BASH is None, reason='Bash is required')


def run_raw(tmp_path, raw, *, direct=False, custom=False):
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(('SB_', 'SYMBIOSIS_', 'CLAUDE_', 'ANTHROPIC_'))}
    env.update({'HOME': str(tmp_path), 'USERPROFILE': str(tmp_path),
                'APPDATA': str(tmp_path), 'LOCALAPPDATA': str(tmp_path),
                'TMPDIR': tmp_path.as_posix(), 'TEMP': str(tmp_path), 'TMP': str(tmp_path),
                'BASH_ENV': '', 'ENV': '', 'SB_NOW': '1000'})
    if custom:
        env['SYMBIOSIS_BRAIN_USER_STATUSLINE_CMD'] = 'printf "CUSTOM\\n"'
    name = 'sb-base-statusline.sh' if direct else 'sb-statusline.sh'
    result = subprocess.run([BASH, (HOOKS / name).as_posix()], input=raw,
                            env=env, cwd=tmp_path, capture_output=True,
                            text=True, encoding='utf-8', timeout=10)
    assert result.returncode == 0, result.stderr
    return result.stdout


@pytest.mark.parametrize('number,expected', [('2.8e1', '28'), ('2.8E+1', '28'),
                                            ('1e2', '100'), ('1e-1', '0'), ('28.9', '28')])
def test_json_numbers(tmp_path, number, expected):
    out = run_raw(tmp_path, '{"session_id":"fixture","context_window":{"used_percentage":' + number + '}}')
    assert expected + '%' in out
    assert (tmp_path / 'brain-context-pct-fixture').read_text(encoding='utf-8').strip() == expected


@pytest.mark.parametrize('raw', [
    '{"context_window":{"used_percentage":28,"used_percentage":null}}',
    '{"context_window":{"used_percentage":28},"context_window":null}',
    '{"context_window":{"used_percentage":28,"current_usage":{"input_tokens":10,"cache_read_input_tokens":{}}}}',
    '{"context_window":{"used_percentage":28,"current_usage":{"input_tokens":10,"cache_creation_input_tokens":true}}}',
])
def test_ambiguous_or_invalid_observation_is_unknown(tmp_path, raw):
    assert 'ctx:?' in run_raw(tmp_path, raw)


@pytest.mark.parametrize('sid', ['../escape', 'x/y', 'x\\y', '..', '', 'a' * 129])
def test_unsafe_session_ids_do_not_publish(tmp_path, sid):
    run_raw(tmp_path, json.dumps({'session_id': sid, 'context_window': {'used_percentage': 28}}))
    assert not list(tmp_path.glob('brain-context-*'))


def test_nested_session_decoy_does_not_select_bridge(tmp_path):
    run_raw(tmp_path, json.dumps({'decoy': {'session_id': 'wrong'}, 'session_id': 'right',
                                 'context_window': {'used_percentage': 28}}))
    assert (tmp_path / 'brain-context-pct-right').read_text(encoding='utf-8').strip() == '28'
    assert not (tmp_path / 'brain-context-pct-wrong').exists()


@pytest.mark.parametrize('direct,custom', [(True, False), (False, True)])
def test_export_independent_of_render_path(tmp_path, direct, custom):
    out = run_raw(tmp_path, json.dumps({'session_id': 'fixture',
                  'context_window': {'used_percentage': 28},
                  'model': {'id': 'fixture-model'}, 'effort': {'level': 'high'}}),
                  direct=direct, custom=custom)
    assert (tmp_path / 'brain-context-pct-fixture').read_text(encoding='utf-8').strip() == '28'
    assert (tmp_path / 'brain-context-state-fixture').read_text(encoding='utf-8').strip() == '28\t1000'
    assert ('CUSTOM' in out) if custom else ('fixture-model (high)' in out)


def test_escaped_keys_and_string_decoys(tmp_path):
    raw = '{"noise":"\\\"context_window\\\":{\\\"used_percentage\\\":99}","context_win\\u0064ow":{"used_percentage":28},"model":{"id":"fixture","display_name":"A \\\"quote\\\""},"effort":{"le\\u0076el":"hi\\u0067h"}}'
    out = run_raw(tmp_path, raw)
    assert '28%' in out
    assert 'A "quote" (high)' in out
    assert '99%' not in out


@pytest.mark.parametrize('raw', ['{"context_window":{"used_percentage":28},}',
                                  '{"context_window":{"used_percentage":28}} garbage',
                                  '{"context_window":{"used_percentage":28}',
                                  '{"context_window":{"used_percentage":028}}'])
def test_malformed_input_is_unknown(tmp_path, raw):
    assert 'ctx:?' in run_raw(tmp_path, raw)
