"""Vault health report (Stage 2, §6): what memory actually surfaced and read,
and what looks dead.

Architecture mirrors the linter: build_report() computes EVERYTHING and
render_report() decides what gets printed (lint.py:52 vs
server._render_lint_report, server.py:208-288). Two surfaces share both —
`brain-cli report` (I-28) and the MCP tool `brain_report` (I-29) — so the text
must be a pure function of the data: no timestamps, no locale-dependent
formatting, no iteration order that depends on insertion luck. The "tool and CLI
print the same text" test (§9, CP-6) is what keeps that honest.

Import-light on purpose: `brain-cli` imports this module inside one branch
(I-28), so nothing here may pull numpy the way search.py:13 does.
"""
from __future__ import annotations

import json
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path

from symbiosis_brain import retrieval_log
from symbiosis_brain.action_rules import META_BASENAME
from symbiosis_brain.resolver import compute_linked_canonicals

# I-30 / §6.4. CRITICAL_FACTS.md is injected by bash straight into the prompt
# (hooks/brain-session-start.sh:71-73), so the log never sees it and it would
# look dead while being read every single session; MEMORY.md and the taxonomy are
# housekeeping files (already excluded in lint.py:16,79-80 and storage.py:296-297).
# log.md is not indexed at all (sync.py:38) and therefore needs no entry here.
EXCLUDED_PATHS = frozenset({"CRITICAL_FACTS.md", "MEMORY.md", "reference/scope-taxonomy.md"})

# §6.3 — thresholds that keep the first run of a fresh install honest.
ARCHIVE_MIN_NOTES = 30
ARCHIVE_MIN_AGE_DAYS = 30
ARCHIVE_STALE_DAYS = 90
ZERO_DAY_MIN_DAYS = 7
ZERO_DAY_MIN_EVENTS = 50
SURFACED_NOT_READ_MIN = 3
DEFAULT_MAX_LINES = 40

# §2.1: three CLI paths write from the hook process; mcp_search / mcp_read are the
# deliberate calls the "hot" ranking is built on.
HOOK_SOURCES = ("hook_prompt", "hook_pre_action", "legacy_gist")
SEARCH_SOURCE = "mcp_search"
READ_SOURCE = "mcp_read"


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------

def _parse_ts(value) -> datetime | None:
    """ISO-8601 → aware datetime, or None. Never raises: a hand-edited note can
    carry anything, and one bad row must not take the report down."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _age_days(value, now: datetime) -> int | None:
    dt = _parse_ts(value)
    if dt is None:
        return None
    return max(0, (now - dt).days)


def _percentile(sorted_values: list[int], q: float) -> int | None:
    """Nearest-rank percentile on an ALREADY SORTED list — deterministic by
    construction, no interpolation and no numpy."""
    if not sorted_values:
        return None
    idx = max(0, math.ceil(q * len(sorted_values)) - 1)
    return int(sorted_values[min(idx, len(sorted_values) - 1)])


def _sorted_counts(counts: dict[str, int]) -> dict[str, int]:
    """Count desc, then name asc. Dict order is part of the rendered text."""
    return dict(sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])))


def _cap(rows: list, top: int | None) -> list:
    if top is None:
        return list(rows)
    return list(rows[:max(0, int(top))])


def _cap_counts(counts: dict[str, int], top: int | None) -> dict[str, int]:
    if top is None:
        return dict(counts)
    return dict(list(counts.items())[:max(0, int(top))])


# --------------------------------------------------------------------------
# data access
# --------------------------------------------------------------------------

def _load_notes(storage, scope: str | None) -> list[dict]:
    """Notes the report is about. `storage._conn` is the house convention for
    read-only SQL outside Storage (search.py:417, sync.py:101, server.py:795)."""
    sql = "SELECT path, title, note_type, scope, created_at, updated_at FROM notes"
    params: list = []
    if scope:
        # Strict single scope (Р6.6): list_notes would mix in 'global'
        # (storage.py:251-259), and I-29 says "limit to ONE project scope".
        sql += " WHERE scope = ?"
        params.append(scope)
    rows = storage._conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows if r["path"] not in EXCLUDED_PATHS]


def _hit_counters(storage, cutoff_iso: str) -> dict[str, dict[str, int]]:
    """Per-note surfacing counters inside the window.

    `mcp_read` writes exactly one hit row per read (I-4), `mcp_context` writes
    none, so this one GROUP BY gives all three counters at once.
    """
    rows = storage._conn.execute(
        """
        SELECT h.note_path AS path,
               SUM(CASE WHEN e.source IN (?, ?, ?) THEN 1 ELSE 0 END) AS hook_n,
               SUM(CASE WHEN e.source = ? THEN 1 ELSE 0 END) AS mcp_n,
               SUM(CASE WHEN e.source = ? THEN 1 ELSE 0 END) AS read_n
          FROM retrieval_hit h
          JOIN retrieval_event e ON e.id = h.event_id
         WHERE e.ts >= ?
         GROUP BY h.note_path
        """,
        (*HOOK_SOURCES, SEARCH_SOURCE, READ_SOURCE, cutoff_iso),
    ).fetchall()
    return {
        r["path"]: {"hook": int(r["hook_n"]), "mcp": int(r["mcp_n"]), "read": int(r["read_n"])}
        for r in rows
        if r["path"] not in EXCLUDED_PATHS
    }


def _event_stats(storage, cutoff_iso: str):
    """(events, by_source, by_origin, sorted e2e_ms). Server paths always write
    e2e_ms NULL (§2.5), so the non-NULL rows ARE the hook paths."""
    rows = storage._conn.execute(
        "SELECT source, origin, e2e_ms FROM retrieval_event WHERE ts >= ?",
        (cutoff_iso,),
    ).fetchall()
    by_source: dict[str, int] = {}
    by_origin: dict[str, int] = {}
    e2e: list[int] = []
    for r in rows:
        by_source[r["source"]] = by_source.get(r["source"], 0) + 1
        by_origin[r["origin"]] = by_origin.get(r["origin"], 0) + 1
        if r["e2e_ms"] is not None:
            e2e.append(int(r["e2e_ms"]))
    e2e.sort()
    return len(rows), _sorted_counts(by_source), _sorted_counts(by_origin), e2e


def _oldest_ts(storage) -> str | None:
    row = storage._conn.execute("SELECT MIN(ts) AS oldest FROM retrieval_event").fetchone()
    return row["oldest"] if row is not None else None


def _skipped_total(storage) -> int:
    """§6.2 п. 1: the number in the header comes from the PERSISTENT counter —
    skipped_count() is structurally 0 in `brain-cli report` (I-26, I-28)."""
    value = storage.get_schema_version(retrieval_log.SKIPPED_TOTAL_KEY)
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _routing_health(vault_path: Path) -> dict:
    """§6.2 section 6. Р3 of 00-plan: `prompt_route_warnings` is added by CP-7 —
    until then its absence means zero warnings, never a crash and never a silently
    missing section."""
    empty = {"meta_present": False, "rules_total": 0, "rules_compiled": 0,
             "skipped": 0, "unmatched_patterns": 0, "prompt_route_warnings": 0}
    meta_path = Path(vault_path) / ".index" / META_BASENAME
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        if not isinstance(meta, dict):
            return empty
        return {
            "meta_present": True,
            "rules_total": int(meta.get("rules_total") or 0),
            "rules_compiled": int(meta.get("rules_compiled") or 0),
            "skipped": len(meta.get("skipped") or []),
            "unmatched_patterns": len(meta.get("unmatched_patterns") or []),
            "prompt_route_warnings": len(meta.get("prompt_route_warnings") or []),
        }
    except (OSError, ValueError, TypeError):
        return empty


# --------------------------------------------------------------------------
# I-26
# --------------------------------------------------------------------------

def build_report(storage, vault_path, *, days=30, scope=None, top=10) -> dict:
    """Compute the whole report (I-26).

    `top` caps the ROWS of every section; `top=None` means no cap and is what
    `--full` / `full=true` pass (Р6.2). The untruncated lengths stay in
    summary['totals'] so the renderer can print "показано 10 из 71" (§6.2 п. 7).
    """
    vault_path = Path(vault_path)
    now = datetime.now(timezone.utc)
    cutoff_iso = (now - timedelta(days=days)).isoformat()

    notes = _load_notes(storage, scope)
    notes_by_path = {n["path"]: n for n in notes}
    linked = compute_linked_canonicals(storage)

    counters = _hit_counters(storage, cutoff_iso)
    if scope:
        counters = {p: c for p, c in counters.items() if p in notes_by_path}

    events, by_source, by_origin, e2e = _event_stats(storage, cutoff_iso)
    oldest_ts = _oldest_ts(storage)
    log_days = _age_days(oldest_ts, now) or 0

    # --- section 2: hot (Р6.4 — the section is named by its own sort key) ---
    hot_all = [
        {"path": path,
         "title": (notes_by_path.get(path) or {}).get("title"),
         "hook": c["hook"], "mcp": c["mcp"], "read": c["read"],
         "rank_key": c["mcp"] + c["read"]}
        for path, c in counters.items()
        if c["mcp"] + c["read"] > 0
    ]
    hot_all.sort(key=lambda r: (-r["rank_key"], r["path"]))

    # --- section 3: archive candidates ---
    archive_all: list[dict] = []
    never_edited = 0
    stale = 0
    for note in notes:
        # Same orphan rule as the linter (lint.py:83,92-93): canonical without .md.
        if note["path"].removesuffix(".md") in linked:
            continue
        created_age = _age_days(note["created_at"], now)
        updated_age = _age_days(note["updated_at"], now)
        reasons: list[str] = []
        # Day-granularity, not raw datetime comparison: two notes stamped with the
        # same days-ago value can differ by microseconds (two separate _now()
        # calls), which would make an exact "updated <= created" comparison miss
        # a note that was in fact never edited. The report already speaks in
        # whole days everywhere else (Р6.1), so "as old or older, to the day" is
        # the right resolution for "never edited since creation".
        # §6.3, age filter: "never edited since it was created" only counts for a
        # note older than 30 days. Without it the first report of a fresh install
        # proposes to archive everything the owner wrote yesterday.
        if (created_age is not None and updated_age is not None
                and updated_age >= created_age
                and created_age >= ARCHIVE_MIN_AGE_DAYS):
            reasons.append("never_edited")
        if updated_age is not None and updated_age > ARCHIVE_STALE_DAYS:
            reasons.append("stale")
        if not reasons:
            continue
        never_edited += "never_edited" in reasons
        stale += "stale" in reasons
        archive_all.append({
            "path": note["path"],
            "title": note["title"],
            "updated_at": note["updated_at"],
            "age_days": updated_age if updated_age is not None else 0,
            "reasons": reasons,
        })
    archive_all.sort(key=lambda r: (-r["age_days"], r["path"]))

    # --- section 4: surfaced but never opened ---
    snr_all = [
        {"path": path,
         "title": (notes_by_path.get(path) or {}).get("title"),
         "surfaced": c["hook"] + c["mcp"], "hook": c["hook"], "mcp": c["mcp"]}
        for path, c in counters.items()
        if c["read"] == 0 and c["hook"] + c["mcp"] >= SURFACED_NOT_READ_MIN
    ]
    snr_all.sort(key=lambda r: (-r["surfaced"], r["path"]))

    # --- section 5: cuts ---
    by_scope_all: dict[str, int] = {}
    by_type_all: dict[str, int] = {}
    for note in notes:
        s = note["scope"] or "—"
        t = note["note_type"] or "—"
        by_scope_all[s] = by_scope_all.get(s, 0) + 1
        by_type_all[t] = by_type_all.get(t, 0) + 1
    by_scope_all = _sorted_counts(by_scope_all)
    by_type_all = _sorted_counts(by_type_all)

    coverage = {
        "events": events,
        "skipped_total": _skipped_total(storage),
        "skipped_process": retrieval_log.skipped_count(),
        "oldest_ts": oldest_ts,
    }

    summary = {
        "window_days": days,
        "scope": scope,
        "top": top,
        "total_notes": len(notes),
        "log_days": log_days,
        "by_source": by_source,
        "by_origin": by_origin,
        "e2e_ms_p50": _percentile(e2e, 0.50),
        "e2e_ms_p95": _percentile(e2e, 0.95),
        "zero_day": bool(events == 0 or log_days < ZERO_DAY_MIN_DAYS
                         or events < ZERO_DAY_MIN_EVENTS),
        "archive_suppressed": len(notes) < ARCHIVE_MIN_NOTES,
        "archive_never_edited": never_edited,
        "archive_stale": stale,
        "totals": {
            "hot": len(hot_all),
            "archive_candidates": len(archive_all),
            "surfaced_not_read": len(snr_all),
            "by_scope": len(by_scope_all),
            "by_type": len(by_type_all),
        },
    }

    return {
        "summary": summary,
        "hot": _cap(hot_all, top),
        "archive_candidates": _cap(archive_all, top),
        "surfaced_not_read": _cap(snr_all, top),
        "by_scope": _cap_counts(by_scope_all, top),
        "by_type": _cap_counts(by_type_all, top),
        "routing_health": _routing_health(vault_path),
        "coverage": coverage,
    }


# --------------------------------------------------------------------------
# I-27 — rendering
# --------------------------------------------------------------------------

# §6.2 п. 1 — WORDING IS THE SPEC's, verbatim: the number is cumulative, not
# per-window, and it is a lower bound (§2.4 п. 4). Do not "improve" this line.
INCOMPLETE_FMT = ("журнал неполон: за всё время пропущено {n} событий "
                  "(счётчик накопительный, не за окно; нижняя оценка)")
TAIL_FMT = "показано {shown} из {total} — --full / full=true"
TRUNCATED_FMT = "…отчёт обрезан потолком в {n} строк — --full / full=true"

# §6.3 — the two honest lines a first-day install must see instead of numbers it
# has not earned yet. Wording follows the spec; only the count words are inflected.
ZERO_DAY_FMT = ("Журнал выдачи накапливается: {events} {ev_word} за {days} {day_word}. "
                "«Горячие» и «выданы, но не прочитаны» появятся после {min_days} дней "
                "наблюдения; ниже — только то, что видно по ссылкам и правкам.")
ARCHIVE_SUPPRESSED_FMT = "кандидатов в архив ещё не считаем: нот в памяти {n}"


def _plural(n: int, one: str, few: str, many: str) -> str:
    """Russian plural for the two literal sentences of §6.2/§6.3."""
    n = abs(int(n))
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


def _tail(shown: int, total: int) -> list[str]:
    return [TAIL_FMT.format(shown=shown, total=total)] if total > shown else []


def _label(row: dict) -> str:
    """`path` — title, or just `path` when the note is gone from the index (Р6.7)."""
    title = row.get("title")
    return f"`{row['path']}` — {title}" if title else f"`{row['path']}` (нет в индексе)"


def _header_block(report: dict) -> list[str]:
    s = report["summary"]
    cov = report["coverage"]
    head = f"нот: {s['total_notes']}  |  окно: {s['window_days']} дн.  |  " \
           f"событий: {cov['events']}  |  журналу: {s['log_days']} дн."
    if s["scope"]:
        head += f"  |  scope: {s['scope']}"
    lines = ["# Отчёт памяти", "", head]
    if s["by_source"]:
        lines.append("источники: " + ", ".join(f"{k} {v}" for k, v in s["by_source"].items()))
    if s["by_origin"]:
        lines.append("origin: " + ", ".join(f"{k} {v}" for k, v in s["by_origin"].items()))
    if s["e2e_ms_p50"] is not None:
        lines.append(f"e2e хук-путей: p50 {s['e2e_ms_p50']} мс, p95 {s['e2e_ms_p95']} мс")
    if cov["skipped_total"]:
        lines.append(INCOMPLETE_FMT.format(n=cov["skipped_total"]))
    return lines


def _hot_block(report: dict) -> list[str]:
    rows = report["hot"]
    total = report["summary"]["totals"]["hot"]
    lines = ["", f"## Горячие (сортировка: mcp_search + mcp_read, убыв.) — {total}"]
    if not rows:
        lines.append("осознанных обращений (mcp_search + mcp_read) за окно нет")
        return lines
    for r in rows:
        lines.append(f"- {_label(r)} — выдана: hook {r['hook']}, mcp {r['mcp']}; "
                     f"прочитана {r['read']}")
    return lines + _tail(len(rows), total)


def _zero_day_block(report: dict) -> list[str]:
    s = report["summary"]
    events = report["coverage"]["events"]
    days = s["log_days"]
    return ["", ZERO_DAY_FMT.format(
        events=events,
        ev_word=_plural(events, "событие", "события", "событий"),
        days=days,
        day_word=_plural(days, "день", "дня", "дней"),
        min_days=ZERO_DAY_MIN_DAYS,
    )]


def _archive_block(report: dict) -> list[str]:
    s = report["summary"]
    rows = report["archive_candidates"]
    total = s["totals"]["archive_candidates"]
    lines = ["", f"## Кандидаты в архив (сирота + мёртвая правка) — {total}",
             f"ни разу не правились: {s['archive_never_edited']}, "
             f"не правились > {ARCHIVE_STALE_DAYS} дн.: {s['archive_stale']}"]
    if not rows:
        lines.append("кандидатов нет")
        return lines
    for r in rows:
        why = ", ".join("ни разу не правилась" if x == "never_edited"
                        else f"> {ARCHIVE_STALE_DAYS} дн." for x in r["reasons"])
        lines.append(f"- {_label(r)} — не правилась {r['age_days']} дн. [{why}]")
    return lines + _tail(len(rows), total)


def _surfaced_block(report: dict) -> list[str]:
    rows = report["surfaced_not_read"]
    total = report["summary"]["totals"]["surfaced_not_read"]
    lines = ["", f"## Выданы, но не прочитаны (≥ {SURFACED_NOT_READ_MIN} выдач, 0 чтений; "
                 f"сортировка: выдач убыв.) — {total}"]
    if not rows:
        lines.append("таких нот за окно нет")
        return lines
    for r in rows:
        lines.append(f"- {_label(r)} — выдана {r['surfaced']} "
                     f"(hook {r['hook']}, mcp {r['mcp']}), прочитана 0")
    return lines + _tail(len(rows), total)


def _cuts_block(report: dict) -> list[str]:
    s = report["summary"]
    lines = ["", "## Разрезы"]
    for key, caption in (("by_scope", "scope"), ("by_type", "type")):
        counts = report[key]
        total = s["totals"][key]
        body = ", ".join(f"{k} {v}" for k, v in counts.items()) or "—"
        if total > len(counts):
            body += f" (+{total - len(counts)} ещё)"
        lines.append(f"{caption}: {body}")
    return lines


def _routing_block(report: dict) -> list[str]:
    rh = report["routing_health"]
    lines = ["", "## Здоровье роутинга"]
    if not rh["meta_present"]:
        lines.append(f"{META_BASENAME} не найден — роуты ещё не компилировались")
        return lines
    lines.append(
        f"скомпилировано {rh['rules_compiled']} из {rh['rules_total']}, "
        f"отброшено {rh['skipped']}, мёртвых паттернов {rh['unmatched_patterns']}, "
        f"предупреждений промпт-роутов {rh['prompt_route_warnings']}"
    )
    return lines


def render_report(report: dict, *, full: bool = False,
                  max_lines: int = DEFAULT_MAX_LINES) -> str:
    """Render the report (I-27).

    `full=True` prints everything; otherwise the whole text is hard-capped at
    `max_lines` (§6.2 п. 7): blocks are emitted in a fixed order, the first block
    that does not fit is truncated and replaced by the truncation notice, and
    nothing after it is printed.
    """
    s = report["summary"]
    blocks = [_header_block(report)]
    if s["zero_day"]:
        # §6.3: sections 2 and 4 both rest on the log, so ONE honest line replaces
        # both — not two apologies.
        blocks.append(_zero_day_block(report))
    else:
        blocks.append(_hot_block(report))
    if s["archive_suppressed"]:
        blocks.append(["", ARCHIVE_SUPPRESSED_FMT.format(n=s["total_notes"])])
    else:
        blocks.append(_archive_block(report))
    if not s["zero_day"]:
        blocks.append(_surfaced_block(report))

    # §6.2/I-31: "Разрезы" and "Здоровье роутинга" are the only surface for
    # prompt_route_warnings and must not disappear just because the list
    # sections ahead of them (hot / archive / surfaced-not-read, all capped by
    # `top`, not by line count) filled the whole budget first. Both tail blocks
    # have a FIXED length regardless of vault size — one joined line per cut
    # dimension, one summary line for routing health — so reserving their
    # combined length up front costs the list sections a small, bounded number
    # of rows and buys the tail an unconditional place in the default render
    # (A3). They are appended after the loop, never through it, so they are
    # never the block that eats the truncation notice.
    tail_blocks = [_cuts_block(report), _routing_block(report)]
    tail_lines = sum(len(b) for b in tail_blocks)
    body_budget = max(0, max_lines - tail_lines)

    lines: list[str] = []
    for block in blocks:
        if not block:
            continue
        if full:
            lines.extend(block)
            continue
        room = body_budget - len(lines)
        if room <= 0:
            break
        if len(block) <= room:
            lines.extend(block)
            continue
        lines.extend(block[:max(0, room - 1)])
        lines.append(TRUNCATED_FMT.format(n=max_lines))
        break

    for block in tail_blocks:
        lines.extend(block)
    return "\n".join(lines)
