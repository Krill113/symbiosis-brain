import os, subprocess, json, tempfile, shutil, pathlib

HOOK = pathlib.Path(__file__).resolve().parents[1] / 'hooks' / 'brain-save-trigger.sh'
START = pathlib.Path(__file__).resolve().parents[1] / 'hooks' / 'brain-session-start.sh'

def _run(prompt, env_extra=None, sb_tmp=None, session='s1'):
    env = dict(os.environ)
    env['TMPDIR'] = sb_tmp; env['TEMP'] = sb_tmp
    env.setdefault('SYMBIOSIS_BRAIN_RULES_ENABLED', 'false')
    env.pop('SYMBIOSIS_BRAIN_VAULT', None)
    if env_extra: env.update(env_extra)
    payload = json.dumps({'session_id': session, 'prompt': prompt})
    subprocess.run(['bash', str(HOOK), 'prompt-check'], input=payload, text=True, encoding='utf-8', env=env, capture_output=True)

def test_counter_increments_across_gates():
    sb = tempfile.mkdtemp()
    try:
        ctr = pathlib.Path(sb) / 'brain-route-turn-s1'
        _run('a long enough normal prompt here', sb_tmp=sb)
        assert ctr.read_text().strip() == '1'
        _run('hi', sb_tmp=sb)
        assert ctr.read_text().strip() == '2'
        _run('/compact', sb_tmp=sb)
        assert ctr.read_text().strip() == '3'
    finally:
        shutil.rmtree(sb, ignore_errors=True)

def test_route_turn_excluded_from_sessionstart_rm():
    sb = tempfile.mkdtemp()
    try:
        env = dict(os.environ); env['TMPDIR'] = sb; env['TEMP'] = sb
        env['SYMBIOSIS_BRAIN_RULES_ENABLED'] = 'false'; env.pop('SYMBIOSIS_BRAIN_VAULT', None)
        p = json.dumps({'session_id': 'sX', 'prompt': 'a normal length prompt'})
        subprocess.run(['bash', str(HOOK), 'prompt-check'], input=p, text=True, encoding='utf-8', env=env, capture_output=True)
        ctr = pathlib.Path(sb) / 'brain-route-turn-sX'
        assert ctr.read_text().strip() == '1'
        subprocess.run(['bash', str(START)], input=json.dumps({'session_id': 'sX'}), text=True, encoding='utf-8', env=env, capture_output=True)
        assert ctr.exists(), 'route-turn counter must survive SessionStart/compact'
        subprocess.run(['bash', str(HOOK), 'prompt-check'], input=p, text=True, encoding='utf-8', env=env, capture_output=True)
        assert ctr.read_text().strip() == '2'
    finally:
        shutil.rmtree(sb, ignore_errors=True)

ORIGINAL = ('Перед grep по коду — проверь `.claude/docs/catalog/` (если есть) и brain_search.\n'
            'Доступно: brain_search/brain_read/brain_lint (память+гигиена), Serena (find_symbol/find_referencing_symbols), субагенты (Explore/general-purpose).\n'
            'Большие чтения / multi-file поиск — делегируй субагентам, не лезь сам в main.')

def test_additive_byte_identical():
    sb = tempfile.mkdtemp()
    try:
        env = dict(os.environ); env['TMPDIR'] = sb; env['TEMP'] = sb
        env['SYMBIOSIS_BRAIN_ROUTING_MODE'] = 'additive'
        env['SYMBIOSIS_BRAIN_RULES_ENABLED'] = 'true'
        env['SYMBIOSIS_BRAIN_RULES_TURN_INTERVAL'] = '1'
        env.pop('SYMBIOSIS_BRAIN_VAULT', None)
        env.pop('SYMBIOSIS_BRAIN_RULES_TEXT', None)
        r = subprocess.run(['bash', str(HOOK), 'prompt-check'],
            input=json.dumps({'session_id': 'add', 'prompt': 'a normal length prompt here'}),
            text=True, encoding='utf-8', env=env, capture_output=True)
        assert ORIGINAL in r.stdout, r.stdout
    finally:
        shutil.rmtree(sb, ignore_errors=True)

def test_silent_turn_no_system_reminder():
    sb = tempfile.mkdtemp()
    try:
        env = dict(os.environ); env['TMPDIR'] = sb; env['TEMP'] = sb
        env['SYMBIOSIS_BRAIN_RULES_ENABLED'] = 'false'
        env.pop('SYMBIOSIS_BRAIN_VAULT', None)
        r = subprocess.run(['bash', str(HOOK), 'prompt-check'],
            input=json.dumps({'session_id': 'q', 'prompt': 'just a normal prompt'}),
            text=True, encoding='utf-8', env=env, capture_output=True)
        assert '<system-reminder>' not in r.stdout
        assert (pathlib.Path(sb) / 'brain-route-turn-q').read_text().strip() == '1'
    finally:
        shutil.rmtree(sb, ignore_errors=True)

def _stub_search_gist(sb, envelope):
    bindir = pathlib.Path(sb) / 'bin'; bindir.mkdir(exist_ok=True)
    uv = bindir / 'uv'
    uv.write_text('#!/bin/bash\ncat <<\'EOF\'\n' + json.dumps(envelope) + '\nEOF\n')
    uv.chmod(0o755)
    return str(bindir)

def _run_route(prompt, sb, route_class):
    env = dict(os.environ)
    env['TMPDIR'] = sb; env['TEMP'] = sb
    env['SYMBIOSIS_BRAIN_VAULT'] = sb
    env['SYMBIOSIS_BRAIN_TOOLS'] = sb
    env['SYMBIOSIS_BRAIN_ROUTING_MODE'] = 'decompose'
    env['SYMBIOSIS_BRAIN_RULES_TURN_INTERVAL'] = '1'
    envelope = {'memory_hits': [], 'route_hints': [{'id': 'serena-symbol-work', 'class': route_class, 'hint': 'Serena до правки.'}]}
    env['PATH'] = _stub_search_gist(sb, envelope) + os.pathsep + env['PATH']
    r = subprocess.run(['bash', str(HOOK), 'prompt-check'],
        input=json.dumps({'session_id': 'sup', 'prompt': prompt}), text=True, encoding='utf-8', env=env, capture_output=True)
    return r.stdout

def test_supersede_suppresses_tools_but_keeps_discipline():
    sb = tempfile.mkdtemp()
    try:
        out = _run_route('переименуй FooBar везде', sb, 'supersede')
        assert 'делегируй субагентам' in out
        assert 'проверь' not in out or 'catalog' not in out
        assert '[route]' in out
    finally:
        shutil.rmtree(sb, ignore_errors=True)

def test_augment_match_does_not_suppress_tools():
    sb = tempfile.mkdtemp()
    try:
        out = _run_route('поищи в сети про uv lockfile', sb, 'augment')
        assert '.claude/docs/catalog/' in out
        assert '[route]' in out
    finally:
        shutil.rmtree(sb, ignore_errors=True)

def test_routing_gate_not_recall_gate():
    """AC#9: the routing-gate is DISTINCT from the recall-gate. A terse intent
    (<15 chars, no slash, not a bare affirmation) STILL routes, while a bare
    affirmation and a slash command do NOT. Guards the Pass-2 blocker #5 fix
    (routing must not inherit the recall 15-char floor)."""
    sb = tempfile.mkdtemp()
    try:
        # terse intent under the recall length floor → still routes
        assert '[route]' in _run_route('latest ruff?', sb, 'augment')
        # bare affirmation → no routing
        assert '[route]' not in _run_route('ok', sb, 'augment')
        # slash command → no routing
        assert '[route]' not in _run_route('/compact', sb, 'augment')
    finally:
        shutil.rmtree(sb, ignore_errors=True)


def test_hook_syntax_valid():
    import subprocess, pathlib
    root = pathlib.Path(__file__).resolve().parents[1]
    r = subprocess.run(['bash', '-n', str(root / 'hooks' / 'brain-save-trigger.sh')], capture_output=True, text=True)
    r2 = subprocess.run(['bash', '-n', str(root / 'hooks' / 'brain-session-start.sh')], capture_output=True, text=True)
    assert r.returncode == 0 and r2.returncode == 0, (r.stderr, r2.stderr)
