"""CP-4 / I-12, I-14 (слайс 2): значение, которым подписываются три пишущих тула.

Фикстуры — синтетика (§11.2): выдуманный клиент `testclient/9.9.9`, выдуманная
модель `test-model-9`. Реальных имён клиентов, моделей и нот здесь быть не должно.
"""
from datetime import date

import pytest

from symbiosis_brain import provenance


class _Info:
    name = "testclient"
    version = "9.9.9"


class _Params:
    clientInfo = _Info()


class _Session:
    client_params = _Params()


class _Ctx:
    session = _Session()


class _App:
    """Минимальный дубль mcp Server: путь доступа §3.3 —
    app.request_context.session.client_params.clientInfo."""
    request_context = _Ctx()


class _AppOutsideRequest:
    @property
    def request_context(self):
        raise LookupError("called outside of a request context")


def test_value_is_client_then_model_then_date():
    value = provenance.written_by_value(_App(), today=date(2026, 1, 2))
    client, model, day = value.split(" ", 2)
    assert client == "testclient/9.9.9"
    assert model == "unknown"          # писателя моста ещё нет — он в CP-5
    assert day == "2026-01-02"


def test_value_without_client_info_is_unknown_unknown():
    """clientInfo недоступен → строка всё равно пишется: «нет поля» зарезервировано
    за легаси-нотами (§1.2, B8), а не за неудачным рукопожатием."""
    value = provenance.written_by_value(_AppOutsideRequest(), today=date(2026, 1, 2))
    assert value == "unknown/unknown unknown 2026-01-02"


def test_value_defaults_to_today():
    assert provenance.written_by_value(_App()).endswith(" " + date.today().isoformat())


def test_value_is_never_date_like():
    """Защита от рецидива B5: рукописное `written_by: 2026-08-26` возвращается из
    YAML как datetime.date. Наша грамматика содержит '/' и пробелы, поэтому
    date-подобной не бывает никогда."""
    value = provenance.written_by_value(_App(), today=date(2026, 1, 2))
    assert "/" in value and value.count(" ") == 2


@pytest.mark.parametrize("model", [None, "", "   ", 7, False, [], {}])
def test_invalid_model_is_unknown(model):
    assert provenance.written_by_value(_App(), date(2026, 1, 2), model=model) == (
        "testclient/9.9.9 unknown 2026-01-02"
    )


def test_explicit_model_is_trimmed_and_normalized():
    assert provenance.written_by_value(
        _App(), date(2026, 1, 2), model=" Test Model 9 ", role="colleague",
    ) == "testclient/9.9.9 Test-Model-9 2026-01-02 · colleague"


@pytest.mark.parametrize(("model", "expected"), [
    ("  provider/model.v2:tag@host+variant_9  ", "provider/model.v2:tag@host+variant_9"),
    ("test   model---9", "test-model-9"),
    ("[model](https://example.invalid)\n*forged*", "-model-https://example.invalid-forged-"),
    ("x\ry\nz", "x-y-z"),
    ("m" * 100, "m" * 80),
])
def test_model_is_safe_single_token(model, expected):
    assert provenance.written_by_value(_App(), date(2026, 1, 2), model=model) == (
        f"testclient/9.9.9 {expected} 2026-01-02"
    )


@pytest.mark.parametrize("role", ["lead", "colleague", "subagent"])
def test_allowed_roles_are_appended(role):
    assert provenance.written_by_value(_App(), model="test-model", role=role).endswith(" · " + role)


@pytest.mark.parametrize("role", [None, "", "other", "lead\nforged", [], 7])
def test_invalid_role_is_omitted(role):
    value = provenance.written_by_value(_App(), model="test-model", role=role)
    assert " · " not in value
    assert "\n" not in value and "\r" not in value


@pytest.mark.parametrize("tool", ["brain_write", "brain_append", "brain_patch", "brain_rotate_handoffs"])
@pytest.mark.parametrize("server", ["symbiosis-brain", "symbiosis_brain"])
def test_brain_write_tool_accepts_only_explicit_mcp_aliases(tool, server):
    assert provenance.is_brain_write_tool(f"mcp__{server}__{tool}")


@pytest.mark.parametrize("tool", ["brain_write", "mcp__other__brain_write", "prefix_brain_append", "mcp__symbiosis-brain__brain_read", "mcp__symbiosis-brain__brain_write_extra"])
def test_brain_write_tool_rejects_unrelated_names(tool):
    assert not provenance.is_brain_write_tool(tool)
