"""Captured subprocess output must be decoded with an explicit encoding.

Why this is a source check and not a behavioural one: `text=True` decodes with
the locale's preferred encoding, so the failure only exists on a machine whose
locale is not UTF-8. CI runs UTF-8, where a behavioural test passes with or
without the fix and therefore proves nothing. This check fails on every platform
the moment the class is reintroduced, which is what actually protects us.

The failure mode is why it is worth a test at all: the decode error is raised
inside subprocess's reader thread, so the caller sees no exception — only an
empty string where the output should be. Code then takes the "not found" branch
and acts on it. See [[global/patterns/subprocess-text-true-decodes-by-locale]].
"""
import ast
from pathlib import Path

import symbiosis_brain

PACKAGE_DIR = Path(symbiosis_brain.__file__).parent


def _capturing_run_calls_without_encoding(tree: ast.AST) -> list[int]:
    """Line numbers of subprocess.run(...) calls that capture text without an encoding."""
    offenders = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        is_subprocess_run = (
            isinstance(func, ast.Attribute)
            and func.attr == "run"
            and isinstance(func.value, ast.Name)
            and func.value.id == "subprocess"
        )
        if not is_subprocess_run:
            continue
        kwargs = {kw.arg for kw in node.keywords if kw.arg}
        decodes_text = "text" in kwargs or "universal_newlines" in kwargs
        if decodes_text and "encoding" not in kwargs:
            offenders.append(node.lineno)
    return offenders


def test_no_subprocess_run_decodes_by_locale():
    found = {}
    for path in sorted(PACKAGE_DIR.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        lines = _capturing_run_calls_without_encoding(tree)
        if lines:
            found[path.name] = lines

    assert not found, (
        "subprocess.run(..., text=True) without encoding= decodes by locale and "
        "silently yields empty output on a non-UTF-8 system: " + repr(found)
    )
