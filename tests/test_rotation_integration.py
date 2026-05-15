"""Integration test against real symbiosis-brain card snapshot."""
import shutil
from pathlib import Path

from symbiosis_brain.rotation import rotate_handoffs, parse_handoff_sections


FIXTURE = Path(__file__).parent / "fixtures" / "symbiosis-brain-card-snapshot-2026-05-15.md"


def test_rotation_on_real_card_snapshot(tmp_path):
    vault = tmp_path / "vault"
    projects_dir = vault / "projects"
    projects_dir.mkdir(parents=True)
    card = projects_dir / "symbiosis-brain.md"
    shutil.copy(FIXTURE, card)

    pre_text = card.read_text(encoding="utf-8")
    pre_sections = parse_handoff_sections(pre_text)
    assert len(pre_sections) >= 7  # was 11 on 2026-05-15

    report = rotate_handoffs(vault=vault, scope="symbiosis-brain", inline_days=2)

    assert report.cards_processed == 1
    assert report.cards_modified == 1
    assert report.sections_archived >= 1

    archive_dir = vault / "archive" / "handoffs"
    for rel_path in report.archive_files_created:
        af = vault / rel_path
        assert af.exists()
        content = af.read_text(encoding="utf-8")
        assert content.startswith("---\n")
        assert "type: project" in content
        assert "scope: symbiosis-brain" in content
        assert "valid_from: " in content
        assert "gist: " in content

    post_text = card.read_text(encoding="utf-8")
    assert len(post_text) < len(pre_text)
    assert "## Archive" in post_text

    # Idempotent
    report2 = rotate_handoffs(vault=vault, scope="symbiosis-brain", inline_days=2)
    assert report2.sections_archived == 0
    assert report2.cards_modified == 0


def test_rotation_lint_clean_post_rotation(tmp_path):
    vault = tmp_path / "vault"
    projects_dir = vault / "projects"
    projects_dir.mkdir(parents=True)
    card = projects_dir / "symbiosis-brain.md"
    shutil.copy(FIXTURE, card)

    rotate_handoffs(vault=vault, scope="symbiosis-brain", inline_days=2)

    archive_dir = vault / "archive" / "handoffs"
    for f in archive_dir.glob("*.md"):
        content = f.read_text(encoding="utf-8")
        assert "title:" in content
        assert "tags:" in content
