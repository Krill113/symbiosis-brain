"""Synthetic contracts for the one-process prompt hook path."""
import io
import sys
from pathlib import Path
import os
import subprocess
import json

from tests._bash_resolver import _bash

import pytest

from symbiosis_brain import __main__ as cli


@pytest.mark.parametrize("prompt,recall,route", [
    ('A sufficiently long task with "quotes"\nand a newline', True, True),
    ('коротко', False, True),
    ('да', False, False),
    ('/brain-save details', False, False),
    ('', False, True),
    (['invalid prompt shape'], False, False),
])
def test_hook_prompt_gates_match_shell(prompt, recall, route, monkeypatch):
    monkeypatch.delenv('SYMBIOSIS_BRAIN_RECALL_ENABLED', raising=False)
    monkeypatch.delenv('SYMBIOSIS_BRAIN_RECALL_SKIP_SHORT_CHARS', raising=False)
    assert cli._hook_prompt_gates(prompt) == (recall, route)


@pytest.mark.parametrize('prompt', [
    '<task-notification>\n<task-id>synthetic</task-id>\n<summary>Completed</summary>\n</task-notification>',
    '<task-notification>synthetic inline completion</task-notification>',
])
def test_task_notification_skips_memory_and_routes_before_config(prompt, tmp_path, monkeypatch):
    assert cli._hook_prompt_gates(prompt) == (False, False)
    monkeypatch.setattr(sys, 'stdin', io.StringIO(json.dumps({'prompt': prompt})))
    out = io.StringIO()
    monkeypatch.setattr(sys, 'stdout', out)
    monkeypatch.setattr(cli, '_load_pre_action_config', lambda: pytest.fail('notification must not load recall/routing config'))
    assert cli._run_search_gist(['--vault', str(tmp_path), '--prompt-from-stdin', '--hook-blocks']) == 0
    assert out.getvalue() == 'S0\n'


@pytest.mark.parametrize('prompt', [
    '<system-reminder>A substantive synthetic user request about memory</system-reminder>',
    'Explain the literal <task-notification> marker in this synthetic request',
    '<task-notification-extra>A substantive user request</task-notification-extra>',
])
def test_notification_gate_does_not_hide_other_user_prompts(prompt, monkeypatch):
    monkeypatch.setenv('SYMBIOSIS_BRAIN_RECALL_ENABLED', 'true')
    monkeypatch.setenv('SYMBIOSIS_BRAIN_RECALL_SKIP_SHORT_CHARS', '15')
    assert cli._hook_prompt_gates(prompt) == (True, True)


def test_task_notification_keeps_rules_context_compaction_relay_and_turn_counter(tmp_path):
    root = Path(__file__).resolve().parents[1]
    profile = tmp_path / 'home'
    (profile / '.claude').mkdir(parents=True)
    sid = 'synthetic-notification'
    import time
    (tmp_path / f'brain-context-pct-{sid}').write_bytes(b'35\n')
    (tmp_path / f'brain-context-state-{sid}').write_bytes(f'35\t{int(time.time())}\n'.encode('utf-8'))
    (tmp_path / f'brain-precompact-pending-{sid}').write_text('', encoding='utf-8')
    env = {**os.environ, 'HOME': str(profile), 'USERPROFILE': str(profile),
           'TMPDIR': str(tmp_path), 'TEMP': str(tmp_path), 'SYMBIOSIS_BRAIN_TOOLS': str(root),
           'SYMBIOSIS_BRAIN_PYTHON': sys.executable, 'SYMBIOSIS_BRAIN_VAULT': str(tmp_path / 'vault'),
           'SYMBIOSIS_BRAIN_RULES_ENABLED': 'true', 'SYMBIOSIS_BRAIN_ROUTING_MODE': 'decompose'}
    result = subprocess.run([_bash(), str(root / 'hooks/brain-save-trigger.sh'), 'prompt-check'],
        input=json.dumps({'session_id': sid, 'prompt': '<task-notification>synthetic done</task-notification>'}),
        env=env, capture_output=True, text=True, encoding='utf-8', timeout=30)
    assert result.returncode == 0, result.stderr
    assert '[rules' in result.stdout and 'context 35%' in result.stdout
    assert 'Compaction was blocked' in result.stdout
    assert '[memory:' not in result.stdout and '[route]' not in result.stdout
    assert (tmp_path / f'brain-route-turn-{sid}').read_text(encoding='utf-8').strip() == '1'
    assert not (tmp_path / f'brain-precompact-pending-{sid}').exists()


def test_hook_blocks_are_data_not_shell_and_preserve_multiline(monkeypatch):
    out = io.StringIO()
    monkeypatch.setattr(sys, 'stdout', out)
    cli._emit_hook_blocks(
        [{'path': 'project-a/wiki/x.md', 'gist': 'Кириллица\n$(not a command)'}],
        [{'hint': ' first\nsecond ', 'class': 'supersede'}],
    )
    assert out.getvalue() == (
        'S1\nM- project-a/wiki/x.md — Кириллица\nM$(not a command)\n'
        'Rfirst\nRsecond\n'
    )


def test_hook_blocks_cli_applies_gate_before_search(tmp_path, monkeypatch):
    import json
    monkeypatch.setattr(sys, 'stdin', io.StringIO(json.dumps({'prompt': '/brain-save'})))
    out = io.StringIO()
    monkeypatch.setattr(sys, 'stdout', out)
    monkeypatch.setattr(cli, '_load_pre_action_config', lambda: None)
    def unexpected(*args, **kwargs):
        pytest.fail('slash gate must not run memory search')
    monkeypatch.setattr(cli, '_gist_search', unexpected)
    assert cli._run_search_gist(['--vault', str(tmp_path), '--prompt-from-stdin', '--hook-blocks']) == 0
    assert out.getvalue() == 'S0\n'


def test_hook_blocks_cli_disabled_recall_keeps_route_gate(tmp_path, monkeypatch):
    import json
    monkeypatch.setenv('SYMBIOSIS_BRAIN_RECALL_ENABLED', 'false')
    monkeypatch.setattr(sys, 'stdin', io.StringIO(json.dumps({'prompt': 'long substantive synthetic task'})))
    out = io.StringIO()
    monkeypatch.setattr(sys, 'stdout', out)
    monkeypatch.setattr(cli, '_load_pre_action_config', lambda: None)
    monkeypatch.setattr(cli, '_gist_search', lambda *a, **k: pytest.fail('recall disabled'))
    assert cli._run_search_gist(['--vault', str(tmp_path), '--prompt-from-stdin', '--hook-blocks']) == 0
    assert out.getvalue() == 'S0\n'


@pytest.mark.parametrize('choice', ['env', 'windows', 'posix', 'uv'])
def test_interpreter_selection_order(tmp_path, choice):
    root = Path(__file__).resolve().parents[1]
    tools = tmp_path / 'tools'
    explicit = tmp_path / 'explicit python'
    windows = tools / '.venv' / 'Scripts' / 'python.exe'
    posix = tools / '.venv' / 'bin' / 'python'
    for p in (explicit, windows, posix):
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text('', encoding='utf-8')
    if choice != 'env':
        explicit.unlink()
    if choice in {'posix', 'uv'}:
        windows.unlink()
    if choice == 'uv':
        posix.unlink()
    env = {**os.environ, 'SYMBIOSIS_BRAIN_PYTHON': str(explicit),
           'SYMBIOSIS_BRAIN_TOOLS': str(tools)}
    script = '. "$1"; sb_python_command; printf "%s\\n" "${SB_PY_CMD[@]}"'
    result = subprocess.run([_bash(), '-c', script, 'probe', str(root / 'hooks' / 'sb-hooklib.sh')],
                            env=env, capture_output=True, text=True, encoding='utf-8')
    assert result.returncode == 0
    selected = result.stdout.splitlines()
    if choice == 'uv':
        assert selected == ['uv', 'run', '--quiet', '--directory', str(tools), 'python']
    else:
        assert len(selected) == 1
        assert Path(selected[0]) == {'env': explicit, 'windows': windows, 'posix': posix}[choice]


@pytest.mark.parametrize('event', ['prompt', 'action'])
def test_selected_interpreter_failure_does_not_retry_uv(tmp_path, event):
    root = Path(__file__).resolve().parents[1]
    bindir = tmp_path / 'bin'
    bindir.mkdir()
    marker = tmp_path / 'uv-called'
    uv = bindir / 'uv'
    uv.write_text('#!/bin/sh\nprintf retry > "$THIN_RETRY_MARKER"\nexit 0\n', encoding='utf-8')
    uv.chmod(0o755)
    direct = tmp_path / 'broken-python'
    direct.write_text('#!/bin/sh\nexit 7\n', encoding='utf-8')
    direct.chmod(0o755)
    env = {**os.environ, 'HOME': str(tmp_path), 'USERPROFILE': str(tmp_path),
           'TMPDIR': str(tmp_path), 'TEMP': str(tmp_path),
           'PATH': str(bindir) + os.pathsep + os.environ.get('PATH', ''),
           'SYMBIOSIS_BRAIN_PYTHON': str(direct), 'SYMBIOSIS_BRAIN_TOOLS': str(root),
           'SYMBIOSIS_BRAIN_VAULT': str(tmp_path), 'SYMBIOSIS_BRAIN_RULES_ENABLED': 'false',
           'THIN_RETRY_MARKER': str(marker)}
    payload = {'session_id': 'failure-probe', 'prompt': 'A long synthetic task',
               'tool_name': 'Edit', 'tool_input': {'file_path': 'synthetic.py', 'new_string': 'check'}}
    args = [str(root / 'hooks' / 'brain-save-trigger.sh'), 'prompt-check'] if event == 'prompt' else [str(root / 'hooks' / 'brain-pre-action-trigger.sh')]
    result = subprocess.run([_bash(), *args], input=json.dumps(payload), env=env,
                            capture_output=True, text=True, encoding='utf-8', timeout=15)
    assert result.returncode == 0
    assert result.stdout == ''
    assert not marker.exists()


@pytest.mark.parametrize('tool', ['brain_write', 'brain_append', 'brain_patch', 'brain_rotate_handoffs'])
def test_signature_branch_precedes_all_recall_imports(tmp_path, monkeypatch, tool):
    import builtins
    transcript = tmp_path / 'session.jsonl'
    transcript.write_text(json.dumps({'type': 'assistant', 'message': {'model': 'executor-model', 'content': []}}) + '\n', encoding='utf-8')
    payload = {'tool_name': 'mcp__symbiosis-brain__' + tool,
               'tool_input': {'model': 'self-report', 'role': 'colleague', 'body': 'business data'},
               'transcript_path': str(transcript)}
    real_import = builtins.__import__
    forbidden = {'symbiosis_brain.pre_action_config', 'symbiosis_brain.pre_action_recall',
                 'symbiosis_brain.storage', 'symbiosis_brain.search', 'symbiosis_brain.sync'}
    def checked_import(name, *args, **kwargs):
        if name in forbidden:
            pytest.fail('signature path imported recall/config/database: ' + name)
        return real_import(name, *args, **kwargs)
    monkeypatch.setattr(builtins, '__import__', checked_import)
    monkeypatch.setattr(sys, 'stdin', io.StringIO(json.dumps(payload)))
    out = io.StringIO()
    monkeypatch.setattr(sys, 'stdout', out)
    monkeypatch.delenv('SYMBIOSIS_BRAIN_PRE_ACTION_DISABLED', raising=False)
    assert cli._run_pre_action_recall(['--vault', str(tmp_path / 'nonexistent-vault')]) == 0
    assert json.loads(out.getvalue()) == {'hookSpecificOutput': {
        'hookEventName': 'PreToolUse',
        'updatedInput': {'model': 'executor-model', 'role': 'colleague', 'body': 'business data'},
    }}


def test_signature_branch_obeys_whole_hook_kill_switch(tmp_path, monkeypatch):
    transcript = tmp_path / 'session.jsonl'
    transcript.write_text(json.dumps({'type': 'assistant', 'message': {'model': 'executor', 'content': []}}) + '\n', encoding='utf-8')
    monkeypatch.setenv('SYMBIOSIS_BRAIN_PRE_ACTION_DISABLED', '1')
    monkeypatch.setattr(sys, 'stdin', io.StringIO(json.dumps({
        'tool_name': 'mcp__symbiosis-brain__brain_write', 'tool_input': {}, 'transcript_path': str(transcript),
    })))
    out = io.StringIO()
    monkeypatch.setattr(sys, 'stdout', out)
    assert cli._run_pre_action_recall(['--vault', str(tmp_path)]) == 0
    assert out.getvalue() == ''


def test_signature_thin_hook_runs_one_python_with_disabled_recall(tmp_path):
    root = Path(__file__).resolve().parents[1]
    transcript = tmp_path / 'session.jsonl'
    transcript.write_text(json.dumps({'type': 'assistant', 'message': {'model': 'executor', 'content': []}}) + '\n', encoding='utf-8')
    profile = tmp_path / 'home'
    (profile / '.claude').mkdir(parents=True)
    (profile / '.claude/symbiosis-brain-pre-action.json').write_text(
        json.dumps({'enabled': False, 'matchers': []}), encoding='utf-8')
    calls = tmp_path / 'python-calls'
    shim = tmp_path / 'python-shim'
    shim.write_text('#!/bin/bash\nprintf "call\\n" >> "$SIGNATURE_CALLS"\nexec "$SIGNATURE_PYTHON" "$@"\n', encoding='utf-8')
    shim.chmod(0o755)
    env = {**os.environ, 'HOME': str(profile), 'USERPROFILE': str(profile),
           'TMPDIR': str(tmp_path), 'TEMP': str(tmp_path),
           'SYMBIOSIS_BRAIN_TOOLS': str(root), 'SYMBIOSIS_BRAIN_VAULT': str(tmp_path / 'missing-vault'),
           'SYMBIOSIS_BRAIN_PYTHON': str(shim), 'SIGNATURE_CALLS': str(calls), 'SIGNATURE_PYTHON': sys.executable}
    env.pop('SYMBIOSIS_BRAIN_PRE_ACTION_DISABLED', None)
    result = subprocess.run([_bash(), str(root / 'hooks/brain-pre-action-trigger.sh')],
        input=json.dumps({'session_id': 'synthetic-session', 'tool_name': 'mcp__symbiosis-brain__brain_append',
                          'tool_input': {'content': 'business data', 'role': 'lead'}, 'transcript_path': str(transcript)}),
        env=env, capture_output=True, text=True, encoding='utf-8', timeout=30)
    assert result.returncode == 0, result.stderr
    assert calls.read_text(encoding='utf-8').splitlines() == ['call']
    assert json.loads(result.stdout) == {'hookSpecificOutput': {'hookEventName': 'PreToolUse',
        'updatedInput': {'content': 'business data', 'role': 'lead', 'model': 'executor'}}}
