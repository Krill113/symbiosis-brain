"""brain_write hard-block when gist is missing (validation gate)."""
import pytest
import asyncio
from pathlib import Path


@pytest.mark.asyncio
async def test_brain_write_blocks_when_gist_missing(tmp_vault: Path, db_path: Path):
    from symbiosis_brain import server
    from symbiosis_brain.storage import Storage
    from symbiosis_brain.search import SearchEngine
    from symbiosis_brain.sync import VaultSync
    from symbiosis_brain.temporal import TemporalManager

    server._storage = Storage(db_path)
    server._search = SearchEngine(server._storage)
    server._sync = VaultSync(tmp_vault, server._storage)
    server._temporal = TemporalManager(server._storage)
    server._vault_path = tmp_vault
    server._ready = asyncio.Event()
    server._ready.set()

    result = await server.call_tool("brain_write", {
        "path": "wiki/test.md", "title": "T", "body": "Body",
        "note_type": "wiki", "scope": "global",
    })
    text = result[0].text
    assert "Error" in text
    assert "gist" in text.lower()  # error message mentions gist
    assert not (tmp_vault / "wiki" / "test.md").exists()  # file not written


@pytest.mark.asyncio
async def test_brain_write_no_warning_when_gist_present(tmp_vault: Path, db_path: Path):
    from symbiosis_brain import server
    from symbiosis_brain.storage import Storage
    from symbiosis_brain.search import SearchEngine
    from symbiosis_brain.sync import VaultSync
    from symbiosis_brain.temporal import TemporalManager

    server._storage = Storage(db_path)
    server._search = SearchEngine(server._storage)
    server._sync = VaultSync(tmp_vault, server._storage)
    server._temporal = TemporalManager(server._storage)
    server._vault_path = tmp_vault
    server._ready = asyncio.Event()
    server._ready.set()

    result = await server.call_tool("brain_write", {
        "path": "wiki/test2.md", "title": "T2", "body": "Body",
        "note_type": "wiki", "scope": "global",
        "gist": "A useful one-line gist",
    })
    text = result[0].text
    assert "Saved" in text
    assert "⚠️" not in text  # no warning


# ================== CP-7: сигнал `[dedup]` (I-25, §5.4) ==================
# Ноты синтетические. Тесты идут через настоящие Storage/SearchEngine: сигнал
# требует `_in_both`, то есть обеих половин поиска, и мок здесь проверял бы
# мок. Модель эмбеддера в dev-окружении уже прогрета фикстурами других файлов.
#
# Провенанс здесь НЕ подменяется, и это осознанно (шов CP-4/CP-5 -> CP-7).
# brain_write после CP-4 штампует written_by, а после CP-5 model_from_bridge
# читает живой TEMP; ни один assert ниже значения штампа не касается, а
# model_from_bridge по контракту не бросает и на мусоре отдаёт 'unknown'
# (I-14 п. 3). Фикстура stub_provenance из tests/test_server_write_gates.py
# сюда не видна (она не в conftest) и не нужна — если тесту CP-7 когда-нибудь
# понадобится точное значение written_by, фикстуру переносят в conftest, а не
# заводят вторую копию.


def _wire_server(tmp_vault: Path, db_path: Path):
    from symbiosis_brain import server
    from symbiosis_brain.search import SearchEngine
    from symbiosis_brain.storage import Storage
    from symbiosis_brain.sync import VaultSync
    from symbiosis_brain.temporal import TemporalManager

    server._storage = Storage(db_path)
    server._search = SearchEngine(server._storage)
    server._sync = VaultSync(tmp_vault, server._storage)
    server._temporal = TemporalManager(server._storage)
    server._vault_path = tmp_vault
    server._ready = asyncio.Event()
    server._ready.set()
    assert server._search._vec_enabled, (
        "sqlite-vec недоступен — сигнал требует `_in_both`, то есть обеих половин "
        "поиска (pyproject.toml:25 объявляет зависимость обязательной)"
    )
    return server


@pytest.fixture(autouse=True)
def _clean_dedup_env_cache():
    from symbiosis_brain.search import _DEDUP_ENV_CACHE

    _DEDUP_ENV_CACHE.clear()
    yield
    _DEDUP_ENV_CACHE.clear()


_A = {
    "path": "wiki/retrieval-log-rotation.md",
    "title": "Retrieval log rotation",
    "gist": "Rotation deletes retrieval events older than ninety days",
    "body": "Rotation deletes retrieval events older than ninety days, keeping the database small.",
    "note_type": "wiki",
    "scope": "global",
}


def _twin(path: str) -> dict:
    twin = dict(_A)
    twin["path"] = path
    return twin


@pytest.mark.asyncio
async def test_brain_write_warns_about_a_near_duplicate(tmp_vault: Path, db_path: Path):
    server = _wire_server(tmp_vault, db_path)
    first = await server.call_tool("brain_write", dict(_A))
    assert "Saved" in first[0].text
    assert "[dedup]" not in first[0].text          # дубля ещё не было

    second = await server.call_tool("brain_write", _twin("wiki/retrieval-log-rotation-2.md"))
    text = second[0].text

    assert "[dedup] похоже на: " in text
    assert _A["path"] in text
    assert text.startswith("Saved: ")              # запись НЕ заблокирована
    assert not text.startswith("Error:")           # hooks/brain-save-marker.sh:40
    assert (tmp_vault / "wiki" / "retrieval-log-rotation-2.md").exists()
    # порядок: сообщение -> [dedup] -> [counter] (I-25)
    assert text.index("[dedup]") < text.index("[counter]")


@pytest.mark.asyncio
async def test_brain_write_dedup_line_shape(tmp_vault: Path, db_path: Path):
    server = _wire_server(tmp_vault, db_path)
    await server.call_tool("brain_write", dict(_A))
    text = (await server.call_tool(
        "brain_write", _twin("wiki/retrieval-log-rotation-3.md")))[0].text

    line = next(ln for ln in text.splitlines() if ln.startswith("[dedup]"))
    assert line.startswith("[dedup] похоже на: ")
    assert " — " in line
    body = line[len("[dedup] похоже на: "):]
    assert len(body.split("; ")) <= 2               # DEDUP_MAX_SHOWN
    for chunk in body.split("; "):
        gist = chunk.split(" — ", 1)[1]
        assert len(gist) <= 80                      # I-25: гист режется до 80


@pytest.mark.asyncio
async def test_brain_write_silent_on_a_new_topic(tmp_vault: Path, db_path: Path):
    server = _wire_server(tmp_vault, db_path)
    await server.call_tool("brain_write", dict(_A))
    text = (await server.call_tool("brain_write", {
        "path": "wiki/valve-sizing.md",
        "title": "Valve sizing",
        "gist": "How to size a valve for a cold water network",
        "body": "Sizing rules for valves in a cold water network.",
        "note_type": "wiki", "scope": "global",
    }))[0].text
    assert "Saved" in text
    assert "[dedup]" not in text


@pytest.mark.asyncio
async def test_brain_write_does_not_warn_about_itself(tmp_vault: Path, db_path: Path):
    """Перезапись той же ноты: единственный кандидат — она сама (§5.2)."""
    server = _wire_server(tmp_vault, db_path)
    await server.call_tool("brain_write", dict(_A))
    text = (await server.call_tool("brain_write", dict(_A)))[0].text
    assert "Saved" in text
    assert "[dedup]" not in text


@pytest.mark.asyncio
async def test_dedup_min_zero_silences_the_signal(tmp_vault: Path, db_path: Path, monkeypatch):
    from symbiosis_brain.search import _DEDUP_ENV_CACHE

    server = _wire_server(tmp_vault, db_path)
    await server.call_tool("brain_write", dict(_A))
    monkeypatch.setenv("SYMBIOSIS_BRAIN_DEDUP_MIN", "0")
    _DEDUP_ENV_CACHE.clear()
    text = (await server.call_tool(
        "brain_write", _twin("wiki/retrieval-log-rotation-4.md")))[0].text
    assert "Saved" in text
    assert "[dedup]" not in text


@pytest.mark.asyncio
async def test_dedup_failure_never_blocks_the_write(tmp_vault: Path, db_path: Path, monkeypatch):
    """§5.4: при любой ошибке строки просто нет — запись всё равно происходит."""
    server = _wire_server(tmp_vault, db_path)

    def _boom(*args, **kwargs):
        raise RuntimeError("дедуп упал")

    monkeypatch.setattr(server, "dedup_candidates", _boom)
    text = (await server.call_tool("brain_write", dict(_A)))[0].text
    assert text.startswith("Saved: ")
    assert "[dedup]" not in text
    assert (tmp_vault / "wiki" / "retrieval-log-rotation.md").exists()


@pytest.mark.asyncio
async def test_append_and_patch_do_not_compute_dedup(tmp_vault: Path, db_path: Path, monkeypatch):
    """§5.2: `brain_append`/`brain_patch` правят существующую ноту — дубля нет."""
    server = _wire_server(tmp_vault, db_path)
    await server.call_tool("brain_write", {
        **_A, "body": "## Section\n\nRotation deletes retrieval events older than ninety days.",
    })

    calls: list = []

    def _spy(*args, **kwargs):
        calls.append(kwargs)
        return []

    monkeypatch.setattr(server, "dedup_candidates", _spy)

    appended = await server.call_tool("brain_append", {
        "path": _A["path"], "section": "Section", "content": "one more line",
    })
    assert "[dedup]" not in appended[0].text
    patched = await server.call_tool("brain_patch", {
        "path": _A["path"], "anchor": "one more line", "replacement": "another line",
    })
    assert "[dedup]" not in patched[0].text
    assert calls == []
