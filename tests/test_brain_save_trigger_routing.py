import os, subprocess, json, tempfile, shutil, pathlib
import pytest  # NEW — file previously imported only os/subprocess/json/tempfile/shutil/pathlib

HOOK = pathlib.Path(__file__).resolve().parents[1] / 'hooks' / 'brain-save-trigger.sh'
START = pathlib.Path(__file__).resolve().parents[1] / 'hooks' / 'brain-session-start.sh'


# Resolver lives in _bash_resolver.py so test_action_rules.py shares the same
# health-checked absolute-path lookup (bare "bash" = WSL stub on Windows CI).
# Two import forms: pytest prepends tests/ to sys.path (bare name works); the
# CI sanity step imports this module as `tests.test_brain_save_trigger_routing`
# from the repo root, where only the package form resolves.
try:
    from tests._bash_resolver import _bash_works, _find_bash, _BASH, _bash  # noqa: E402,F401
except ImportError:  # pragma: no cover - pytest prepend mode
    from _bash_resolver import _bash_works, _find_bash, _BASH, _bash  # noqa: E402,F401


def _run(prompt, env_extra=None, sb_tmp=None, session='s1'):
    env = dict(os.environ)
    env['TMPDIR'] = sb_tmp; env['TEMP'] = sb_tmp
    env.setdefault('SYMBIOSIS_BRAIN_RULES_ENABLED', 'false')
    env.pop('SYMBIOSIS_BRAIN_VAULT', None)
    if env_extra: env.update(env_extra)
    payload = json.dumps({'session_id': session, 'prompt': prompt})
    subprocess.run([_bash(), str(HOOK), 'prompt-check'], input=payload, text=True, encoding='utf-8', env=env, capture_output=True)

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
        subprocess.run([_bash(), str(HOOK), 'prompt-check'], input=p, text=True, encoding='utf-8', env=env, capture_output=True)
        ctr = pathlib.Path(sb) / 'brain-route-turn-sX'
        assert ctr.read_text().strip() == '1'
        subprocess.run([_bash(), str(START)], input=json.dumps({'session_id': 'sX'}), text=True, encoding='utf-8', env=env, capture_output=True)
        assert ctr.exists(), 'route-turn counter must survive SessionStart/compact'
        subprocess.run([_bash(), str(HOOK), 'prompt-check'], input=p, text=True, encoding='utf-8', env=env, capture_output=True)
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
        r = subprocess.run([_bash(), str(HOOK), 'prompt-check'],
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
        r = subprocess.run([_bash(), str(HOOK), 'prompt-check'],
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
    r = subprocess.run([_bash(), str(HOOK), 'prompt-check'],
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
    r = subprocess.run([_bash(), '-n', str(root / 'hooks' / 'brain-save-trigger.sh')], capture_output=True, text=True)
    r2 = subprocess.run([_bash(), '-n', str(root / 'hooks' / 'brain-session-start.sh')], capture_output=True, text=True)
    assert r.returncode == 0 and r2.returncode == 0, (r.stderr, r2.stderr)


# --- _find_bash / _bash resolver unit tests ---

def test_find_bash_posix_uses_which():
    result = _find_bash(windows=False, which=lambda name: '/usr/bin/bash')
    assert result == '/usr/bin/bash'


def test_find_bash_derives_from_git_location(tmp_path):
    root = tmp_path / 'GitRoot'
    (root / 'cmd').mkdir(parents=True)
    git_exe = root / 'cmd' / 'git.exe'
    git_exe.write_text('')
    (root / 'bin').mkdir(parents=True)
    bash_exe = root / 'bin' / 'bash.exe'
    bash_exe.write_text('')
    result = _find_bash(
        which=lambda name: str(git_exe) if name == 'git' else None,
        exists=os.path.exists, environ={}, windows=True, works=lambda p: True,
    )
    assert result == str(bash_exe)


def test_find_bash_rejects_system32_stub(tmp_path):
    windir = tmp_path / 'Windows'
    stub = windir / 'System32' / 'bash.exe'
    result = _find_bash(
        which=lambda name: str(stub) if name == 'bash.exe' else None,
        exists=os.path.exists,
        environ={'SystemRoot': str(windir)},
        windows=True, works=lambda p: True,
    )
    assert result is None


def test_find_bash_falls_back_to_program_files(tmp_path):
    pf = tmp_path / 'ProgramFiles'
    (pf / 'Git' / 'bin').mkdir(parents=True)
    bash_exe = pf / 'Git' / 'bin' / 'bash.exe'
    bash_exe.write_text('')
    result = _find_bash(
        which=lambda name: None,
        exists=os.path.exists,
        environ={'ProgramFiles': str(pf)},
        windows=True, works=lambda p: True,
    )
    assert result == str(bash_exe)


def test_find_bash_prefers_bin_over_usr_bin(tmp_path):
    # same root: bin wins over usr\bin
    root = tmp_path / 'SameRoot'
    (root / 'cmd').mkdir(parents=True)
    git_exe = root / 'cmd' / 'git.exe'
    git_exe.write_text('')
    (root / 'bin').mkdir(parents=True)
    bin_bash = root / 'bin' / 'bash.exe'
    bin_bash.write_text('')
    (root / 'usr' / 'bin').mkdir(parents=True)
    usr_bin_bash = root / 'usr' / 'bin' / 'bash.exe'
    usr_bin_bash.write_text('')
    result = _find_bash(
        which=lambda name: str(git_exe) if name == 'git' else None,
        exists=os.path.exists, environ={}, windows=True, works=lambda p: True,
    )
    assert result == str(bin_bash)

    # two roots: root A has BOTH bin and usr\bin (both unhealthy), root B only
    # has bin (healthy) -> the health check must actually reject A's bin
    # (the earliest candidate, which exists) before falling through all the
    # way to B's bin; a health check that never runs would wrongly return
    # A's bin here.
    root_a = tmp_path / 'RootA'
    (root_a / 'cmd').mkdir(parents=True)
    git_a = root_a / 'cmd' / 'git.exe'
    git_a.write_text('')
    (root_a / 'bin').mkdir(parents=True)
    a_bin = root_a / 'bin' / 'bash.exe'
    a_bin.write_text('')
    (root_a / 'usr' / 'bin').mkdir(parents=True)
    a_usr_bin = root_a / 'usr' / 'bin' / 'bash.exe'
    a_usr_bin.write_text('')
    root_b = tmp_path / 'RootB'
    (root_b / 'Git' / 'bin').mkdir(parents=True)
    b_bin = root_b / 'Git' / 'bin' / 'bash.exe'
    b_bin.write_text('')

    def works(p):
        return p not in (str(a_bin), str(a_usr_bin))

    result2 = _find_bash(
        which=lambda name: str(git_a) if name == 'git' else None,
        exists=os.path.exists,
        environ={'ProgramFiles': str(root_b)},
        windows=True, works=works,
    )
    assert result2 == str(b_bin)


def test_find_bash_allows_windows_sibling_dirs(tmp_path):
    windir = tmp_path / 'Windows'
    sibling = tmp_path / 'WindowsApps' / 'bash.exe'
    result = _find_bash(
        which=lambda name: str(sibling) if name == 'bash.exe' else None,
        exists=os.path.exists,
        environ={'SystemRoot': str(windir)},
        windows=True, works=lambda p: True,
    )
    assert result == str(sibling)

    # an unhealthy which()-fallback candidate must not be returned (guards
    # dropping the works(found) check from the which() fallback branch)
    result2 = _find_bash(
        which=lambda name: str(sibling) if name == 'bash.exe' else None,
        exists=os.path.exists,
        environ={'SystemRoot': str(windir)},
        windows=True, works=lambda p: False,
    )
    assert result2 is None


def _resolver_module():
    # _bash() reads _BASH from the module it was defined in. That module may be
    # loaded as `tests._bash_resolver` or as bare `_bash_resolver` depending on
    # how this file was imported — patch the one the function actually uses.
    import sys
    return sys.modules[_bash.__module__]


def test_bash_skips_when_unresolved(monkeypatch):
    monkeypatch.delenv('CI', raising=False)
    monkeypatch.delenv('SB_REQUIRE_BASH', raising=False)
    monkeypatch.setattr(_resolver_module(), '_BASH', None)
    with pytest.raises(pytest.skip.Exception):
        _bash()


def test_bash_fails_on_ci_when_unresolved(monkeypatch):
    monkeypatch.setenv('CI', '1')
    monkeypatch.setattr(_resolver_module(), '_BASH', None)
    with pytest.raises(pytest.fail.Exception):
        _bash()
