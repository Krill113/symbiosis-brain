"""Tests for write-time validation gates.

Hard-block: missing_gist, malformed_frontmatter, broken_outgoing_ref.
Soft-warn:  gist_too_long, few_wiki_links.
"""
import pytest

from symbiosis_brain.storage import Storage
from symbiosis_brain.validation import (
    ValidationError,
    validate_note,
    Warning_,
)


def _storage_with_note(tmp_path, path: str = "wiki/existing.md") -> Storage:
    storage = Storage(tmp_path / "test.db")
    storage.upsert_note(
        path=path,
        title="Existing",
        content="# H",
        note_type="wiki",
        scope="global",
        tags=[],
        frontmatter={"gist": "x"},
        valid_from=None,
        valid_to=None,
    )
    return storage


def test_missing_gist_raises_validation_error(tmp_path):
    storage = _storage_with_note(tmp_path)
    with pytest.raises(ValidationError) as exc:
        validate_note(
            path="wiki/new.md",
            title="New",
            body="# H\n[[wiki/existing]] [[wiki/existing]]",
            frontmatter={},
            storage=storage,
        )
    assert "gist" in str(exc.value).lower()


def test_whitespace_only_gist_raises(tmp_path):
    """gist: '   ' (only whitespace) is not a real gist; treat as missing."""
    storage = _storage_with_note(tmp_path)
    with pytest.raises(ValidationError) as exc:
        validate_note(
            path="wiki/new.md",
            title="New",
            body="# H\n[[wiki/existing]] [[wiki/existing]]",
            frontmatter={"gist": "   "},
            storage=storage,
        )
    assert "gist" in str(exc.value).lower()


def test_malformed_frontmatter_raises(tmp_path):
    storage = _storage_with_note(tmp_path)
    with pytest.raises(ValidationError) as exc:
        validate_note(
            path="wiki/new.md",
            title="New",
            body="# H",
            frontmatter=None,  # type: ignore[arg-type]
            storage=storage,
        )
    assert "frontmatter" in str(exc.value).lower()


def test_broken_outgoing_ref_raises(tmp_path):
    storage = _storage_with_note(tmp_path)
    with pytest.raises(ValidationError) as exc:
        validate_note(
            path="wiki/new.md",
            title="New",
            body="# H\n[[wiki/does-not-exist]]\n[[wiki/existing]]",
            frontmatter={"gist": "x"},
            storage=storage,
        )
    msg = str(exc.value)
    assert "broken" in msg.lower()
    assert "wiki/does-not-exist" in msg


def test_forward_ref_marker_does_not_raise(tmp_path):
    storage = _storage_with_note(tmp_path)
    warnings = validate_note(
        path="wiki/new.md",
        title="New",
        body="# H\n[[forward:wiki/planned]]\n[[wiki/existing]]",
        frontmatter={"gist": "x"},
        storage=storage,
    )
    assert all("broken" not in w.message.lower() for w in warnings)


def test_gist_too_long_returns_warning(tmp_path):
    storage = _storage_with_note(tmp_path)
    long_gist = "x" * 150
    warnings = validate_note(
        path="wiki/new.md",
        title="New",
        body="# H\n[[wiki/existing]] [[wiki/existing]]",
        frontmatter={"gist": long_gist},
        storage=storage,
    )
    rules = [w.rule for w in warnings]
    assert "gist_too_long" in rules


def test_gist_under_limit_no_warning(tmp_path):
    storage = _storage_with_note(tmp_path)
    warnings = validate_note(
        path="wiki/new.md",
        title="New",
        body="# H\n[[wiki/existing]] [[wiki/existing]]",
        frontmatter={"gist": "ok"},
        storage=storage,
    )
    assert "gist_too_long" not in [w.rule for w in warnings]


def test_few_wiki_links_returns_warning(tmp_path):
    storage = _storage_with_note(tmp_path)
    warnings = validate_note(
        path="wiki/new.md",
        title="New",
        body="# H\n[[wiki/existing]]",
        frontmatter={"gist": "x"},
        storage=storage,
    )
    rules = [w.rule for w in warnings]
    assert "few_wiki_links" in rules


def test_two_links_no_few_links_warning(tmp_path):
    storage = _storage_with_note(tmp_path)
    storage.upsert_note(
        path="wiki/second.md", title="Second", content="# S", note_type="wiki",
        scope="global", tags=[], frontmatter={"gist": "x"},
        valid_from=None, valid_to=None,
    )
    warnings = validate_note(
        path="wiki/new.md",
        title="New",
        body="# H\n[[wiki/existing]] [[wiki/second]]",
        frontmatter={"gist": "x"},
        storage=storage,
    )
    assert "few_wiki_links" not in [w.rule for w in warnings]
