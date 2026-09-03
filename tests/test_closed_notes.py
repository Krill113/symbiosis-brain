"""A note whose `valid_to` has passed stated something that no longer holds.

It stays findable — `brain_search` returns it, and the duplicate check on the
write path needs it more than anyone — but it is not injected into a prompt as
though it were current. Which of the two a caller wants is a policy, so every
call site states it: `include_closed` is a keyword on `SearchEngine.search`, and
the guard at the bottom of this file requires our own code to pass it rather
than inherit a default.
"""
import ast
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from conftest import REPO_ROOT, iter_source_files

from symbiosis_brain.search import SearchEngine, dedup_candidates
from symbiosis_brain.storage import Storage

PAST = "2025-03-15"


def _future() -> str:
    return (datetime.now(timezone.utc) + timedelta(days=90)).date().isoformat()


@pytest.fixture
def engine_with_a_closed_note(db_path: Path) -> SearchEngine:
    storage = Storage(db_path)
    storage.upsert_note(path="d/dapper-live.md", title="Dapper Choice",
                        content="Chose Dapper over EF Core for performance",
                        note_type="decision", scope="global",
                        valid_from="2025-01-01")
    storage.upsert_note(path="d/dapper-closed.md", title="Dapper Choice superseded",
                        content="Chose Dapper over EF Core for performance",
                        note_type="decision", scope="global",
                        valid_from="2024-01-01", valid_to=PAST)
    engine = SearchEngine(storage)
    engine.index_all()
    return engine


def _paths(hits) -> set[str]:
    return {h["path"] for h in hits}


class TestClosedNotesInSearch:
    def test_closed_note_is_kept_out_of_injected_recall(self, engine_with_a_closed_note):
        """The hook paths inject what nobody asked for, so a superseded claim
        shown there reads as current and the reader has no reason to doubt it."""
        hits = engine_with_a_closed_note.search(
            query="Dapper EF Core performance", limit=10, include_closed=False)
        assert "d/dapper-closed.md" not in _paths(hits)
        assert "d/dapper-live.md" in _paths(hits), \
            "the filter must remove the closed note only, not empty the result"

    def test_closed_note_is_returned_to_an_explicit_search(self, engine_with_a_closed_note):
        """A person searching their own memory is asking a different question,
        and "there was a decision, it was superseded" is an answer to it."""
        hits = engine_with_a_closed_note.search(
            query="Dapper EF Core performance", limit=10, include_closed=True)
        assert "d/dapper-closed.md" in _paths(hits)

    def test_a_valid_to_still_ahead_is_not_a_closure(self, db_path: Path):
        """`valid_to` is when a claim STOPS holding. A date in the future means
        it holds now, and the naive "is not None" rule would hide exactly the
        notes someone took the trouble to mark as valid until a known day."""
        storage = Storage(db_path)
        storage.upsert_note(path="d/expires.md", title="Licence decision",
                            content="Chose Dapper over EF Core for performance",
                            note_type="decision", scope="global",
                            valid_from="2026-01-01", valid_to=_future())
        engine = SearchEngine(storage)
        engine.index_all()

        for include_closed in (True, False):
            hits = engine.search(query="Dapper EF Core performance", limit=10,
                                 include_closed=include_closed)
            assert "d/expires.md" in _paths(hits), \
                f"a future valid_to must not hide the note (include_closed={include_closed})"


class TestDedupSeesClosedNotes:
    def test_closed_duplicate_is_reported_and_marked(self, engine_with_a_closed_note):
        """Hiding closed notes from the duplicate check would let us write a
        duplicate of a deliberately superseded claim with no link back to it —
        the very thing `valid_to` was set to prevent, reached from the other
        side. And the failure is silent: no hint is indistinguishable from
        "nothing like this exists".

        The marker matters as much as the inclusion. "A live note already says
        this" and "a closed note used to say this" call for opposite actions;
        rendered identically, the author reads a tombstone as a living duplicate
        and does not write the note that was needed."""
        found = dedup_candidates(
            engine_with_a_closed_note, engine_with_a_closed_note.storage,
            title="Dapper Choice superseded",
            gist="Chose Dapper over EF Core for performance",
            self_path="d/new-note.md",
        )
        by_path = {c["path"]: c for c in found}
        assert "d/dapper-closed.md" in by_path, \
            "a closed note is exactly what the write path must be warned about"
        assert by_path["d/dapper-closed.md"]["closed"] is True
        if "d/dapper-live.md" in by_path:
            assert by_path["d/dapper-live.md"]["closed"] is False


class TestEveryCallSiteStatesThePolicy:
    """`include_closed` defaults to True — today's behaviour — so a forgotten
    call site shows a closed note rather than hiding a live one: of the two
    mistakes, the harmless one. The default is not the guarantee, though. This
    is: our own code has to say which it wants, and CI says so before the code
    ever runs, which a required argument could not — a `TypeError` only fires on
    a path something actually calls.

    Known limit, stated rather than papered over: a call that passes the query
    positionally and names no other keyword is invisible here. Every call in the
    codebase uses keywords, and the ones that matter pass `query=`, but this
    catches a shape, not a type."""

    # Distinctive to SearchEngine.search — `re.search`/`pattern.search` never
    # carry any of them, which is how this tells the two apart.
    OUR_KEYWORDS = {"query", "fts_mode", "log_ctx", "mode", "scope", "limit"}

    def test_source_calls_pass_include_closed(self):
        offenders = {}
        for path in iter_source_files():
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                if not (isinstance(func, ast.Attribute) and func.attr == "search"):
                    continue
                named = {kw.arg for kw in node.keywords if kw.arg}
                if not (named & self.OUR_KEYWORDS):
                    continue                    # a regex .search(), not ours
                if "include_closed" not in named:
                    rel = path.relative_to(REPO_ROOT).as_posix()
                    offenders.setdefault(rel, []).append(node.lineno)

        assert not offenders, (
            "these calls to SearchEngine.search do not say whether closed notes "
            "belong in their result, and would silently inherit the default: "
            + repr(offenders)
        )
