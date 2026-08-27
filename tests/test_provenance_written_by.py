"""CP-4 / I-12, I-14 (слайс 2): значение, которым подписываются три пишущих тула.

Фикстуры — синтетика (§11.2): выдуманный клиент `testclient/9.9.9`, выдуманная
модель `test-model-9`. Реальных имён клиентов, моделей и нот здесь быть не должно.
"""
from datetime import date

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


def test_model_from_bridge_is_unknown_without_a_writer(tmp_path, monkeypatch):
    """Слайс 2: моста нет. Тест переживает CP-5 — в пустом каталоге ни своего
    файла, ни чужих, значит `unknown` и там."""
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    monkeypatch.setenv("TEMP", str(tmp_path))
    assert provenance.model_from_bridge() == "unknown"


def test_bridge_constants_are_the_contract():
    assert provenance.BRIDGE_PREFIX == "brain-model-"
    assert provenance.BRIDGE_TTL_SECONDS == 900
