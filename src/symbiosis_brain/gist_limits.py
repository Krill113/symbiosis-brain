"""Single source of truth for every gist length limit.

Before this module the same four numbers lived in three files — rotation.GIST_MAX,
rotation.INDEX_ONELINER_MAX, validation.GIST_SOFT_LIMIT, validation.GIST_HARD_LIMIT —
plus a bare literal `100` in lint.py's gist_too_long check (B-N4/B-N6). Nothing kept
them in step, so a threshold could be moved in one place and silently disagree with
the other three. Import from here; never restate a limit at a call site.
"""

GIST_SOFT_LIMIT = 100
"""Recommended ceiling. lint reports a longer gist as `gist_too_long`;
brain_write emits a soft warning but still writes the note."""

GIST_HARD_LIMIT = 140
"""Hard ceiling. brain_write rejects a longer gist outright (Q5 closure 2026-05-14)."""

GIST_ARCHIVE_MAX = 140
"""rotation.extract_gist — max length of the gist written into an archived
handoff's frontmatter. Was rotation.GIST_MAX."""

GIST_INDEX_ONELINER_MAX = 100
"""rotation.render_archive_index_entry — max length of the one-line snippet in a
card's `## Archive` index. Was rotation.INDEX_ONELINER_MAX."""
