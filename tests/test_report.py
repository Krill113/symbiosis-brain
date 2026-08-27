"""CP-6 — отчёт «горячие / мёртвые» (I-26 … I-30, спека §6).

Фикстуры — только синтетика: выдуманные пути нот, выдуманные скоупы, никаких путей
машины и имён людей (CLAUDE.md проекта, §11.2 спеки). Журнал наполняется прямым
INSERT'ом по DDL CP-2 (I-1, I-2) — тестам не нужен ни живой serve, ни хук-процесс.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _ts(days_ago: float = 0.0) -> str:
    """ISO-8601 UTC — тот же формат, что пишет Storage._now (storage.py:197-198)."""
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()


def _note(storage, path, *, title=None, scope="global", note_type="wiki",
          created_days_ago=200.0, updated_days_ago=None, content="Body"):
    """Нота с УПРАВЛЯЕМЫМ возрастом: upsert_note всегда штампует now
    (storage.py:205), поэтому даты правим прямым UPDATE."""
    storage.upsert_note(path=path, title=title or path, content=content,
                        note_type=note_type, scope=scope, tags=[], frontmatter={})
    updated = created_days_ago if updated_days_ago is None else updated_days_ago
    storage._conn.execute(
        "UPDATE notes SET created_at=?, updated_at=? WHERE path=?",
        (_ts(created_days_ago), _ts(updated), path),
    )
    storage._conn.commit()


def _event(storage, *, source, days_ago=1.0, hits=(), origin="unknown", query="q",
           scope=None, mode=None, fts_mode=None, e2e_ms=None, latency_ms=5,
           session_id=None, tool=None, client=None, dedup_dropped=0, vec_enabled=0):
    """Одно событие журнала + его хиты (DDL §2.3, I-1/I-2)."""
    cur = storage._conn.execute(
        "INSERT INTO retrieval_event (ts, session_id, origin, source, tool, query, scope,"
        " mode, fts_mode, n_returned, dedup_dropped, vec_enabled, latency_ms, e2e_ms, client)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (_ts(days_ago), session_id, origin, source, tool, query, scope, mode, fts_mode,
         len(hits), dedup_dropped, vec_enabled, latency_ms, e2e_ms, client),
    )
    event_id = cur.lastrowid
    for rank, path in enumerate(hits):
        storage._conn.execute(
            "INSERT INTO retrieval_hit (event_id, rank, note_path, score, in_both)"
            " VALUES (?,?,?,?,?)",
            (event_id, rank, path, 0.5, 0),
        )
    storage._conn.commit()
    return event_id


@pytest.fixture
def vault_db(tmp_vault_with_taxonomy, db_path):
    """Vault + Storage. Storage закрывается в конце — иначе на Windows файл БД
    остаётся занятым и tmp_path не убирается."""
    from symbiosis_brain.storage import Storage

    storage = Storage(db_path)
    yield tmp_vault_with_taxonomy, storage
    storage.close()


def _seed_busy(storage, *, notes=32, events=60, span_days=10.0):
    """Не-нулевой день: >= ARCHIVE_MIN_NOTES нот, >= 50 событий, журнал старше 7 дней."""
    for i in range(notes):
        _note(storage, f"wiki/topic-{i:02d}.md", title=f"Topic {i:02d}")
    for i in range(events):
        _event(storage, source="hook_pre_action",
               days_ago=span_days * (i + 1) / (events + 1),
               hits=[f"wiki/topic-{i % 3:02d}.md"])
    _event(storage, source="mcp_search", days_ago=span_days, hits=["wiki/topic-00.md"])
    return storage


# ---------------------------------------------------------------------------
# Task 6.1 — build_report
# ---------------------------------------------------------------------------

def test_build_report_has_exactly_the_contract_keys(vault_db):
    """I-26: восемь ключей, не семь и не девять."""
    from symbiosis_brain import report

    vault, storage = vault_db
    _note(storage, "wiki/alpha.md")
    data = report.build_report(storage, vault)
    assert set(data) == {
        "summary", "hot", "archive_candidates", "surfaced_not_read",
        "by_scope", "by_type", "routing_health", "coverage",
    }


def test_hot_is_ranked_by_mcp_search_plus_read_then_path(vault_db):
    """I-26: ключ сортировки — mcp_search + mcp_read, при равенстве путь по алфавиту.
    Хуковые выдачи в ранг НЕ входят (§6.2 п. 2: иначе топ заполнит автоинжект)."""
    from symbiosis_brain import report

    vault, storage = vault_db
    for p in ("wiki/aaa.md", "wiki/bbb.md", "wiki/ccc.md"):
        _note(storage, p)
    # ccc: 1 mcp_search + 1 mcp_read = 2
    _event(storage, source="mcp_search", hits=["wiki/ccc.md"])
    _event(storage, source="mcp_read", mode="read", hits=["wiki/ccc.md"])
    # bbb: 2 mcp_search = 2  → ничья с ccc, решает алфавит
    _event(storage, source="mcp_search", hits=["wiki/bbb.md"])
    _event(storage, source="mcp_search", hits=["wiki/bbb.md"])
    # aaa: 20 хуковых выдач и ни одной осознанной = 0
    for _ in range(20):
        _event(storage, source="hook_prompt", hits=["wiki/aaa.md"])

    data = report.build_report(storage, vault)
    assert [r["path"] for r in data["hot"]] == ["wiki/bbb.md", "wiki/ccc.md"]
    assert [r["rank_key"] for r in data["hot"]] == [2, 2]
    assert "wiki/aaa.md" not in {r["path"] for r in data["hot"]}


def test_hot_row_splits_hook_and_mcp_counters(vault_db):
    """§6.2 п. 2: «выдана» разделена на hook и mcp; legacy_gist считается хуковой (Р6.3)."""
    from symbiosis_brain import report

    vault, storage = vault_db
    _note(storage, "wiki/alpha.md")
    _event(storage, source="hook_pre_action", hits=["wiki/alpha.md"])
    _event(storage, source="legacy_gist", hits=["wiki/alpha.md"])
    _event(storage, source="mcp_search", hits=["wiki/alpha.md"])
    _event(storage, source="mcp_read", mode="read", hits=["wiki/alpha.md"])

    row = report.build_report(storage, vault)["hot"][0]
    assert (row["hook"], row["mcp"], row["read"]) == (2, 1, 1)


def test_surfaced_not_read_needs_three_surfacings_and_zero_reads(vault_db):
    """§6.2 п. 4 + I-26: >= 3 выдач и 0 чтений; сортировка — выдач убыв., затем путь."""
    from symbiosis_brain import report

    vault, storage = vault_db
    for p in ("wiki/many.md", "wiki/mid.md", "wiki/few.md", "wiki/read.md"):
        _note(storage, p)
    for _ in range(5):
        _event(storage, source="hook_prompt", hits=["wiki/many.md"])
    for _ in range(3):
        _event(storage, source="mcp_search", hits=["wiki/mid.md"])
    for _ in range(2):                       # ниже порога
        _event(storage, source="hook_prompt", hits=["wiki/few.md"])
    for _ in range(4):                       # выдавали, но и читали
        _event(storage, source="hook_prompt", hits=["wiki/read.md"])
    _event(storage, source="mcp_read", mode="read", hits=["wiki/read.md"])

    rows = report.build_report(storage, vault)["surfaced_not_read"]
    assert [r["path"] for r in rows] == ["wiki/many.md", "wiki/mid.md"]
    assert rows[0]["surfaced"] == 5


def test_archive_candidates_are_orphans_sorted_by_last_edit_age(vault_db):
    """§6.2 п. 3 + I-26: сирота ∧ (ни разу не правилась | не правилась > 90 дней),
    сортировка — возраст последней правки убыв., при равенстве путь по алфавиту."""
    from symbiosis_brain import report

    vault, storage = vault_db
    for i in range(30):                      # порог ARCHIVE_MIN_NOTES, Task 6.3
        _note(storage, f"wiki/filler-{i:02d}.md", created_days_ago=200.0,
              updated_days_ago=1.0)          # свежая правка → не кандидат
    _note(storage, "wiki/never.md", created_days_ago=120.0)          # created == updated
    _note(storage, "wiki/stale.md", created_days_ago=400.0, updated_days_ago=300.0)
    _note(storage, "wiki/linked.md", created_days_ago=400.0, updated_days_ago=300.0,
          content="")
    # linked.md перестаёт быть сиротой: на неё ссылается filler-00
    storage.upsert_relation(from_name="wiki/filler-00", to_name="wiki/linked",
                            relation_type="references", source_note="wiki/filler-00.md")

    rows = report.build_report(storage, vault)["archive_candidates"]
    paths = [r["path"] for r in rows]
    assert paths[:2] == ["wiki/stale.md", "wiki/never.md"], paths
    assert "wiki/linked.md" not in paths
    assert "stale" in rows[0]["reasons"]
    assert "never_edited" in rows[1]["reasons"]


def test_excluded_paths_never_show_up_anywhere(vault_db):
    """I-30 + §6.4: CRITICAL_FACTS.md вкладывается bash-ом мимо БД, MEMORY.md и
    reference/scope-taxonomy.md — служебные."""
    from symbiosis_brain import report

    vault, storage = vault_db
    assert report.EXCLUDED_PATHS == frozenset(
        {"CRITICAL_FACTS.md", "MEMORY.md", "reference/scope-taxonomy.md"})
    for p in report.EXCLUDED_PATHS:
        _note(storage, p, created_days_ago=400.0)
        for _ in range(5):
            _event(storage, source="mcp_search", hits=[p])
    _note(storage, "wiki/alpha.md", created_days_ago=400.0)

    data = report.build_report(storage, vault)
    seen = {r["path"] for r in data["hot"]} | {r["path"] for r in data["archive_candidates"]}
    seen |= {r["path"] for r in data["surfaced_not_read"]}
    assert seen & report.EXCLUDED_PATHS == set()
    assert data["summary"]["total_notes"] == 1


def test_scope_filter_limits_both_notes_and_hits(vault_db):
    """Р6.6: строгий фильтр по одному скоупу, без подмешивания global."""
    from symbiosis_brain import report

    vault, storage = vault_db
    _note(storage, "wiki/mine.md", scope="testproj", created_days_ago=400.0)
    _note(storage, "wiki/theirs.md", scope="global", created_days_ago=400.0)
    for _ in range(3):
        _event(storage, source="mcp_search", hits=["wiki/mine.md", "wiki/theirs.md"])

    data = report.build_report(storage, vault, scope="testproj")
    assert data["summary"]["total_notes"] == 1
    assert {r["path"] for r in data["hot"]} == {"wiki/mine.md"}
    assert data["by_scope"] == {"testproj": 1}


def test_window_days_cuts_old_events_but_not_the_log_age(vault_db):
    """Окно режет события; возраст журнала считается по САМОЙ СТАРОЙ строке (§6.2 шапка)."""
    from symbiosis_brain import report

    vault, storage = vault_db
    _note(storage, "wiki/alpha.md", created_days_ago=400.0)
    _event(storage, source="mcp_search", days_ago=100.0, hits=["wiki/alpha.md"])
    _event(storage, source="mcp_search", days_ago=1.0, hits=["wiki/alpha.md"])

    data = report.build_report(storage, vault, days=30)
    assert data["coverage"]["events"] == 1
    assert data["summary"]["log_days"] >= 99


def test_coverage_carries_two_skip_counters_and_no_single_key(vault_db):
    """I-26: skipped_total (персистентный) и skipped_process (этого процесса);
    одиночный ключ `skipped` запрещён."""
    from symbiosis_brain import report, retrieval_log

    vault, storage = vault_db
    storage.set_schema_version(retrieval_log.SKIPPED_TOTAL_KEY, 4)
    cov = report.build_report(storage, vault)["coverage"]
    assert cov["skipped_total"] == 4
    assert cov["skipped_process"] == retrieval_log.skipped_count()
    assert "skipped" not in cov
    assert set(cov) >= {"events", "skipped_total", "skipped_process", "oldest_ts"}


def test_missing_skipped_key_reads_as_zero(vault_db):
    """I-26: get_schema_version вернул None → 0, а не падение и не None в JSON."""
    from symbiosis_brain import report

    vault, storage = vault_db
    assert report.build_report(storage, vault)["coverage"]["skipped_total"] == 0


def test_cuts_are_counts_sorted_desc_then_name(vault_db):
    """§6.2 п. 5: разрезы — только числа, порядок детерминирован."""
    from symbiosis_brain import report

    vault, storage = vault_db
    _note(storage, "wiki/a.md", scope="beta", note_type="wiki")
    _note(storage, "wiki/b.md", scope="beta", note_type="wiki")
    _note(storage, "decisions/c.md", scope="alpha", note_type="decision")

    data = report.build_report(storage, vault)
    assert list(data["by_scope"].items()) == [("beta", 2), ("alpha", 1)]
    assert list(data["by_type"].items()) == [("wiki", 2), ("decision", 1)]


def test_top_caps_rows_and_totals_keep_the_full_length(vault_db):
    """Р6.2: build капает строки, непокапанные длины остаются в summary['totals']."""
    from symbiosis_brain import report

    vault, storage = vault_db
    for i in range(12):
        _note(storage, f"wiki/n-{i:02d}.md")
        for _ in range(3):
            _event(storage, source="mcp_search", hits=[f"wiki/n-{i:02d}.md"])

    data = report.build_report(storage, vault, top=5)
    assert len(data["hot"]) == 5
    assert data["summary"]["totals"]["hot"] == 12
    full = report.build_report(storage, vault, top=None)
    assert len(full["hot"]) == 12


# ---------------------------------------------------------------------------
# Task 6.2 — render_report
# ---------------------------------------------------------------------------

def test_render_header_carries_notes_window_events_and_log_age(vault_db):
    """§6.2 шапка: нот, окно, события (всего + по source + по origin), возраст журнала."""
    from symbiosis_brain import report

    vault, storage = vault_db
    _seed_busy(storage)
    text = report.render_report(report.build_report(storage, vault))
    assert "нот: 32" in text
    assert "окно: 30 дн." in text
    assert "событий: 61" in text
    assert "hook_pre_action 60" in text
    assert "origin:" in text


def test_render_hot_header_names_its_sort_key(vault_db):
    """§6.2 п. 2: «Ключ сортировки печатается в заголовке секции, чтобы его нельзя
    было прочитать неправильно»."""
    from symbiosis_brain import report

    vault, storage = vault_db
    _seed_busy(storage)
    text = report.render_report(report.build_report(storage, vault))
    hot_header = next(l for l in text.splitlines() if l.startswith("## Горячие"))
    assert "mcp_search + mcp_read" in hot_header


def test_render_hot_row_shows_all_three_counters(vault_db):
    """§6.2 п. 2: колонки выдана(hook) | выдана(mcp) | прочитана — все три видны."""
    from symbiosis_brain import report

    vault, storage = vault_db
    _seed_busy(storage)
    _event(storage, source="mcp_read", mode="read", hits=["wiki/topic-00.md"])
    text = report.render_report(report.build_report(storage, vault))
    row = next(l for l in text.splitlines() if "wiki/topic-00.md" in l)
    assert "hook" in row and "mcp" in row and "прочитана" in row


def test_render_default_never_exceeds_forty_lines(vault_db):
    """§6.2 п. 7: потолок 40 строк — требование, а не пожелание (verbose линтера
    уже не влезает в лимит ответа тула)."""
    from symbiosis_brain import report

    vault, storage = vault_db
    _seed_busy(storage, notes=60)
    for i in range(60):
        for _ in range(4):
            _event(storage, source="mcp_search", hits=[f"wiki/topic-{i:02d}.md"])
    text = report.render_report(report.build_report(storage, vault, top=10))
    assert len(text.splitlines()) <= 40, text


def test_render_full_removes_the_cap(vault_db):
    """I-27 + I-28: --full / full=true снимает и потолок строк, и капы секций."""
    from symbiosis_brain import report

    vault, storage = vault_db
    _seed_busy(storage, notes=60)
    for i in range(60):
        for _ in range(4):
            _event(storage, source="mcp_search", hits=[f"wiki/topic-{i:02d}.md"])
    data = report.build_report(storage, vault, top=None)
    text = report.render_report(data, full=True)
    assert len(text.splitlines()) > 40
    assert "wiki/topic-59.md" in text


def test_render_tail_says_how_many_of_how_many(vault_db):
    """§6.2 п. 7: хвост «показано 10 из 71 — --full / full=true»."""
    from symbiosis_brain import report

    vault, storage = vault_db
    _seed_busy(storage, notes=40)
    for i in range(40):
        for _ in range(4):
            _event(storage, source="mcp_search", hits=[f"wiki/topic-{i:02d}.md"])
    text = report.render_report(report.build_report(storage, vault, top=3))
    assert "показано 3 из 40 — --full / full=true" in text


def test_render_is_deterministic(vault_db):
    """Р6.1: ни одной временной метки — две сборки подряд дают идентичный текст,
    иначе тест «тул и CLI печатают одно и то же» (§9 CP-6) мигает."""
    from symbiosis_brain import report

    vault, storage = vault_db
    _seed_busy(storage)
    first = report.render_report(report.build_report(storage, vault))
    second = report.render_report(report.build_report(storage, vault))
    assert first == second


def test_render_survives_a_hit_on_a_deleted_note(vault_db):
    """Р6.7: у retrieval_hit.note_path нет FK (§2.3) — путь без ноты печатается
    как есть и ничего не роняет."""
    from symbiosis_brain import report

    vault, storage = vault_db
    _seed_busy(storage)
    for _ in range(3):
        _event(storage, source="mcp_search", hits=["wiki/vanished.md"])
    text = report.render_report(report.build_report(storage, vault))
    assert "wiki/vanished.md" in text


# ---------------------------------------------------------------------------
# Task 6.3 — нулевой день, порог архива, возрастной фильтр
# ---------------------------------------------------------------------------

def test_zero_day_hides_hot_and_surfaced_and_prints_the_honest_line(vault_db):
    """§6.3: журнал младше 7 дней / < 50 событий → секций 2 и 4 нет, вместо них
    одна честная строка."""
    from symbiosis_brain import report

    vault, storage = vault_db
    for i in range(35):
        _note(storage, f"wiki/topic-{i:02d}.md", created_days_ago=200.0,
              updated_days_ago=1.0)
    for _ in range(37):
        _event(storage, source="mcp_search", days_ago=1.0, hits=["wiki/topic-00.md"])

    data = report.build_report(storage, vault)
    assert data["summary"]["zero_day"] is True
    text = report.render_report(data)
    assert "## Горячие" not in text
    assert "## Выданы, но не прочитаны" not in text
    assert "Журнал выдачи накапливается: 37 событий за 1 день." in text
    assert "появятся после 7 дней наблюдения" in text
    # секции 3 и 5 журнала не требуют и печатаются с первого дня
    assert "## Кандидаты в архив" in text
    assert "## Разрезы" in text


def test_busy_log_is_not_zero_day(vault_db):
    """Контроль обратной стороны: ≥ 50 событий и журнал старше 7 дней → секции есть."""
    from symbiosis_brain import report

    vault, storage = vault_db
    _seed_busy(storage)
    data = report.build_report(storage, vault)
    assert data["summary"]["zero_day"] is False
    assert "## Горячие" in report.render_report(data)


def test_archive_section_is_silent_until_thirty_notes(vault_db):
    """§6.3: секция 3 не печатается, пока нот меньше ARCHIVE_MIN_NOTES = 30."""
    from symbiosis_brain import report

    vault, storage = vault_db
    for i in range(5):
        _note(storage, f"wiki/fresh-{i}.md", created_days_ago=400.0)
    data = report.build_report(storage, vault)
    assert data["summary"]["archive_suppressed"] is True
    text = report.render_report(data)
    assert "кандидатов в архив ещё не считаем: нот в памяти 5" in text
    assert "## Кандидаты в архив" not in text


def test_note_written_yesterday_is_not_an_archive_candidate(vault_db):
    """§6.3, возрастной фильтр: «ни разу не правилась» засчитывается только для нот
    старше 30 дней. Нота, написанная вчера, — свежая, а не мёртвая."""
    from symbiosis_brain import report

    vault, storage = vault_db
    for i in range(30):
        _note(storage, f"wiki/filler-{i:02d}.md", created_days_ago=200.0,
              updated_days_ago=1.0)
    _note(storage, "wiki/yesterday.md", created_days_ago=1.0)
    _note(storage, "wiki/old.md", created_days_ago=45.0)

    rows = report.build_report(storage, vault)["archive_candidates"]
    paths = {r["path"] for r in rows}
    assert "wiki/yesterday.md" not in paths
    assert "wiki/old.md" in paths


def test_stale_bucket_needs_no_age_filter(vault_db):
    """§6.3: «не правилась > 90 дней» возрастного фильтра не требует — он в нём уже есть."""
    from symbiosis_brain import report

    vault, storage = vault_db
    for i in range(30):
        _note(storage, f"wiki/filler-{i:02d}.md", created_days_ago=200.0,
              updated_days_ago=1.0)
    _note(storage, "wiki/edited-long-ago.md", created_days_ago=400.0,
          updated_days_ago=120.0)
    rows = report.build_report(storage, vault)["archive_candidates"]
    row = next(r for r in rows if r["path"] == "wiki/edited-long-ago.md")
    assert row["reasons"] == ["stale"]


# ---------------------------------------------------------------------------
# Task 6.4 — строка неполноты журнала
# ---------------------------------------------------------------------------

def test_incompleteness_line_is_verbatim_and_comes_from_the_persistent_counter(vault_db):
    """§6.2 п. 1: формулировка дословная, источник — schema_version, а не
    skipped_count() текущего процесса (в brain-cli report он структурно всегда 0)."""
    from symbiosis_brain import report, retrieval_log

    vault, storage = vault_db
    _seed_busy(storage)
    storage.set_schema_version(retrieval_log.SKIPPED_TOTAL_KEY, 7)
    text = report.render_report(report.build_report(storage, vault))
    assert ("журнал неполон: за всё время пропущено 7 событий "
            "(счётчик накопительный, не за окно; нижняя оценка)") in text


def test_incompleteness_line_absent_when_nothing_was_skipped(vault_db):
    """§6.2 п. 1: «При N == 0 строка не печатается»."""
    from symbiosis_brain import report

    vault, storage = vault_db
    _seed_busy(storage)
    text = report.render_report(report.build_report(storage, vault))
    assert "журнал неполон" not in text


def test_report_never_promises_a_complete_log(vault_db):
    """§6.2 п. 1: «Обещания „журнал за окно полон“ отчёт не даёт нигде»."""
    from symbiosis_brain import report

    vault, storage = vault_db
    _seed_busy(storage)
    text = report.render_report(report.build_report(storage, vault))
    assert "полон" not in text


# ---------------------------------------------------------------------------
# Task 6.6 — MCP-тул brain_report (I-29)
# ---------------------------------------------------------------------------

def _wire_server(vault, storage):
    """Минимальная проводка сервера: отчёту нужны только _storage и _vault_path."""
    import asyncio as _asyncio

    from symbiosis_brain import server

    server._storage = storage
    server._vault_path = vault
    server._ready = _asyncio.Event()
    server._ready.set()
    return server


def test_brain_report_tool_is_declared_verbatim():
    """I-29: схема и описание — дословно."""
    import asyncio

    from symbiosis_brain import server

    tool = next(t for t in asyncio.run(server.list_tools()) if t.name == "brain_report")
    assert tool.description == (
        "Vault health report: what memory actually surfaced and read, "
        "and what looks dead (archive candidates)."
    )
    assert tool.inputSchema == {
        "type": "object",
        "properties": {
            "window_days": {"type": "integer", "default": 30,
                            "description": "Analysis window in days."},
            "scope": {"type": "string", "description": "Limit to one project scope."},
            "top": {"type": "integer", "default": 10, "description": "Rows per section."},
            "full": {"type": "boolean", "default": False,
                     "description": "Print full lists instead of the capped default."},
        },
    }


def test_brain_report_tool_is_in_the_allowlist():
    """I-29: без имени в SB_PERMISSIONS тул бесполезен."""
    from symbiosis_brain import install_cli

    assert "mcp__symbiosis-brain__brain_report" in install_cli.SB_PERMISSIONS


@pytest.mark.asyncio
async def test_brain_report_tool_renders_the_report(vault_db):
    from symbiosis_brain import report

    vault, storage = vault_db
    _seed_busy(storage)
    server = _wire_server(vault, storage)

    result = await server.call_tool("brain_report", {})
    text = result[0].text
    assert text == report.render_report(report.build_report(storage, vault, top=10))
    assert len(text.splitlines()) <= 40


@pytest.mark.asyncio
async def test_brain_report_tool_full_removes_the_cap(vault_db):
    """I-29 + Р6.2: full=true снимает и потолок строк, и капы секций."""
    vault, storage = vault_db
    _seed_busy(storage, notes=60)
    for i in range(60):
        for _ in range(4):
            _event(storage, source="mcp_search", hits=[f"wiki/topic-{i:02d}.md"])
    server = _wire_server(vault, storage)

    capped = (await server.call_tool("brain_report", {}))[0].text
    full = (await server.call_tool("brain_report", {"full": True}))[0].text
    assert len(capped.splitlines()) <= 40
    assert len(full.splitlines()) > len(capped.splitlines())


@pytest.mark.asyncio
async def test_brain_report_tool_honours_window_and_scope(vault_db):
    vault, storage = vault_db
    _note(storage, "wiki/mine.md", scope="testproj", created_days_ago=400.0)
    _note(storage, "wiki/theirs.md", scope="global", created_days_ago=400.0)
    server = _wire_server(vault, storage)

    text = (await server.call_tool("brain_report", {"window_days": 7,
                                                    "scope": "testproj"}))[0].text
    assert "окно: 7 дн." in text
    assert "scope: testproj" in text
    assert "нот: 1" in text


# ---------------------------------------------------------------------------
# Task 6.8 — здоровье роутинга (Р3: ключа prompt_route_warnings ещё нет)
# ---------------------------------------------------------------------------

def _write_meta(vault, payload):
    import json as _json

    (vault / ".index").mkdir(parents=True, exist_ok=True)
    (vault / ".index" / "action-rules.meta.json").write_text(
        _json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def test_routing_health_without_prompt_route_warnings_key_reads_as_zero(vault_db):
    """Р3 каркаса: CP-6 идёт ДО CP-7, ключа в meta ещё нет — это ноль
    предупреждений, а не падение и не исчезнувшая секция."""
    from symbiosis_brain import report

    vault, storage = vault_db
    _write_meta(vault, {"compiled_at": "2026-08-26T00:00:00+00:00",
                        "rules_total": 12, "rules_compiled": 11,
                        "skipped": [{"id": "r1", "reason": "no id"}],
                        "unmatched_patterns": [{"id": "r2", "tool": "bash", "re": "x"}]})
    rh = report.build_report(storage, vault)["routing_health"]
    assert rh["prompt_route_warnings"] == 0
    assert (rh["rules_total"], rh["rules_compiled"], rh["skipped"],
            rh["unmatched_patterns"]) == (12, 11, 1, 1)
    text = report.render_report(report.build_report(storage, vault))
    assert "## Здоровье роутинга" in text
    assert "скомпилировано 11 из 12, отброшено 1, мёртвых паттернов 1" in text
    assert "предупреждений промпт-роутов 0" in text


def test_routing_health_counts_prompt_route_warnings_when_cp7_adds_them(vault_db):
    """Форвард-совместимость с I-31: появившийся ключ считается, отчёт не правится."""
    from symbiosis_brain import report

    vault, storage = vault_db
    _write_meta(vault, {"rules_total": 3, "rules_compiled": 3, "skipped": [],
                        "unmatched_patterns": [],
                        "prompt_route_warnings": [{"id": "r9", "reason": "bad regex: x"}]})
    rh = report.build_report(storage, vault)["routing_health"]
    assert rh["prompt_route_warnings"] == 1


def test_routing_health_survives_a_missing_or_corrupt_meta(vault_db):
    """Секция не должна убивать отчёт: meta пишется best-effort (action_rules.py:460-463)."""
    from symbiosis_brain import report

    vault, storage = vault_db
    rh = report.build_report(storage, vault)["routing_health"]
    assert rh["meta_present"] is False
    assert rh["prompt_route_warnings"] == 0
    assert "не найден" in report.render_report(report.build_report(storage, vault))

    (vault / ".index").mkdir(parents=True, exist_ok=True)
    (vault / ".index" / "action-rules.meta.json").write_text("{not json",
                                                             encoding="utf-8")
    assert report.build_report(storage, vault)["routing_health"]["meta_present"] is False


# ---------------------------------------------------------------------------
# Task 6.9 — одна поверхность, один текст (решение Q4)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_tool_and_cli_print_the_same_text(vault_db):
    """Q4: CLI и тул — две обёртки над ОДНИМ рендерером. Разошедшийся текст здесь
    означает вторую копию рендерера или временную метку внутри (Р6.1)."""
    import subprocess
    import sys

    from symbiosis_brain import report
    from symbiosis_brain.storage import Storage

    vault, storage = vault_db
    _seed_busy(storage)
    db_file = storage.db_path
    storage.close()                      # Windows: отдаём файл дочернему процессу

    res = subprocess.run(
        [sys.executable, "-m", "symbiosis_brain.scope_cli", "report",
         "--vault", str(vault)],
        capture_output=True, text=True, encoding="utf-8",
    )
    assert res.returncode == 0, res.stderr

    reopened = Storage(db_file)
    try:
        server = _wire_server(vault, reopened)
        tool_text = (await server.call_tool("brain_report", {}))[0].text
    finally:
        reopened.close()

    assert res.stdout.replace("\r\n", "\n").strip("\n") == tool_text
    assert tool_text == report.render_report(
        report.build_report(Storage(db_file), vault, top=10))
