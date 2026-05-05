import pytest
from symbiosis_brain.scope_resolver import normalize_scope


@pytest.mark.parametrize("raw, expected", [
    # Camel case → kebab
    ("AlphaDiagnostics", "alpha-diagnostics"),
    ("MyCoolApp", "my-cool-app"),
    ("ABCService", "abc-service"),  # acronym splits at last upper-before-lower (XMLParser → xml-parser)
    # Точки → дефисы
    ("Alpha.Pdf", "alpha-pdf"),
    ("My.Cool.App", "my-cool-app"),
    # Подчёркивания → дефисы
    ("my_cool_app", "my-cool-app"),
    # Уже kebab — без изменений
    ("alpha-faq", "alpha-faq"),
    ("beta", "beta"),
    # Смешанное
    ("Alpha_Cool.App", "alpha-cool-app"),
    # Спец-символы — стрипаются
    ("foo@bar!baz", "foobarbaz"),
    # Несколько разделителей подряд → один дефис
    ("a__b", "a-b"),
    ("a..b", "a-b"),
    ("Alpha_-Diag", "alpha-diag"),
    # Цифры допустимы
    ("Project2026", "project2026"),
    # Пустые края — стрипаются
    ("-foo-", "foo"),
    # Edge case: пустая строка → пусто (caller проверяет)
    ("", ""),
    # Edge case: только разделители → пусто
    ("---", ""),
    # Edge case (reviewer): only-special-chars → пусто
    ("@@@", ""),
])
def test_normalize_scope(raw, expected):
    assert normalize_scope(raw) == expected


# ---------------------------------------------------------------------------
# parse_marker tests
# ---------------------------------------------------------------------------

from pathlib import Path
from symbiosis_brain.scope_resolver import parse_marker, Marker


def write_claude_md(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "CLAUDE.md"
    p.write_text(body, encoding="utf-8")
    return p


def test_parse_marker_v1_full(tmp_path):
    f = write_claude_md(tmp_path, "# Project\n\nstuff\n\n<!-- symbiosis-brain v1: scope=alpha-diagnostics, umbrella=alpha -->\n")
    m = parse_marker(f)
    assert m == Marker(version=1, scope="alpha-diagnostics", umbrella="alpha", status=None)


def test_parse_marker_v1_no_umbrella(tmp_path):
    f = write_claude_md(tmp_path, "<!-- symbiosis-brain v1: scope=beta -->\n")
    m = parse_marker(f)
    assert m == Marker(version=1, scope="beta", umbrella=None, status=None)


def test_parse_marker_v1_with_status(tmp_path):
    f = write_claude_md(tmp_path, "<!-- symbiosis-brain v1: scope=foo, status=draft -->\n")
    m = parse_marker(f)
    assert m == Marker(version=1, scope="foo", umbrella=None, status="draft")


def test_parse_marker_v1_full_with_status(tmp_path):
    f = write_claude_md(tmp_path, "<!-- symbiosis-brain v1: scope=foo, umbrella=bar, status=draft -->\n")
    m = parse_marker(f)
    assert m == Marker(version=1, scope="foo", umbrella="bar", status="draft")


def test_parse_marker_v2_future(tmp_path):
    f = write_claude_md(tmp_path, "<!-- symbiosis-brain v2: scope=foo, team=alpha -->\n")
    m = parse_marker(f)
    assert m is not None
    assert m.version == 2
    assert m.scope == "foo"


def test_parse_marker_no_marker(tmp_path):
    f = write_claude_md(tmp_path, "# Project\n\nNo marker here.\n")
    assert parse_marker(f) is None


def test_parse_marker_corrupt_returns_none(tmp_path):
    f = write_claude_md(tmp_path, "<!-- symbiosis-brain something-broken -->\n")
    assert parse_marker(f) is None


def test_parse_marker_missing_file(tmp_path):
    assert parse_marker(tmp_path / "nope.md") is None


def test_parse_marker_takes_last_when_multiple(tmp_path):
    """Если каким-то образом два маркера — берём последний (legitimate state после миграции)."""
    body = (
        "<!-- symbiosis-brain v1: scope=old -->\n"
        "stuff\n"
        "<!-- symbiosis-brain v1: scope=new, umbrella=u -->\n"
    )
    f = write_claude_md(tmp_path, body)
    m = parse_marker(f)
    assert m.scope == "new"
    assert m.umbrella == "u"


def test_parse_marker_whitespace_tolerant(tmp_path):
    """Разрешаем небольшую вариацию пробелов внутри маркера."""
    f = write_claude_md(tmp_path, "<!--  symbiosis-brain  v1:  scope=foo,  umbrella=bar  -->\n")
    m = parse_marker(f)
    assert m == Marker(version=1, scope="foo", umbrella="bar", status=None)


def test_parse_marker_uppercase_marker_not_matched(tmp_path):
    """Uppercase marker is treated as no marker — we control marker emission."""
    f = write_claude_md(tmp_path, "<!-- SYMBIOSIS-BRAIN V1: SCOPE=foo -->\n")
    assert parse_marker(f) is None


def test_parse_marker_non_utf8_file_returns_none(tmp_path):
    """File with bytes that don't decode as UTF-8 → None, not raise."""
    f = tmp_path / "CLAUDE.md"
    # UTF-16-LE bytes for "<!-- symbiosis-brain v1: scope=foo -->"
    f.write_bytes("<!-- symbiosis-brain v1: scope=foo -->\n".encode("utf-16-le"))
    # With errors="replace" decoding succeeds but produces garbage; regex won't match.
    assert parse_marker(f) is None


# ---------------------------------------------------------------------------
# _MARKER_RE direct-regex tests — pin pattern behavior independent of parse_marker
# ---------------------------------------------------------------------------

from symbiosis_brain.scope_resolver import _MARKER_RE


def test_marker_re_captures_version_and_body():
    m = _MARKER_RE.search("<!-- symbiosis-brain v1: scope=foo -->")
    assert m is not None
    assert m.group("version") == "1"
    assert m.group("body") == "scope=foo"


def test_marker_re_multi_digit_version():
    m = _MARKER_RE.search("<!-- symbiosis-brain v42: scope=x -->")
    assert m is not None and m.group("version") == "42"


def test_marker_re_does_not_match_uppercase():
    assert _MARKER_RE.search("<!-- SYMBIOSIS-BRAIN V1: SCOPE=foo -->") is None


def test_marker_re_requires_closing_tag():
    assert _MARKER_RE.search("<!-- symbiosis-brain v1: scope=foo") is None


def test_marker_re_requires_opening_tag():
    assert _MARKER_RE.search("symbiosis-brain v1: scope=foo -->") is None


def test_marker_re_whitespace_tolerant():
    m = _MARKER_RE.search("<!--   symbiosis-brain   v1   :   scope=foo  -->")
    assert m is not None
    assert m.group("body") == "scope=foo"


def test_marker_re_finditer_returns_all_markers():
    text = (
        "<!-- symbiosis-brain v1: scope=a -->\n"
        "noise\n"
        "<!-- symbiosis-brain v1: scope=b, umbrella=u -->\n"
    )
    matches = list(_MARKER_RE.finditer(text))
    assert len(matches) == 2
    assert matches[0].group("body") == "scope=a"
    assert matches[1].group("body") == "scope=b, umbrella=u"


def test_marker_re_body_stops_at_gt():
    """Body group is [^>]*? — stops at first `>`, won't bleed past closing tag."""
    text = "<!-- symbiosis-brain v1: scope=foo --> some > extra -->"
    m = _MARKER_RE.search(text)
    assert m is not None
    assert m.group("body") == "scope=foo"
