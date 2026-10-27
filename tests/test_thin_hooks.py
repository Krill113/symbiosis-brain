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
