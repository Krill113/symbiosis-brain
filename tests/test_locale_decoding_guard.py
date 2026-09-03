"""Every text I/O in this repository declares its encoding explicitly.

Why a source check and not a behavioural one: `text=True` (and `open`, and
`Path.read_text`) decode with the locale's preferred encoding, so the failure
only exists on a machine whose locale is not UTF-8. A behavioural test passes
with or without the fix wherever the locale happens to be UTF-8, and therefore
proves nothing there.

Why that argument is stronger than it looks — and why this check is not a
backstop but the ONLY detector this class has:

* CI already runs a non-UTF-8 job. The matrix is `[ubuntu-latest,
  windows-latest]` with no locale pinning, and the Windows runner's ANSI
  codepage is not UTF-8. That job is green.
* It is green because **every fixture in this suite is ASCII**, and on ASCII a
  locale decoder and a UTF-8 decoder produce byte-identical results. That reason
  is independent of platform and locale: adding a non-UTF-8 CI job would not
  help while the payloads stay ASCII, and making payloads non-ASCII is a
  different change (rewriting tests on the merits).

So the suite cannot catch this class by running, on any platform we use. It is
caught here, statically, or not at all.

Why the whole repository and not just the package: the previous version of this
guard scanned `Path(symbiosis_brain.__file__).parent` only. Every live violation
in this repo — 81 in `tests/`, one in `tools/` — sat outside that directory and
was invisible to it, not because the matcher was narrow but because the **area**
was. A guard that looks in the one place already known to be clean reports zero
forever.

The failure mode is why this is worth a test at all: for `subprocess`, the decode
error is raised inside the reader thread, so the caller sees no exception — only
`None` where the output should be, or, more often, silently mojibake'd text that
a negative assertion (`assert x not in out`) then passes on. See
[[global/patterns/subprocess-text-true-decodes-by-locale]].

**The invariant is a pair, not a keyword.** The parent's decoder must match the
child's encoder. `encoding="utf-8"` on the parent is correct here only because
our own CLI entry points force UTF-8 on stdout — which the second test below
pins. A child that is not our CLI (a bare `python -c`) must be given `-X utf8`,
or the pair agrees only while the output happens to be ASCII.
"""
import ast
from pathlib import Path

import pytest
from conftest import iter_source_files

REPO_ROOT = Path(__file__).resolve().parents[1]

SUBPROCESS_FUNCS = {"run", "Popen", "check_output", "check_call", "call",
                    "getoutput", "getstatusoutput"}

# Entry points that promise UTF-8 stdout to every subprocess.run(..., encoding=
# "utf-8") in the suite. If one of them loses its reconfigure, the promise is
# gone and the keyword above becomes wrong — silently.
UTF8_STDOUT_ENTRY_POINTS = (
    "src/symbiosis_brain/__main__.py",
    "src/symbiosis_brain/scope_cli.py",
    "src/symbiosis_brain/install_cli.py",
)


def _python_files():
    # Area comes from conftest, shared with every other static guard: two guards
    # disagreeing about where our code lives is how the previous version of this
    # one reported clean for as long as it existed.
    return iter_source_files(include_tests=True)


def _subprocess_bindings(tree):
    """Names this module can call subprocess through, in any import form."""
    modules, functions = set(), set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "subprocess":
                    modules.add(alias.asname or alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module == "subprocess":
            for alias in node.names:
                if alias.name in SUBPROCESS_FUNCS:
                    functions.add(alias.asname or alias.name)
    return modules, functions


def _literal_mode(call):
    """The mode string of an open()/fdopen() call, '' if defaulted, None if dynamic."""
    mode = None
    for kw in call.keywords:
        if kw.arg == "mode":
            mode = kw.value
    if mode is None and len(call.args) >= 2:
        mode = call.args[1]
    if mode is None:
        return ""
    if isinstance(mode, ast.Constant) and isinstance(mode.value, str):
        return mode.value
    return None


def _offenders(tree):
    """[(lineno, kind)] for every call that would decode by locale."""
    modules, functions = _subprocess_bindings(tree)
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        named = {kw.arg for kw in node.keywords if kw.arg}
        has_encoding = "encoding" in named

        is_subprocess = (
            (isinstance(func, ast.Attribute) and func.attr in SUBPROCESS_FUNCS
             and isinstance(func.value, ast.Name) and func.value.id in modules)
            or (isinstance(func, ast.Name) and func.id in functions)
        )
        if is_subprocess and ("text" in named or "universal_newlines" in named) \
                and not has_encoding:
            found.append((node.lineno, "subprocess decodes text"))

        if isinstance(func, ast.Name) and func.id == "open" and not has_encoding:
            mode = _literal_mode(node)
            if mode is not None and "b" not in mode:
                found.append((node.lineno, "open() in text mode"))

        if isinstance(func, ast.Attribute) and func.attr in ("read_text", "write_text") \
                and not has_encoding:
            found.append((node.lineno, f".{func.attr}()"))

        if (isinstance(func, ast.Attribute) and func.attr == "fdopen"
                and isinstance(func.value, ast.Name) and func.value.id == "os"
                and not has_encoding):
            mode = _literal_mode(node)
            if mode is not None and "b" not in mode:
                found.append((node.lineno, "os.fdopen() in text mode"))
    return found


def test_no_text_io_decodes_by_locale():
    """Covers subprocess, open, read_text, write_text and os.fdopen, in
    src/, scripts/, tools/ and tests/ alike."""
    offences = {}
    for path in _python_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        hits = _offenders(tree)
        if hits:
            offences[str(path.relative_to(REPO_ROOT).as_posix())] = hits

    assert not offences, (
        "text I/O without an explicit encoding decodes by locale and fails "
        "silently on a non-UTF-8 system (empty output, or mojibake a negative "
        "assertion then passes): " + repr(offences)
    )


@pytest.mark.parametrize("rel_path", UTF8_STDOUT_ENTRY_POINTS)
def test_cli_entry_point_forces_utf8_stdout(rel_path):
    """The other half of the invariant.

    Callers decode our CLI's output as UTF-8. That is correct only while the CLI
    itself forces UTF-8 on stdout instead of writing in the machine's locale. If
    this promise is dropped, every `encoding="utf-8"` in the suite becomes wrong
    at once — and nothing else would notice, because the fixtures are ASCII.
    """
    path = REPO_ROOT / rel_path
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))

    reconfigures = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "reconfigure"
        and any(kw.arg == "encoding"
                and isinstance(kw.value, ast.Constant)
                and str(kw.value.value).lower().replace("-", "") == "utf8"
                for kw in node.keywords)
    ]
    assert reconfigures, (
        f"{rel_path} no longer forces UTF-8 on stdout. Callers in the suite "
        f'decode its output with encoding="utf-8"; without this reconfigure the '
        f"child writes in the machine's locale and the pair agrees only while "
        f"the output happens to be ASCII."
    )
