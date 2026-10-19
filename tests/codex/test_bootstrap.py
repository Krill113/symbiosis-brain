from __future__ import annotations

import os
import subprocess
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from symbiosis_brain.adapters.codex.bootstrap import (
    build_bootstrap,
    estimate_tokens,
    extract_project_context,
    select_handoff,
)
from symbiosis_brain.adapters.codex import bootstrap as bootstrap_module
from symbiosis_brain.adapters.codex.config import CodexConfig
from symbiosis_brain.adapters.codex.scope import ScopeResolution
from symbiosis_brain.pre_action_config import PreActionConfig


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _directory_link(link: Path, target: Path) -> None:
    try:
        link.symlink_to(target, target_is_directory=True)
        return
    except OSError:
        if os.name != "nt":
            raise
    completed = subprocess.run(
        ["cmd.exe", "/d", "/c", "mklink", "/J", str(link), str(target)],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise OSError(completed.stderr or completed.stdout)


def _file_link(link: Path, target: Path) -> None:
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(target)


def _taxonomy(vault: Path, *scopes: str) -> None:
    rows = "\n".join(f"| `{scope}` | synthetic |" for scope in scopes)
    _write(
        vault / "reference" / "scope-taxonomy.md",
        "## Whitelist\n\n| scope | purpose |\n|---|---|\n"
        f"{rows}\n\n"
        "## Folder ↔ type convention\n\n"
        "| folder | type |\n|---|---|\n"
        "| `projects/` | `project` |\n"
        "| `decisions/` | `decision` |\n"
        "| `reference/` | `wiki` |\n",
    )


def _config(*, stale_days: int = 30) -> CodexConfig:
    return CodexConfig(
        bootstrap_visibility="one-line",
        trusted_projects=(),
        handoff_stale_days=stale_days,
        save_reminder_turns=10,
        status_line_policy="recommend-only",
        sync_mode="manual",
        sync_bash_path=None,
        recall=PreActionConfig(),
    )


def _note(
    *,
    path: str,
    title: str,
    content: str,
    valid_from: str | None,
    tags: list[str] | None = None,
    valid_to: str | None = None,
    frontmatter: dict[str, Any] | None = None,
    scope: str = "demo",
    note_type: str = "project",
) -> dict[str, Any]:
    return {
        "path": path,
        "title": title,
        "content": content,
        "scope": scope,
        "note_type": note_type,
        "tags": tags or [],
        "frontmatter": frontmatter or {},
        "valid_from": valid_from,
        "valid_to": valid_to,
        "updated_at": "2026-10-09T00:00:00+00:00",
    }


def test_separate_newer_handoff_beats_card_handoff() -> None:
    notes = [
        _note(
            path="demo/reference/setup-handoff-2026-10-08.md",
            title="Session handoff 2026-10-08",
            content="## Next step\nConnect tools.",
            tags=["handoff"],
            valid_from="2026-10-08",
        ),
        _note(
            path="demo/archive/demo-2026-09-01.md",
            title="Handoff 2026-09-01",
            content="Old.",
            tags=["handoff"],
            valid_from="2026-09-01",
        ),
    ]

    result = select_handoff(
        "## Handoff 2026-09-30\nCard handoff.\n",
        notes,
        stale_days=30,
        today=date(2026, 10, 9),
    )

    assert result is not None
    assert result.path == "demo/reference/setup-handoff-2026-10-08.md"
    assert result.date == date(2026, 10, 8)
    assert result.text == "## Next step\nConnect tools."
    assert result.stale is False
    assert result.age_days == 1


def test_newer_card_handoff_beats_separate_note() -> None:
    notes = [
        _note(
            path="demo/reference/handoff.md",
            title="Handoff 2026-10-01",
            content="Separate.",
            tags=["handoff"],
            valid_from="2026-10-01",
        )
    ]

    result = select_handoff(
        "## Handoff 2026-10-08\nCard wins.\n",
        notes,
        stale_days=30,
        today=date(2026, 10, 9),
    )

    assert result is not None
    assert result.path == "project-card"
    assert result.date == date(2026, 10, 8)
    assert "Card wins." in result.text


def test_closed_superseded_and_malformed_handoffs_are_excluded() -> None:
    notes = [
        _note(
            path="demo/closed.md",
            title="Handoff 2026-10-08",
            content="Closed.",
            tags=["handoff"],
            valid_from="2026-10-08",
            valid_to="2026-10-09",
        ),
        _note(
            path="demo/superseded.md",
            title="Handoff 2026-10-07",
            content="Superseded.",
            tags=["handoff"],
            valid_from="2026-10-07",
            frontmatter={"superseded_by": "demo/current.md"},
        ),
        _note(
            path="demo/malformed.md",
            title="Handoff 2026-10-06",
            content="Malformed.",
            tags=["handoff"],
            valid_from="not-a-date",
        ),
    ]

    result = select_handoff(
        "",
        notes,
        stale_days=30,
        today=date(2026, 10, 9),
    )

    assert result is None


def test_archive_only_handoff_is_retained() -> None:
    notes = [
        _note(
            path="demo/archive/demo-2026-10-02.md",
            title="Archived session",
            content="## Next step\nResume the synthetic task.",
            tags=["handoff"],
            valid_from="2026-10-02",
        )
    ]

    result = select_handoff(
        "",
        notes,
        stale_days=30,
        today=date(2026, 10, 9),
    )

    assert result is not None
    assert result.path == "demo/archive/demo-2026-10-02.md"


def test_unrelated_dated_heading_is_not_a_handoff_candidate() -> None:
    notes = [
        _note(
            path="demo/reference/setup.md",
            title="Session setup",
            content="## 2026-10-08\nContinue the setup.",
            valid_from=None,
        )
    ]

    result = select_handoff(
        "",
        notes,
        stale_days=30,
        today=date(2026, 10, 9),
    )

    assert result is None


def test_structured_handoff_heading_wins_over_other_note_dates() -> None:
    notes = [
        _note(
            path="demo/reference/handoff-2026-10-07.md",
            title="Handoff reviewed 2026-10-08",
            content=(
                "Release reference 2026-10-01.\n\n"
                "## Handoff 2026-10-05\nContinue the bounded work.\n"
            ),
            tags=["handoff"],
            valid_from="2026-10-02",
        )
    ]

    result = select_handoff(
        "",
        notes,
        stale_days=30,
        today=date(2026, 10, 9),
    )

    assert result is not None
    assert result.date == date(2026, 10, 5)


def test_mixed_past_and_future_handoff_note_returns_only_latest_past_section() -> None:
    notes = [
        _note(
            path="demo/reference/session-handoff.md",
            title="Session handoff",
            content=(
                "Intro must not be returned.\n\n"
                "## Handoff 2026-10-07\nOlder past section.\n\n"
                "## Handoff 2026-10-08\nSelected past section.\n\n"
                "## Handoff 2026-10-10\nFUTURE SECRET.\n"
            ),
            tags=["handoff"],
            valid_from="2026-10-10",
        )
    ]

    result = select_handoff(
        "",
        notes,
        stale_days=30,
        today=date(2026, 10, 9),
    )

    assert result is not None
    assert result.date == date(2026, 10, 8)
    assert result.text == "## Handoff 2026-10-08\nSelected past section."
    assert "Older past section" not in result.text
    assert "FUTURE SECRET" not in result.text


def test_ambiguous_multiple_title_dates_are_rejected() -> None:
    notes = [
        _note(
            path="demo/reference/session.md",
            title="Handoff 2026-10-05 reviewed 2026-10-08",
            content="Continue the bounded work.",
            tags=["handoff"],
            valid_from=None,
        )
    ]

    assert (
        select_handoff(
            "",
            notes,
            stale_days=30,
            today=date(2026, 10, 9),
        )
        is None
    )


def test_conflicting_fallback_handoff_dates_are_rejected() -> None:
    notes = [
        _note(
            path="demo/reference/handoff-2026-10-07.md",
            title="Handoff 2026-10-08",
            content="Continue the bounded work.",
            tags=["handoff"],
            valid_from=None,
        )
    ]

    assert (
        select_handoff(
            "",
            notes,
            stale_days=30,
            today=date(2026, 10, 9),
        )
        is None
    )


@pytest.mark.parametrize(
    "note",
    [
        _note(
            path="demo/reference/handoff.md",
            title="Session handoff",
            content="Continue later.",
            tags=["handoff"],
            valid_from="2026-10-10",
        ),
        _note(
            path="demo/reference/handoff-2026-10-10.md",
            title="Session handoff",
            content="Continue later.",
            tags=["handoff"],
            valid_from=None,
        ),
        _note(
            path="demo/reference/session.md",
            title="Session handoff",
            content="## Handoff 2026-10-10\nContinue later.",
            tags=["handoff"],
            valid_from=None,
        ),
    ],
)
def test_future_handoff_dates_are_rejected(note: dict[str, Any]) -> None:
    assert (
        select_handoff(
            "",
            [note],
            stale_days=30,
            today=date(2026, 10, 9),
        )
        is None
    )


def test_handoff_older_than_threshold_is_labelled_stale() -> None:
    result = select_handoff(
        "## Handoff 2026-09-08\nVerify before continuing.\n",
        [],
        stale_days=30,
        today=date(2026, 10, 9),
    )

    assert result is not None
    assert result.age_days == 31
    assert result.stale is True


def test_estimator_is_conservative_and_dependency_free() -> None:
    assert estimate_tokens("") == 0
    assert estimate_tokens("a") == 1
    assert estimate_tokens("abcd") == 2
    assert estimate_tokens("abcdef") == 2


def test_large_card_extracts_only_active_sections_within_budget() -> None:
    card = (
        "# Demo\n\n"
        "## Background\n"
        + ("Historical detail. " * 500)
        + "\n\n## Current Status\n"
        + ("Current work is healthy. " * 80)
        + "\n\n## Next Up\n"
        + ("Run focused verification. " * 80)
        + "\n\n## Handoff 2026-10-08\n"
        + ("Continue from the bounded adapter. " * 80)
        + "\n\n## Archive\n"
        + ("Old archive entry. " * 500)
    )

    result = extract_project_context(card, token_budget=240)

    assert estimate_tokens(result) <= 240
    assert "Current Status" in result
    assert "Next Up" in result
    assert "Handoff 2026-10-08" in result
    assert "Background" not in result
    assert "Archive" not in result
    assert result.rstrip().endswith((".", "!", "?", "\n"))


def test_unpunctuated_single_line_is_omitted_instead_of_word_cut() -> None:
    text = "unpunctuated " * 100

    assert bootstrap_module._truncate_at_boundary(text, 20) == ""


def test_truncation_does_not_treat_cut_inside_token_as_sentence_end() -> None:
    assert bootstrap_module._truncate_at_boundary("abc.defghijkl", 2) == ""


def _resolution(scope: str | None, *, warning: str | None = None) -> ScopeResolution:
    return ScopeResolution(
        scope=scope,
        source="basename" if scope is not None else "none",
        marker_path=None,
        depth=0 if scope is not None else None,
        warning=warning,
        trusted=True,
        allow_global=True,
        memory_authorized=True,
    )


def test_unavailable_workspace_bootstrap_reads_no_vault(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    resolution = ScopeResolution(
        scope=None,
        source="none",
        marker_path=None,
        depth=None,
        warning="workspace unavailable",
        trusted=False,
        memory_authorized=False,
    )

    monkeypatch.setattr(
        bootstrap_module,
        "secure_read_text",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("unavailable workspace must not read the vault")
        ),
    )

    result = build_bootstrap(
        vault,
        resolution,
        _config(),
        source="startup",
        today=date(2026, 10, 15),
    )

    assert result.context == ""
    assert result.scope is None
    assert result.warning == "workspace unavailable"


def test_unknown_project_still_receives_global_bootstrap(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    _write(vault / "CRITICAL_FACTS.md", "Synthetic global fact.")

    result = build_bootstrap(
        vault,
        _resolution("unknown-project"),
        _config(),
        source="startup",
        today=date(2026, 10, 15),
    )

    assert result.scope == "unknown-project"
    assert "Synthetic global fact." in result.context
    assert "scope: unknown-project" in result.context
    assert result.warning == "project card missing"


def test_project_bootstrap_combines_global_card_handoff_and_related(
    tmp_path: Path,
) -> None:
    vault = tmp_path / "vault"
    _write(vault / "CRITICAL_FACTS.md", "Use only synthetic fixtures.")
    _write(
        vault / "demo" / "demo.md",
        "---\ntitle: Demo\ntype: project\nscope: demo\n---\n\n"
        "# Demo\n\n## Current Status\nAdapter work is active.\n\n"
        "## Next Up\nRun verification.\n\n"
        "## Handoff 2026-10-10\nOlder inline handoff.\n",
    )
    _write(
        vault / "demo" / "reference" / "handoff-2026-10-14.md",
        "---\n"
        "title: Session handoff 2026-10-14\n"
        "type: project\n"
        "scope: demo\n"
        "valid_from: 2026-10-14\n"
        "tags: [handoff]\n"
        "---\n\n"
        "## Next step\nRun the isolated suite.\n",
    )
    _write(
        vault / "demo" / "decisions" / "decision.md",
        "---\n"
        "title: Canonical memory\n"
        "type: decision\n"
        "scope: demo\n"
        "gist: Use canonical global plus project memory\n"
        "valid_from: 2026-10-13\n"
        "---\n\n"
        "Canonical behavior.\n",
    )

    result = build_bootstrap(
        vault,
        _resolution("demo"),
        _config(),
        source="startup",
        today=date(2026, 10, 15),
    )

    assert result.scope == "demo"
    assert result.handoff is not None
    assert result.handoff.date == date(2026, 10, 14)
    assert result.related_count == 1
    assert "Use only synthetic fixtures." in result.context
    assert "Adapter work is active." in result.context
    assert "Run the isolated suite." in result.context
    assert "Use canonical global plus project memory" in result.context
    assert estimate_tokens(result.context) <= 3200
    assert str(tmp_path) not in result.context


def test_bootstrap_uses_secure_reads_without_sync_or_search(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import symbiosis_brain.search as search_module
    import symbiosis_brain.sync as sync_module

    vault = tmp_path / "vault"
    _write(vault / "CRITICAL_FACTS.md", "Synthetic global fact.")
    _write(
        vault / "projects" / "demo.md",
        "---\ntitle: Demo\ntype: project\nscope: demo\n---\n\n"
        "# Demo\n\n## Next Up\nRead the legacy card.\n",
    )
    reads: list[str] = []
    original_read = bootstrap_module.secure_read_text

    def record_read(root: Path, relative: str | Path) -> str:
        reads.append(Path(relative).as_posix())
        return original_read(root, relative)

    monkeypatch.setattr(bootstrap_module, "secure_read_text", record_read)
    monkeypatch.setattr(
        sync_module.VaultSync,
        "sync_all",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("bootstrap must not sync")
        ),
    )
    monkeypatch.setattr(
        search_module,
        "SearchEngine",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("bootstrap must not create SearchEngine")
        ),
    )

    result = build_bootstrap(
        vault,
        _resolution("demo"),
        _config(),
        source="startup",
        today=date(2026, 10, 15),
    )

    assert reads[:2] == ["CRITICAL_FACTS.md", "demo/demo.md"]
    assert "projects/demo.md" in reads
    assert "Synthetic global fact." in result.context
    assert "Read the legacy card." in result.context


def test_unsafe_project_scope_keeps_global_context_without_project_escape(
    tmp_path: Path,
) -> None:
    vault = tmp_path / "vault"
    outside = tmp_path / "outside"
    _write(vault / "CRITICAL_FACTS.md", "Safe global context.")
    _write(outside / "outside.md", "MUST NOT LEAK")

    result = build_bootstrap(
        vault,
        _resolution("../outside"),
        _config(),
        source="startup",
        today=date(2026, 10, 15),
    )

    assert "Safe global context." in result.context
    assert "MUST NOT LEAK" not in result.context
    assert result.warning == "unsafe vault path"


def test_scope_directory_link_escape_keeps_global_and_drops_project(
    tmp_path: Path,
) -> None:
    vault = tmp_path / "vault"
    outside = tmp_path / "outside"
    vault.mkdir()
    outside.mkdir()
    _write(vault / "CRITICAL_FACTS.md", "Safe global context.")
    _write(outside / "demo.md", "MUST NOT LEAK")
    try:
        _directory_link(vault / "demo", outside)
    except OSError as exc:
        pytest.skip(f"directory links unavailable: {exc}")

    result = build_bootstrap(
        vault,
        _resolution("demo"),
        _config(),
        source="startup",
        today=date(2026, 10, 15),
    )

    assert "Safe global context." in result.context
    assert "MUST NOT LEAK" not in result.context
    assert result.warning == "unsafe vault path"


def test_compact_bootstrap_is_smaller_and_skips_related(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    _write(vault / "CRITICAL_FACTS.md", "Global synthetic fact. " * 40)
    _write(
        vault / "demo" / "demo.md",
        "---\ntitle: Demo\ntype: project\nscope: demo\n---\n\n"
        "# Demo\n\n"
        "## Current Status\n" + ("Active status sentence. " * 80) + "\n\n"
        "## Next Up\n" + ("Next action sentence. " * 80) + "\n\n"
        "## Handoff 2026-10-14\n" + ("Resume work sentence. " * 80),
    )
    _write(
        vault / "demo" / "decisions" / "decision.md",
        "---\n"
        "title: Related decision\n"
        "type: decision\n"
        "scope: demo\n"
        "gist: This must not enter compact context\n"
        "valid_from: 2026-10-13\n"
        "---\n\nRelated detail.\n",
    )
    resolution = _resolution("demo")

    startup = build_bootstrap(
        vault,
        resolution,
        _config(),
        source="startup",
        today=date(2026, 10, 15),
    )
    compact = build_bootstrap(
        vault,
        resolution,
        _config(),
        source="compact",
        today=date(2026, 10, 15),
    )

    assert estimate_tokens(compact.context) < estimate_tokens(startup.context)
    assert compact.related_count == 0
    assert "Related decision" not in compact.context
