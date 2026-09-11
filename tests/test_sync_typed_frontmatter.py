"""A frontmatter value YAML types as something other than a string must cost
at most that one note — never the whole vault.

python-frontmatter parses with yaml.SafeLoader, which turns an unquoted
`reviewed: 2026-09-10` into datetime.date, `2026-09-10 12:30:00` into
datetime, `!!binary` into bytes and `!!set` into set. parse_note normalised
dates only for valid_from/valid_to/created_at; every other field went into
`extra` as-is, `json.dumps(frontmatter)` in upsert_note raised TypeError, and
sync_all's per-note guard did not list TypeError — so one such note aborted
the entire sync, with a message that named no file (recurrence 2026-09-10:
drift of 357 notes after a `git pull`).

Two fixes, each covering a class the other does not:
- dates and datetimes inside the frontmatter — values, nested values, nested
  keys — become ISO strings in parse_note, so the realistic note (a person
  typing `date: 2026-09-07`) is INDEXED, not merely survived;
- anything no normalisation can reach or serialise is caught per note: the
  note lands in `failed` under its own path and the rest of the vault syncs.
  That covers bytes and sets, and also a date used as a TOP-LEVEL key, which
  python-frontmatter itself rejects (`Post(**metadata)` needs string keywords)
  before parse_note ever sees it.
"""
from __future__ import annotations

import json
from pathlib import Path

from symbiosis_brain.markdown_parser import parse_note
from symbiosis_brain.storage import Storage
from symbiosis_brain.sync import VaultSync

HEAD = "---\ntitle: {title}\ntype: wiki\nscope: global\ngist: x\n{extra}---\n# {title}\n\nbody\n"


def _note(title: str, extra: str = "") -> str:
    return HEAD.format(title=title, extra=extra)


def _vault(tmp_path: Path, notes: dict[str, str]) -> tuple[Path, Storage, VaultSync]:
    vault = tmp_path / "v"
    (vault / "wiki").mkdir(parents=True)
    for name, text in notes.items():
        (vault / "wiki" / name).write_text(text, encoding="utf-8")
    storage = Storage(tmp_path / "t.db")
    return vault, storage, VaultSync(vault, storage)


def test_parse_note_turns_every_date_into_an_iso_string():
    extra = ("reviewed: 2026-09-10\n"
             "when: 2026-09-10 12:30:00\n"
             "review:\n"
             "  last: 2026-09-01\n"
             "  history: [2026-08-01, 2026-07-01]\n"
             "  2026-09-11: a date as a nested key\n")
    parsed = parse_note(_note("N", extra))
    ex = parsed["extra"]
    assert ex["reviewed"] == "2026-09-10"
    assert ex["when"].startswith("2026-09-10T12:30:00")
    assert ex["review"] == {"last": "2026-09-01", "history": ["2026-08-01", "2026-07-01"],
                            "2026-09-11": "a date as a nested key"}
    json.dumps(parsed["extra"])  # the whole point: it serialises now


def test_unquoted_date_in_an_extra_field_is_indexed_not_fatal(tmp_path):
    vault, storage, sync = _vault(tmp_path, {
        "dated.md": _note("Dated", "date: 2026-09-07\n"),
        "plain.md": _note("Plain"),
    })

    result = sync.sync_all()

    assert result.failed == []
    assert sorted(result.added) == ["wiki/dated.md", "wiki/plain.md"]
    assert storage.get_note("wiki/dated.md")["frontmatter"]["date"] == "2026-09-07"


def test_a_value_nothing_can_serialise_fails_that_note_by_name_only(tmp_path):
    vault, storage, sync = _vault(tmp_path, {
        "binary.md": _note("Binary", "blob: !!binary aGVsbG8=\n"),
        "aset.md": _note("Aset", "kinds: !!set {a: null}\n"),
        "datekey.md": _note("Datekey", "2026-09-11: a date as a top-level key\n"),
        "plain.md": _note("Plain"),
    })

    result = sync.sync_all()  # must not raise

    failed = dict(result.failed)
    assert set(failed) == {"wiki/binary.md", "wiki/aset.md", "wiki/datekey.md"}
    assert all(msg.startswith("TypeError") for msg in failed.values())
    assert result.added == ["wiki/plain.md"]
    assert storage.get_note("wiki/plain.md") is not None
