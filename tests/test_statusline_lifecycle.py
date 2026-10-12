"""Availability transitions must not erase independent save/recall behavior."""
import json
import subprocess

from tests.test_context_observation_consumers import _env, BASH, HOOKS
from tests.test_statusline_boundaries import run_raw


def test_unknown_does_not_overwrite_last_successful_save(tmp_path):
    (tmp_path / 'brain-last-save-pct-fixture').write_bytes(b'28\n')
    run_raw(tmp_path, json.dumps({'session_id': 'fixture', 'context_window': {
        'used_percentage': 0, 'current_usage': {'input_tokens': 0}}}))
    result = subprocess.run([BASH, (HOOKS / 'brain-save-marker.sh').as_posix()],
                            input='{"session_id":"fixture","tool_response":{"text":"Saved"}}',
                            env=_env(tmp_path), cwd=tmp_path, capture_output=True,
                            text=True, encoding='utf-8', timeout=10)
    assert result.returncode == 0
    assert (tmp_path / 'brain-last-save-pct-fixture').read_bytes() == b'28\n'


def test_two_sessions_keep_independent_observations(tmp_path):
    for sid, pct in [('first', 31), ('second', 12)]:
        run_raw(tmp_path, json.dumps({'session_id': sid, 'context_window': {'used_percentage': pct}}))
    run_raw(tmp_path, '{"session_id":"first","context_window":{"used_percentage":null}}')
    assert (tmp_path / 'brain-context-pct-first').read_text(encoding='utf-8').strip() == ''
    assert (tmp_path / 'brain-context-pct-second').read_text(encoding='utf-8').strip() == '12'


def test_unknown_does_not_disable_precompact(tmp_path):
    (tmp_path / 'brain-context-pct-fixture').write_bytes(b'')
    (tmp_path / 'brain-context-state-fixture').write_bytes(b'unknown\t1000\n')
    result = subprocess.run([BASH, (HOOKS / 'brain-save-trigger.sh').as_posix(), 'precompact'],
                            input='{"session_id":"fixture"}', env=_env(tmp_path), cwd=tmp_path,
                            capture_output=True, text=True, encoding='utf-8', timeout=10)
    assert result.returncode == 2
    assert (tmp_path / 'brain-precompact-pending-fixture').exists()


def test_unknown_preserves_prompt_rules_without_fabricating_zero(tmp_path):
    env = _env(tmp_path)
    env.update({'SYMBIOSIS_BRAIN_RECALL_ENABLED': 'false',
                'SYMBIOSIS_BRAIN_RULES_ENABLED': 'true'})
    result = subprocess.run([BASH, (HOOKS / 'brain-save-trigger.sh').as_posix(), 'prompt-check'],
                            input='{"session_id":"fixture","prompt":"fixture prompt long enough"}',
                            env=env, cwd=tmp_path, capture_output=True,
                            text=True, encoding='utf-8', timeout=10)
    assert result.returncode == 0, result.stderr
    assert '[rules' in result.stdout
    assert 'context 0%' not in result.stdout
    assert not (tmp_path / 'brain-context-pct-fixture').exists()


def test_session_start_clears_only_own_observation(tmp_path):
    env = _env(tmp_path)
    env.update({'SYMBIOSIS_BRAIN_TOOLS': '', 'SYMBIOSIS_BRAIN_VAULT': str(tmp_path / 'vault')})
    # Fresh roster prevents any external health-check path; no tools means no prewarm.
    (tmp_path / 'brain-mcp-roster-fixture').write_bytes(b'fixture\n')
    for sid in ('fixture', 'other'):
        (tmp_path / ('brain-context-pct-' + sid)).write_bytes(b'45\n')
        (tmp_path / ('brain-context-state-' + sid)).write_bytes(b'45\t1000\n')
    result = subprocess.run([BASH, (HOOKS / 'brain-session-start.sh').as_posix()],
                            input='{"session_id":"fixture","source":"compact"}',
                            env=env, cwd=tmp_path, capture_output=True,
                            text=True, encoding='utf-8', timeout=10)
    assert result.returncode == 0, result.stderr
    assert not (tmp_path / 'brain-context-pct-fixture').exists()
    assert not (tmp_path / 'brain-context-state-fixture').exists()
    assert (tmp_path / 'brain-context-pct-other').read_bytes() == b'45\n'
    assert (tmp_path / 'brain-context-state-other').read_bytes() == b'45\t1000\n'
