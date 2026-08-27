"""Integration: brain_write MCP handler must reject hard-block violations and
render soft warnings without writing partial state.

Test pattern — see existing tests/test_server_append_patch.py for the canonical
shape: import `symbiosis_brain.server as server_mod`, init the module-level
state via `server_mod._init(tmp_vault_with_taxonomy)`, call tools via
`await server_mod.call_tool(...)`, then teardown by closing storage and resetting
all the module globals (otherwise the next test reuses stale state).

`pytest-asyncio` is in `asyncio_mode = "auto"` (see pyproject.toml), so async
test functions and async fixtures work without explicit `@pytest.mark.asyncio`.
"""
from pathlib import Path

import pytest

import symbiosis_brain.server as server_mod


@pytest.fixture
async def initialized_server(tmp_vault_with_taxonomy: Path):
    """Initialize module-level server state for a tmp vault. Tear down after."""
    server_mod._init(tmp_vault_with_taxonomy)
    yield server_mod
    if server_mod._storage is not None:
        server_mod._storage.close()
    for attr in ("_storage", "_search", "_sync", "_graph", "_temporal",
                 "_linter", "_vault_path"):
        setattr(server_mod, attr, None)


@pytest.fixture
async def initialized_server_with_anchor(initialized_server, tmp_vault_with_taxonomy: Path):
    """initialized_server + a pre-seeded `wiki/anchor.md` note that's safe to
    reference in test bodies (avoids broken-ref hard-block during setup)."""
    server_mod._storage.upsert_note(
        path="wiki/anchor.md",
        title="Anchor",
        content="# H",
        note_type="wiki",
        scope="global",
        tags=[],
        frontmatter={"gist": "anchor for test refs"},
        valid_from=None,
        valid_to=None,
    )
    yield server_mod


async def _call(name: str, args: dict) -> str:
    result = await server_mod.call_tool(name, args)
    return result[0].text


async def test_brain_write_missing_gist_does_not_write_file(
    initialized_server, tmp_vault_with_taxonomy: Path,
):
    text = await _call("brain_write", {
        "path": "wiki/new.md",
        "title": "New",
        "body": "# H",
    })
    assert "gist" in text.lower()
    assert "error" in text.lower() or "required" in text.lower()
    assert not (tmp_vault_with_taxonomy / "wiki" / "new.md").exists()


async def test_brain_write_broken_ref_does_not_write_file(
    initialized_server, tmp_vault_with_taxonomy: Path,
):
    text = await _call("brain_write", {
        "path": "wiki/new.md",
        "title": "New",
        "body": "# H\n[[wiki/does-not-exist]]",
        "gist": "ok",
    })
    assert "broken" in text.lower()
    assert not (tmp_vault_with_taxonomy / "wiki" / "new.md").exists()


async def test_brain_write_long_gist_writes_with_warning(
    initialized_server_with_anchor, tmp_vault_with_taxonomy: Path,
):
    """Soft-zone gist (>100 but ≤140) writes successfully with warning."""
    long_gist = "x" * 130
    text = await _call("brain_write", {
        "path": "wiki/new.md",
        "title": "New",
        "body": "# H\n[[wiki/anchor]] [[wiki/anchor]]",
        "gist": long_gist,
    })
    assert "saved" in text.lower()
    assert "gist" in text.lower() and "130" in text
    assert (tmp_vault_with_taxonomy / "wiki" / "new.md").exists()


async def test_brain_append_introducing_broken_ref_blocks(
    initialized_server_with_anchor, tmp_vault_with_taxonomy: Path,
):
    await _call("brain_write", {
        "path": "wiki/host.md",
        "title": "Host",
        "body": "# H\n## Sec\nfoo\n[[wiki/anchor]] [[wiki/anchor]]",
        "gist": "x",
    })
    text = await _call("brain_append", {
        "path": "wiki/host.md",
        "section": "Sec",
        "content": "[[wiki/never-existed]]",
    })
    assert "broken" in text.lower() or "error" in text.lower()
    body = (tmp_vault_with_taxonomy / "wiki" / "host.md").read_text(encoding="utf-8")
    assert "never-existed" not in body


async def test_brain_append_no_new_links_does_not_validate(
    initialized_server_with_anchor, tmp_vault_with_taxonomy: Path,
):
    """Pure-text appends bypass validation — they cannot introduce breakage."""
    await _call("brain_write", {
        "path": "wiki/host2.md",
        "title": "Host2",
        "body": "# H\n## Sec\nfoo\n[[wiki/anchor]] [[wiki/anchor]]",
        "gist": "x",
    })
    text = await _call("brain_append", {
        "path": "wiki/host2.md",
        "section": "Sec",
        "content": "more text without any wiki-links",
    })
    assert "appended" in text.lower()


# ============================ CP-4 / I-13, I-12 ==============================
# Слияние frontmatter, атомарность цикла и штамп written_by.
# Синтетика: выдуманные ноты, выдуманный клиент `testclient/9.9.9`, выдуманная
# модель `test-model-9`.

import re
from contextlib import contextmanager
from datetime import date

from symbiosis_brain import provenance


@pytest.fixture
def stub_provenance(monkeypatch):
    """Значение штампа делаем детерминированным, но НЕ подменяем written_by_value —
    иначе тест перестаёт проверять её композицию. Подменяются обе половины:
    client_id (в тесте нет MCP-рукопожатия) и model_from_bridge (в CP-5 у моста
    появится настоящий читатель, и тест не должен от него зависеть)."""
    monkeypatch.setattr(provenance, "client_id", lambda app: "testclient/9.9.9")
    monkeypatch.setattr(provenance, "model_from_bridge", lambda *a, **kw: "test-model-9")
    return f"testclient/9.9.9 test-model-9 {date.today().isoformat()}"


async def _seed(path: str, **extra) -> None:
    args = {"path": path, "title": "Seed", "body": "Seed body",
            "note_type": "wiki", "scope": "global", "gist": "seeded note"}
    args.update(extra)
    text = await _call("brain_write", args)
    assert text.startswith("Saved:"), text


def _meta(vault: Path, path: str) -> dict:
    import frontmatter as _fm
    return dict(_fm.loads((vault / path).read_text(encoding="utf-8")).metadata)


async def test_rewrite_keeps_unknown_frontmatter_keys(
    initialized_server, tmp_vault_with_taxonomy: Path, stub_provenance,
):
    """Дефект B4: umbrella/aliases/superseded_by/allow_type_mismatch сегодня
    исчезают при каждой перезаписи (22 ноты, [отчёт 02, P16])."""
    note = tmp_vault_with_taxonomy / "wiki" / "merge-a.md"
    await _seed("wiki/merge-a.md")
    raw = note.read_text(encoding="utf-8")
    note.write_text(
        raw.replace("title: Seed",
                    "title: Seed\numbrella: alpha\naliases: [ALPHA]\n"
                    "superseded_by: wiki/merge-b\nallow_type_mismatch: true"),
        encoding="utf-8",
    )
    await _call("brain_write", {
        "path": "wiki/merge-a.md", "title": "Seed 2", "body": "New body",
        "note_type": "wiki", "scope": "global", "gist": "rewritten",
    })
    meta = _meta(tmp_vault_with_taxonomy, "wiki/merge-a.md")
    assert meta["umbrella"] == "alpha"
    assert meta["aliases"] == ["ALPHA"]
    assert meta["superseded_by"] == "wiki/merge-b"
    assert meta["allow_type_mismatch"] is True
    assert meta["title"] == "Seed 2"          # новое имя не воскрешается старым
    assert meta["gist"] == "rewritten"


async def test_rewrite_without_scope_keeps_the_file_scope(
    initialized_server, tmp_vault_with_taxonomy: Path, stub_provenance,
):
    await _seed("wiki/merge-scope.md", scope="beta", note_type="decision")
    await _call("brain_write", {
        "path": "wiki/merge-scope.md", "title": "Seed", "body": "New body",
        "gist": "no scope, no note_type in this call",
    })
    meta = _meta(tmp_vault_with_taxonomy, "wiki/merge-scope.md")
    assert meta["scope"] == "beta"
    assert meta["type"] == "decision"


async def test_note_type_comes_from_the_call_not_from_a_type_key(
    initialized_server, tmp_vault_with_taxonomy: Path, stub_provenance,
):
    """`"type" in arguments` всегда ложна: у тула аргумент называется note_type
    (server.py:363). Без отображения ARG_TO_FM тип никогда не взялся бы из вызова."""
    await _seed("wiki/merge-type.md", note_type="wiki")
    await _call("brain_write", {
        "path": "wiki/merge-type.md", "title": "Seed", "body": "New body",
        "note_type": "reference", "gist": "type changes on this call",
    })
    assert _meta(tmp_vault_with_taxonomy, "wiki/merge-type.md")["type"] == "reference"


async def test_empty_scope_is_treated_as_absent(
    initialized_server, tmp_vault_with_taxonomy: Path, stub_provenance,
):
    """scope/note_type очистке не подлежат: render_note печатает их всегда."""
    await _seed("wiki/merge-empty-scope.md", scope="beta")
    await _call("brain_write", {
        "path": "wiki/merge-empty-scope.md", "title": "Seed", "body": "B",
        "scope": "", "gist": "empty scope means absent",
    })
    assert _meta(tmp_vault_with_taxonomy, "wiki/merge-empty-scope.md")["scope"] == "beta"


async def test_omitted_tags_survive_and_empty_tags_clear(
    initialized_server, tmp_vault_with_taxonomy: Path, stub_provenance,
):
    await _seed("wiki/merge-tags.md", tags=["alpha", "beta"])
    await _call("brain_write", {
        "path": "wiki/merge-tags.md", "title": "Seed", "body": "B",
        "gist": "tags not passed at all",
    })
    assert _meta(tmp_vault_with_taxonomy, "wiki/merge-tags.md")["tags"] == ["alpha", "beta"]

    await _call("brain_write", {
        "path": "wiki/merge-tags.md", "title": "Seed", "body": "B",
        "tags": [], "gist": "tags explicitly cleared",
    })
    assert "tags" not in _meta(tmp_vault_with_taxonomy, "wiki/merge-tags.md")


async def test_empty_valid_from_erases_the_key_instead_of_writing_a_blank(
    initialized_server, tmp_vault_with_taxonomy: Path, stub_provenance,
):
    """«Присутствует и пусто» — два действия: не пишем И не спасаем. Ровно одно из
    двух даёт либо `valid_from: ""` в файле, либо воскресшее старое значение."""
    await _seed("wiki/merge-valid.md", valid_from="2026-01-01", valid_to="2026-02-01")
    await _call("brain_write", {
        "path": "wiki/merge-valid.md", "title": "Seed", "body": "B",
        "valid_from": "", "gist": "valid_from cleared, valid_to untouched",
    })
    meta = _meta(tmp_vault_with_taxonomy, "wiki/merge-valid.md")
    assert "valid_from" not in meta
    assert str(meta["valid_to"]) == "2026-02-01"    # не передан → взят из файла


async def test_brain_write_stamps_written_by(
    initialized_server, tmp_vault_with_taxonomy: Path, stub_provenance,
):
    await _call("brain_write", {
        "path": "wiki/stamp-new.md", "title": "Stamped", "body": "B",
        "note_type": "wiki", "scope": "global", "gist": "fresh note",
    })
    assert _meta(tmp_vault_with_taxonomy, "wiki/stamp-new.md")["written_by"] == stub_provenance


async def test_rewrite_replaces_the_previous_stamp(
    initialized_server, tmp_vault_with_taxonomy: Path, stub_provenance,
):
    """merge_frontmatter старую подпись не возвращает, extra_fm перекрывает
    preserved — вместе это гарантирует, что чужая подпись не переживёт запись."""
    note = tmp_vault_with_taxonomy / "wiki" / "stamp-old.md"
    await _seed("wiki/stamp-old.md")
    raw = note.read_text(encoding="utf-8")
    note.write_text(
        raw.replace("title: Seed",
                    "title: Seed\nwritten_by: oldclient/0.0.1 old-model-1 2026-01-01"),
        encoding="utf-8",
    )
    await _call("brain_write", {
        "path": "wiki/stamp-old.md", "title": "Seed", "body": "B", "gist": "rewritten",
    })
    text = note.read_text(encoding="utf-8")
    assert "oldclient/0.0.1" not in text
    assert _meta(tmp_vault_with_taxonomy, "wiki/stamp-old.md")["written_by"] == stub_provenance


async def test_written_by_value_called_exactly_once_per_call(
    initialized_server, tmp_vault_with_taxonomy: Path, monkeypatch,
):
    calls = []
    real = provenance.written_by_value

    def counting(app, today=None):
        calls.append(app)
        return real(app, today)

    monkeypatch.setattr(provenance, "written_by_value", counting)
    await _call("brain_write", {
        "path": "wiki/stamp-once.md", "title": "Once", "body": "B",
        "note_type": "wiki", "scope": "global", "gist": "one stamp per call",
    })
    assert len(calls) == 1


async def test_merge_cycle_runs_inside_one_note_lock(
    initialized_server, tmp_vault_with_taxonomy: Path, monkeypatch, stub_provenance,
):
    """Слияние делает brain_write read-modify-write, поэтому весь цикл обязан
    идти под note_write_lock, а запись внутри лока — через
    _write_note_body_unlocked (иначе лок берётся дважды, server.py:291-316)."""
    await _seed("wiki/lock-cycle.md")

    events: list[str] = []
    real_lock = server_mod.note_write_lock
    real_merge = server_mod.merge_frontmatter
    real_render = server_mod.render_note
    real_write = server_mod._write_note_body_unlocked
    real_locked_write = server_mod._write_note_body

    @contextmanager
    def recording_lock(vault_path, rel_path, *a, **kw):
        events.append("lock")
        with real_lock(vault_path, rel_path, *a, **kw):
            yield
        events.append("unlock")

    def recording_merge(existing, incoming):
        events.append("merge")
        return real_merge(existing, incoming)

    def recording_render(*a, **kw):
        events.append("render")
        return real_render(*a, **kw)

    def recording_write(*a, **kw):
        events.append("write")
        return real_write(*a, **kw)

    def forbidden_write(*a, **kw):
        events.append("write-with-its-own-lock")
        return real_locked_write(*a, **kw)

    monkeypatch.setattr(server_mod, "note_write_lock", recording_lock)
    monkeypatch.setattr(server_mod, "merge_frontmatter", recording_merge)
    monkeypatch.setattr(server_mod, "render_note", recording_render)
    monkeypatch.setattr(server_mod, "_write_note_body_unlocked", recording_write)
    monkeypatch.setattr(server_mod, "_write_note_body", forbidden_write)

    await _call("brain_write", {
        "path": "wiki/lock-cycle.md", "title": "Seed", "body": "B2", "gist": "locked",
    })
    assert events == ["lock", "merge", "render", "write", "unlock"]


async def test_escaping_path_is_rejected_before_the_file_is_read(
    initialized_server, tmp_vault_with_taxonomy: Path, monkeypatch, stub_provenance,
):
    """Чтение по непроверенному пути «чтобы слить frontmatter» превратило бы
    brain_write в примитив чтения произвольного файла (§3.5)."""
    outside = tmp_vault_with_taxonomy.parent / "outside-the-vault.md"
    outside.write_text("---\nsecret: leak-canary\n---\nbody\n", encoding="utf-8")

    seen: list[str] = []
    real_loads = server_mod.frontmatter.loads

    def spying_loads(text, *a, **kw):
        seen.append(text)
        return real_loads(text, *a, **kw)

    monkeypatch.setattr(server_mod.frontmatter, "loads", spying_loads)
    text = await _call("brain_write", {
        "path": "../outside-the-vault.md", "title": "X", "body": "B",
        "note_type": "wiki", "scope": "global", "gist": "must not be written",
    })
    assert text == "Error: path must be within vault"
    assert not any("leak-canary" in t for t in seen)
    assert "leak-canary" in outside.read_text(encoding="utf-8")


async def test_arg_to_fm_covers_every_frontmatter_argument(initialized_server):
    """Появится новый аргумент у brain_write — строка добавляется в ARG_TO_FM,
    иначе поле молча теряет связь с вызовом (§3.5)."""
    assert server_mod.ARG_TO_FM == {
        "note_type": "type", "scope": "scope", "tags": "tags",
        "gist": "gist", "valid_from": "valid_from", "valid_to": "valid_to",
    }
    assert server_mod.NON_ERASABLE == ("note_type", "scope")


async def test_brain_append_stamps_written_by(
    initialized_server, tmp_vault_with_taxonomy: Path, stub_provenance,
):
    """§3.1: append — адресная правка ОДНОЙ ноты этим клиентом и этой моделью,
    поэтому подпись обновляется. Прежняя при этом теряется — принятая цена."""
    await _seed("wiki/append-stamp.md")
    note = tmp_vault_with_taxonomy / "wiki" / "append-stamp.md"
    # `_seed` already stamps written_by (Task 4.3-4.5 already ships that), so the
    # file already carries `written_by: {stub_provenance}`. Replace that line
    # instead of injecting a duplicate `written_by:` key next to `title` — a
    # duplicate key parses to whichever occurrence comes LAST (see the rename
    # test below), which would silently make this "old stamp" invisible.
    note.write_text(
        note.read_text(encoding="utf-8").replace(
            f"written_by: {stub_provenance}",
            "written_by: oldclient/0.0.1 old-model-1 2026-01-01",
        ) + "\n## Notes\n\nfirst line\n",
        encoding="utf-8",
    )
    msg = await _call("brain_append", {
        "path": "wiki/append-stamp.md", "section": "Notes", "content": "second line",
    })
    assert msg.startswith("Appended to")
    meta = _meta(tmp_vault_with_taxonomy, "wiki/append-stamp.md")
    assert meta["written_by"] == stub_provenance
    assert "second line" in note.read_text(encoding="utf-8")


async def test_brain_patch_stamps_written_by(
    initialized_server, tmp_vault_with_taxonomy: Path, stub_provenance,
):
    await _seed("wiki/patch-stamp.md")
    note = tmp_vault_with_taxonomy / "wiki" / "patch-stamp.md"
    # Same reasoning as the append test above: overwrite the seed's own stamp
    # with an "old" one first, so the final assertion can tell "patch restamps"
    # apart from "patch happens to leave the seed's stamp untouched".
    note.write_text(
        note.read_text(encoding="utf-8").replace(
            f"written_by: {stub_provenance}",
            "written_by: oldclient/0.0.1 old-model-1 2026-01-01",
        ) + "\nanchor-text-here\n",
        encoding="utf-8",
    )
    msg = await _call("brain_patch", {
        "path": "wiki/patch-stamp.md", "anchor": "anchor-text-here",
        "replacement": "replaced-text",
    })
    assert msg.startswith("Patched")
    assert _meta(tmp_vault_with_taxonomy, "wiki/patch-stamp.md")["written_by"] == stub_provenance


async def test_brain_rename_does_not_stamp_the_sources_it_rewrites(
    initialized_server, tmp_vault_with_taxonomy: Path, stub_provenance,
):
    """rename/delete переписывают тела ЧУЖИХ нот ради ссылок: один вызов подписал
    бы нашей моделью десятки нот, которых она не писала ([отчёт 02, P24])."""
    await _seed("wiki/rename-target.md")
    await _seed("wiki/rename-source.md", body="see [[wiki/rename-target]] here")
    src = tmp_vault_with_taxonomy / "wiki" / "rename-source.md"
    # `_seed` already runs under `stub_provenance`, so the file already carries a
    # `written_by:` line (Task 4.3-4.5 stamp it on every brain_write). Injecting a
    # SECOND `written_by:` line here (as a plain `"title: Seed"` insertion would)
    # creates a duplicate YAML key; python-frontmatter's loader keeps the LAST one,
    # silently resurrecting the fresh stamp instead of the "old" one this test
    # means to plant. Replace the existing line instead.
    src.write_text(
        src.read_text(encoding="utf-8").replace(
            f"written_by: {stub_provenance}",
            "written_by: oldclient/0.0.1 old-model-1 2026-01-01",
        ),
        encoding="utf-8",
    )
    await _call("brain_rename", {
        "old_path": "wiki/rename-target.md", "new_path": "wiki/rename-renamed.md",
    })
    meta = _meta(tmp_vault_with_taxonomy, "wiki/rename-source.md")
    assert meta["written_by"] == "oldclient/0.0.1 old-model-1 2026-01-01"


async def test_rewrite_over_unparsable_frontmatter_degrades_instead_of_crashing(
    initialized_server, tmp_vault_with_taxonomy: Path, stub_provenance,
):
    """A1 (F4+F5+F6): a stray colon in `gist:` makes python-frontmatter raise
    `yaml.YAMLError` on load. Before this test, that exception was uncaught, so
    the only write path into the vault (brain_write) could never repair the note
    it came from — the tool just crashed. The fix degrades to before-Stage-2
    behaviour: nothing preserved, a clean rewrite, a warning in the response."""
    await _seed("wiki/bad-colon.md")
    note = tmp_vault_with_taxonomy / "wiki" / "bad-colon.md"
    note.write_text(
        note.read_text(encoding="utf-8").replace(
            "gist: seeded note", "gist: fix: colon"
        ),
        encoding="utf-8",
    )
    text = await _call("brain_write", {
        "path": "wiki/bad-colon.md", "title": "Fixed", "body": "New body",
        "note_type": "wiki", "scope": "global", "gist": "rewritten clean",
    })
    assert text.startswith("Saved:"), text
    assert "unparsable" in text
    meta = _meta(tmp_vault_with_taxonomy, "wiki/bad-colon.md")
    assert meta["title"] == "Fixed"
    assert meta["gist"] == "rewritten clean"


async def test_rewrite_over_unclosed_flow_sequence_degrades_instead_of_crashing(
    initialized_server, tmp_vault_with_taxonomy: Path, stub_provenance,
):
    """Same defect, second yaml.YAMLError shape: an unclosed `tags: [a, b`."""
    await _seed("wiki/bad-brackets.md")
    note = tmp_vault_with_taxonomy / "wiki" / "bad-brackets.md"
    note.write_text(
        note.read_text(encoding="utf-8").replace(
            "gist: seeded note", "gist: seeded note\ntags: [a, b"
        ),
        encoding="utf-8",
    )
    text = await _call("brain_write", {
        "path": "wiki/bad-brackets.md", "title": "Fixed", "body": "New body",
        "note_type": "wiki", "scope": "global", "gist": "rewritten clean",
    })
    assert text.startswith("Saved:"), text
    assert "unparsable" in text

# NOTE (U1, found during final review): a note whose frontmatter already has a
# literal `content:` or `handler:` key makes `frontmatter.loads` itself raise
# `TypeError` — `Post.__init__()` ends in `Post(content, handler, **metadata)`,
# so either metadata key collides with a positional parameter. That is a
# distinct exception shape from the YAML-level errors A1 handles above
# (yaml.YAMLError / ValueError), and it reached brain_write specifically
# because this branch made brain_write read the note's existing frontmatter
# before rewriting it (CP-4) — a regression of this branch, not a pre-existing
# one, unlike the same call shared by sync.py's parse_note, brain_append and
# brain_patch (still open there, out of scope here: none of those three read
# the existing frontmatter as part of *this* branch's work). Fixed the same way
# as A1: caught and degraded to "not preserved" rather than raised.
async def test_rewrite_over_content_key_collision_degrades_instead_of_crashing(
    initialized_server, tmp_vault_with_taxonomy: Path, stub_provenance,
):
    """U1: a hand-added `content:` key in frontmatter collides with
    `Post.__init__()`'s positional `content` parameter and raises `TypeError`,
    not a `yaml.YAMLError` — a different exception shape than A1's guard caught."""
    await _seed("wiki/bad-content-key.md")
    note = tmp_vault_with_taxonomy / "wiki" / "bad-content-key.md"
    note.write_text(
        note.read_text(encoding="utf-8").replace(
            "gist: seeded note", "gist: seeded note\ncontent: y"
        ),
        encoding="utf-8",
    )
    text = await _call("brain_write", {
        "path": "wiki/bad-content-key.md", "title": "Fixed", "body": "New body",
        "note_type": "wiki", "scope": "global", "gist": "rewritten clean",
    })
    assert text.startswith("Saved:"), text
    assert "unparsable" in text
    meta = _meta(tmp_vault_with_taxonomy, "wiki/bad-content-key.md")
    assert meta["title"] == "Fixed"
    assert meta["gist"] == "rewritten clean"
    assert "content" not in meta


async def test_rewrite_over_handler_key_collision_degrades_instead_of_crashing(
    initialized_server, tmp_vault_with_taxonomy: Path, stub_provenance,
):
    """U1, second collision: `handler:` is `Post.__init__()`'s other positional
    parameter."""
    await _seed("wiki/bad-handler-key.md")
    note = tmp_vault_with_taxonomy / "wiki" / "bad-handler-key.md"
    note.write_text(
        note.read_text(encoding="utf-8").replace(
            "gist: seeded note", "gist: seeded note\nhandler: y"
        ),
        encoding="utf-8",
    )
    text = await _call("brain_write", {
        "path": "wiki/bad-handler-key.md", "title": "Fixed", "body": "New body",
        "note_type": "wiki", "scope": "global", "gist": "rewritten clean",
    })
    assert text.startswith("Saved:"), text
    assert "unparsable" in text
    meta = _meta(tmp_vault_with_taxonomy, "wiki/bad-handler-key.md")
    assert meta["title"] == "Fixed"
    assert meta["gist"] == "rewritten clean"
    assert "handler" not in meta
