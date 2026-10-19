from datetime import datetime, timezone

import pytest

from symbiosis_brain.adapters.opencode.bootstrap import (
    ScopeContext,
    build_bootstrap,
    is_superseded,
    resolve_project,
)
from symbiosis_brain.temporal import TemporalManager


NOW = datetime(2026, 10, 10, tzinfo=timezone.utc)


def write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_future_valid_to_does_not_hide_design_intent(tmp_path):
    folder = tmp_path / "alpha"
    write(
        folder / "alpha.md",
        "---\ntype: project\nscope: alpha\nvalid_to: 2099-01-01\n---\n"
        "# Alpha\n## Purpose\nKeep geometry editable.\n",
    )
    blocks = build_bootstrap(
        tmp_path, ScopeContext("alpha", None), char_limit=8000, now=NOW
    )
    assert "Keep geometry editable" in "\n".join(block.text for block in blocks)


def test_temporal_predicate_matches_core_utc_boundary_matrix():
    for raw, expected in [
        (None, False),
        ("", False),
        ("not-a-date", False),
        ("2026-10-10T00:00:00+00:00", True),
        ("2026-10-10T00:00:01+00:00", False),
        ("2026-10-09T20:00:00-04:00", True),
        ("2026-10-11", False),
    ]:
        note = {"valid_to": raw}
        assert is_superseded(note, now=NOW) is expected
        assert is_superseded(note, now=NOW) == TemporalManager(None).is_superseded(note, now=NOW)


def test_resolve_marker_with_umbrella_and_status_within_explicit_boundary(tmp_path):
    root = tmp_path / "workspace"
    project = root / "alpha" / "nested"
    project.mkdir(parents=True)
    write(
        root / "alpha" / "CLAUDE.md",
        "<!-- symbiosis-brain v1: scope=alpha, umbrella=acme, status=active -->\n",
    )
    context = resolve_project(project, tmp_path / "vault", root)
    assert (context.scope, context.umbrella, context.status) == ("alpha", "acme", "active")


def test_missing_boundary_is_global_only_and_does_not_search_ancestors(tmp_path):
    project = tmp_path / "alpha" / "nested"
    project.mkdir(parents=True)
    write(tmp_path / "alpha" / "CLAUDE.md", "<!-- symbiosis-brain v1: scope=alpha -->")
    context = resolve_project(project, tmp_path / "vault", None)
    assert context.scope is None
    assert context.global_only is True
    assert context.diagnostics


@pytest.mark.parametrize("scope,kind", [("workspace", "workspace-root"), ("umbrella-root", "зонтик")])
def test_broad_taxonomy_marker_on_ancestor_is_global_only(tmp_path, scope, kind):
    root = tmp_path / "workspace"
    project = root / "product" / "nested"
    project.mkdir(parents=True)
    write(root / "CLAUDE.md", f"<!-- symbiosis-brain v1: scope={scope} -->")
    write(tmp_path / "vault" / "reference" / "scope-taxonomy.md", f"| `{scope}` | {kind} | Container scope |\n")

    context = resolve_project(project, tmp_path / "vault", root)

    assert context.scope is None
    assert context.global_only is True
    assert "broad-ancestor-scope-rejected" in context.diagnostics


def test_own_broad_scope_marker_remains_valid(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    write(root / "CLAUDE.md", "<!-- symbiosis-brain v1: scope=workspace -->")
    write(tmp_path / "vault" / "reference" / "scope-taxonomy.md", "| `workspace` | workspace-root | Container scope |\n")

    context = resolve_project(root, tmp_path / "vault", root)

    assert (context.scope, context.global_only) == ("workspace", False)


def test_product_ancestor_and_missing_taxonomy_fail_open(tmp_path):
    root = tmp_path / "workspace"
    project = root / "nested"
    project.mkdir(parents=True)
    write(root / "CLAUDE.md", "<!-- symbiosis-brain v1: scope=product -->")
    vault = tmp_path / "vault"
    write(vault / "reference" / "scope-taxonomy.md", "| `product` | product | Project scope |\n")

    context = resolve_project(project, vault, root)
    assert (context.scope, context.global_only) == ("product", False)

    write(vault / "reference" / "scope-taxonomy.md", "| `product` | workspace-root | Broad scope |\n")
    broad_context = resolve_project(project, vault, root)
    assert broad_context.global_only is True

    missing_taxonomy = resolve_project(project, tmp_path / "missing-vault", root)
    assert (missing_taxonomy.scope, missing_taxonomy.global_only) == ("product", False)


def test_scope_first_then_legacy_and_newest_handoff_fit_budget(tmp_path):
    vault = tmp_path / "vault"
    write(vault / "CRITICAL_FACTS.md", "# Critical\nSynthetic user fact.\n")
    write(vault / "alpha" / "alpha.md", "# Alpha\nGeometry must stay editable.\n")
    write(vault / "projects" / "alpha.md", "# Legacy Alpha\nWrong fallback.\n")
    write(vault / "alpha" / "decisions" / "design-principles.md", "# Design principles\nKeep geometry editable.\n")
    write(vault / "alpha" / "handoffs" / "2026-10-09.md", "# Handoff 2026-10-09\nResume the survey.\n")
    write(vault / "alpha" / "handoffs" / "2026-10-08.md", "# Handoff 2026-10-08\nStale handoff.\n")
    blocks = build_bootstrap(vault, ScopeContext("alpha", None), char_limit=1000, now=NOW)
    text = "\n".join(block.text for block in blocks)
    assert "Synthetic user fact" in text
    assert "Geometry must stay editable" in text
    assert "Wrong fallback" not in text
    assert "Resume the survey" in text
    assert "Stale handoff" not in text
    assert len(text) <= 1000


def test_legacy_project_card_is_used_when_scope_first_card_is_absent(tmp_path):
    vault = tmp_path / "vault"
    write(vault / "projects" / "alpha.md", "# Alpha\nLegacy project purpose.\n")
    blocks = build_bootstrap(vault, ScopeContext("alpha", None), char_limit=800, now=NOW)
    assert "Legacy project purpose" in "\n".join(block.text for block in blocks)


def test_expired_or_unreadable_notes_are_skipped_without_mutating_vault(tmp_path):
    vault = tmp_path / "vault"
    write(vault / "alpha" / "alpha.md", "---\nvalid_to: 2020-01-01\n---\n# Expired\n")
    before = {
        str(p.relative_to(vault)): p.read_bytes()
        for p in vault.rglob("*")
        if p.is_file()
    }
    blocks = build_bootstrap(vault, ScopeContext("alpha", None), char_limit=1000, now=NOW)
    assert "Expired" not in "\n".join(block.text for block in blocks)
    after = {
        str(p.relative_to(vault)): p.read_bytes()
        for p in vault.rglob("*")
        if p.is_file()
    }
    assert after == before


def test_oversized_card_is_bounded_and_future_or_invalid_markers_degrade(tmp_path):
    vault = tmp_path / "vault"
    write(vault / "alpha" / "alpha.md", "# Alpha\n" + "x" * 5000)
    blocks = build_bootstrap(vault, ScopeContext("alpha", None), char_limit=300, now=NOW)
    assert sum(len(block.text) for block in blocks) <= 300
    workspace = tmp_path / "workspace"
    project = workspace / "alpha"
    project.mkdir(parents=True)
    write(workspace / "CLAUDE.md", "<!-- symbiosis-brain v9: scope=alpha -->")
    bad = resolve_project(project, vault, workspace)
    assert bad.scope is None and bad.global_only and bad.diagnostics
    write(workspace / "CLAUDE.md", "<!-- symbiosis-brain v1: scope=../../private -->")
    bad = resolve_project(project, vault, workspace)
    assert bad.scope is None and bad.global_only and bad.diagnostics


def test_bootstrap_reserves_budget_for_handoff_and_hides_future_valid_from(tmp_path):
    vault = tmp_path / "vault"
    write(vault / "CRITICAL_FACTS.md", "# Critical\n" + "critical detail " * 500)
    write(
        vault / "alpha" / "alpha.md",
        "---\nvalid_from: 2099-01-01\n---\n# Future\nDo not include this claim.\n",
    )
    write(
        vault / "alpha" / "handoffs" / "2026-10-09.md",
        "# Handoff 2026-10-09\nResume the current design review.\n",
    )
    blocks = build_bootstrap(vault, ScopeContext("alpha", None), char_limit=1000, now=NOW)
    text = "\n".join(block.text for block in blocks)
    assert "Do not include this claim" not in text
    assert "Resume the current design review" in text
    assert len(text) <= 1000


def test_rejected_critical_facts_are_never_reinserted_raw_in_global_only_mode(tmp_path, monkeypatch):
    from symbiosis_brain.adapters.opencode import bootstrap

    vault = tmp_path / "vault"
    write(vault / "CRITICAL_FACTS.md", "---\nscope: global\nvalid_to: 2020-01-01\n---\nEXPIRED FACT\n")
    blocks = build_bootstrap(vault, ScopeContext(None, None, global_only=True), char_limit=2000, now=NOW)
    assert "EXPIRED FACT" not in "\n".join(block.text for block in blocks)
    assert "valid_to" not in "\n".join(block.text for block in blocks)

    write(vault / "CRITICAL_FACTS.md", "---\nscope: global\nvalid_from: 2099-01-01\n---\nFUTURE FACT\n")
    blocks = build_bootstrap(vault, ScopeContext(None, None, global_only=True), char_limit=2000, now=NOW)
    assert "FUTURE FACT" not in "\n".join(block.text for block in blocks)

    monkeypatch.setattr(bootstrap, "parse_note", lambda content: (_ for _ in ()).throw(ValueError("bad synthetic frontmatter")))
    write(vault / "CRITICAL_FACTS.md", "MALFORMED SECRET FACT")
    blocks = build_bootstrap(vault, ScopeContext(None, None, global_only=True), char_limit=2000, now=NOW)
    assert "MALFORMED SECRET FACT" not in "\n".join(block.text for block in blocks)


def test_nested_intent_and_handoff_keep_subsections_and_meaningful_body(tmp_path):
    vault = tmp_path / "vault"
    write(
        vault / "alpha" / "alpha.md",
        "# Alpha\n## Purpose\nKeep geometry editable.\n### Constraints\nPreserve source entities.\n"
        "## Handoff 2026-10-09\n### Shipped\nValidated synthetic geometry.\n"
        "### Next step\nResume the design review.\n## Archive\nOld content.\n",
    )
    blocks = build_bootstrap(vault, ScopeContext("alpha", None), char_limit=2000, now=NOW)
    text = "\n".join(block.text for block in blocks)
    assert "Preserve source entities" in text
    assert "Validated synthetic geometry" in text
    assert "Resume the design review" in text
    assert "Old content" not in text


def test_oversized_intent_keeps_body_not_only_its_heading(tmp_path):
    vault = tmp_path / "vault"
    write(
        vault / "alpha" / "alpha.md",
        "# Alpha\n## Purpose\nMeaningful design intent. " + "detail " * 500
        + "\n## Handoff 2026-10-09\n### Next step\nResume the bounded design review.\n"
        + "Additional detail. " * 100,
    )
    blocks = build_bootstrap(vault, ScopeContext("alpha", None), char_limit=600, now=NOW)
    intent = next(block.text for block in blocks if block.id == "project-intent")
    assert "## Purpose" in intent
    assert "Meaningful design intent" in intent
    handoff = next(block.text for block in blocks if block.id == "latest-handoff")
    assert "Resume the bounded design review" in handoff
    assert len("\n".join(block.text for block in blocks)) <= 600


def test_explicit_foreign_scope_is_rejected_but_missing_legacy_scope_is_allowed(tmp_path):
    vault = tmp_path / "vault"
    write(vault / "CRITICAL_FACTS.md", "---\nscope: beta\n---\nForeign global fact.\n")
    write(vault / "alpha" / "alpha.md", "---\nscope: beta\n---\n## Purpose\nOTHER PROJECT CONTENT\n")
    write(vault / "alpha" / "decisions" / "choice.md", "---\nscope: beta\n---\nForeign decision.\n")
    write(vault / "alpha" / "design-principles.md", "---\nscope: beta\n---\nForeign principles.\n")
    blocks = build_bootstrap(vault, ScopeContext("alpha", None), char_limit=3000, now=NOW)
    text = "\n".join(block.text for block in blocks)
    assert "OTHER PROJECT CONTENT" not in text
    assert "Foreign decision" not in text
    assert "Foreign principles" not in text
    assert "Foreign global fact" not in text
    assert any("scope" in block.text.lower() for block in blocks)

    write(vault / "projects" / "legacy.md", "# Legacy\nPath-local legacy content.\n")
    legacy = build_bootstrap(vault, ScopeContext("legacy", None), char_limit=1000, now=NOW)
    assert "Path-local legacy content" in "\n".join(block.text for block in legacy)


def test_handoff_falls_back_to_newest_applicable_candidate_and_skips_future_card_section(tmp_path, monkeypatch):
    from symbiosis_brain.adapters.opencode import bootstrap

    vault = tmp_path / "vault"
    write(
        vault / "alpha" / "alpha.md",
        "# Alpha\n## Handoff 2099-01-01\n### Next step\nFuture section.\n"
        "## Handoff 2026-10-06\n### Next step\nCard fallback step.\n",
    )
    handoff_dir = vault / "alpha" / "handoffs"
    write(handoff_dir / "2026-10-10.md", "Unreadable candidate.\n")
    write(handoff_dir / "2026-10-09.md", "---\nvalid_to: 2020-01-01\n---\nExpired candidate.\n")
    write(handoff_dir / "2026-10-08.md", "## Handoff 2099-01-01\n### Next step\nFuture file section.\n")
    write(handoff_dir / "2026-10-07.md", "---\nscope: alpha\n---\nApplicable directory fallback.\n")
    write(handoff_dir / "2099-01-01.md", "Future file candidate.\n")

    original = bootstrap._safe_read
    def unread_newest(path, root, diagnostics):
        if path.name == "2026-10-10.md":
            diagnostics.append("bootstrap-file-unavailable")
            return None
        return original(path, root, diagnostics)
    monkeypatch.setattr(bootstrap, "_safe_read", unread_newest)
    blocks = build_bootstrap(vault, ScopeContext("alpha", None), char_limit=3000, now=NOW)
    text = "\n".join(block.text for block in blocks)
    assert "Applicable directory fallback" in text
    assert "Future section" not in text
    assert "Future file candidate" not in text
    assert "Expired candidate" not in text


def test_long_shipped_section_does_not_hide_later_next_step(tmp_path):
    vault = tmp_path / "vault"
    write(
        vault / "alpha" / "alpha.md",
        "# Alpha\n## Purpose\nKeep geometry editable.\n"
        "## Handoff 2026-10-09\n### Shipped\n" + "Completed detail. " * 300
        + "\n### Next step\nResume the geometry review.\n",
    )
    blocks = build_bootstrap(vault, ScopeContext("alpha", None), char_limit=8000, now=NOW)
    handoff = next(block.text for block in blocks if block.id == "latest-handoff")
    assert len(handoff) <= 2000
    assert "Resume the geometry review" in handoff


def test_headings_only_newest_handoff_falls_back_to_older_substantive_section(tmp_path):
    vault = tmp_path / "vault"
    write(
        vault / "alpha" / "alpha.md",
        "## Purpose\nKeep geometry editable.\n"
        "## Handoff 2026-10-10\n### Shipped\n\n"
        "## Handoff 2026-10-09\n### Next step\nResume the geometry review.\n",
    )
    blocks = build_bootstrap(vault, ScopeContext("alpha", None), char_limit=8000, now=NOW)
    handoff = next(block.text for block in blocks if block.id == "latest-handoff")
    assert "2026-10-09" in handoff
    assert "Resume the geometry review" in handoff
    assert "2026-10-10" not in handoff
