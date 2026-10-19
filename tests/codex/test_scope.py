from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from symbiosis_brain.adapters.codex.config import TrustedProject
from symbiosis_brain.adapters.codex.scope import resolve_scope, safe_vault_path


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _marker(scope: str, *, version: int = 1) -> str:
    return f"<!-- symbiosis-brain v{version}: scope={scope} -->\n"


def _taxonomy(vault: Path) -> None:
    _write(
        vault / "reference" / "scope-taxonomy.md",
        "| Scope | Type | Purpose |\n"
        "|---|---|---|\n"
        "| `product` | product | product notes |\n"
        "| `umbrella-root` | **зонтик** (cross-ecosystem only) | umbrella notes |\n"
        "| `umbrella-part` | umbrella-member (umbrella-root) | member notes |\n"
        "| `workspace` | workspace-root | container folder |\n",
    )


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
        encoding="utf-8",
        check=False,
    )
    if completed.returncode != 0:
        raise OSError(completed.stderr or completed.stdout)


def _file_link(link: Path, target: Path) -> None:
    link.symlink_to(target)


def test_nearest_claude_marker_uses_canonical_parser(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    project = workspace / "demo"
    cwd = project / "nested"
    cwd.mkdir(parents=True)
    _write(workspace / "CLAUDE.md", _marker("parent"))
    _write(project / "CLAUDE.md", _marker("demo"))

    result = resolve_scope(cwd, tmp_path / "vault", ())

    assert result.scope == "demo"
    assert result.source == "marker_v1"
    assert result.marker_path == (project / "CLAUDE.md").resolve()
    assert result.depth == 1
    assert result.warning is None
    assert result.trusted is True
    assert result.memory_authorized is True
    assert result.allow_global is True


def test_future_marker_keeps_canonical_scope_and_reports_source(tmp_path: Path) -> None:
    project = tmp_path / "demo"
    project.mkdir()
    _write(project / "CLAUDE.md", _marker("future-scope", version=2))

    result = resolve_scope(project, tmp_path / "vault", ())

    assert result.scope == "future-scope"
    assert result.source == "marker_future"
    assert result.warning is None


def test_markerless_nearest_claude_uses_that_project_basename(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    project = workspace / "Demo_Project"
    cwd = project / "nested"
    cwd.mkdir(parents=True)
    _write(workspace / "CLAUDE.md", _marker("parent"))
    _write(project / "CLAUDE.md", "# project instructions without marker\n")
    _write(cwd / "AGENTS.md", _marker("agents-must-not-route"))

    result = resolve_scope(cwd, tmp_path / "vault", ())

    assert result.scope == "demo-project"
    assert result.source == "basename"
    assert result.marker_path == (project / "CLAUDE.md").resolve()
    assert result.depth == 1


def test_unconfigured_project_uses_canonical_cwd_basename(tmp_path: Path) -> None:
    project = tmp_path / "Unknown_Project"
    project.mkdir()

    result = resolve_scope(project, tmp_path / "vault", ())

    assert result.scope == "unknown-project"
    assert result.source == "basename"
    assert result.marker_path is None
    assert result.depth == 0
    assert result.warning is None
    assert result.allow_global is True


def test_legacy_trusted_projects_argument_does_not_authorize_or_deny(
    tmp_path: Path,
) -> None:
    project = tmp_path / "demo"
    project.mkdir()
    unrelated = tmp_path / "unrelated"
    unrelated.mkdir()
    legacy = TrustedProject(
        root=unrelated,
        allowed_scopes=frozenset({"other"}),
        allow_global=False,
    )

    result = resolve_scope(project, tmp_path / "vault", (legacy,))

    assert result.scope == "demo"
    assert result.trusted is True
    assert result.memory_authorized is True
    assert result.allow_global is True


def test_marker_scope_is_not_checked_against_adapter_policy(tmp_path: Path) -> None:
    project = tmp_path / "demo"
    project.mkdir()
    _write(project / "CLAUDE.md", _marker("not-in-any-codex-allowlist"))

    result = resolve_scope(project, tmp_path / "vault", ())

    assert result.scope == "not-in-any-codex-allowlist"
    assert result.warning is None


@pytest.mark.parametrize("scope", ["workspace", "umbrella-root"])
def test_broad_ancestor_marker_falls_back_to_global_scope(
    tmp_path: Path,
    scope: str,
) -> None:
    vault = tmp_path / "vault"
    workspace = tmp_path / "workspace"
    cwd = workspace / "product" / "nested"
    cwd.mkdir(parents=True)
    _taxonomy(vault)
    _write(workspace / "CLAUDE.md", _marker(scope))

    result = resolve_scope(cwd, vault, ())

    assert result.scope is None
    assert result.source == "marker_v1"
    assert result.depth == 2
    assert result.warning == "broad ancestor scope rejected"
    assert result.allow_global is True


@pytest.mark.parametrize(
    ("scope", "expected"),
    [
        ("product", "product"),
        ("umbrella-part", "umbrella-part"),
    ],
)
def test_product_and_umbrella_member_ancestor_markers_are_inherited(
    tmp_path: Path,
    scope: str,
    expected: str,
) -> None:
    vault = tmp_path / "vault"
    workspace = tmp_path / "workspace"
    cwd = workspace / "child"
    cwd.mkdir(parents=True)
    _taxonomy(vault)
    _write(workspace / "CLAUDE.md", _marker(scope))

    result = resolve_scope(cwd, vault, ())

    assert result.scope == expected
    assert result.depth == 1
    assert result.warning is None


def test_own_broad_scope_marker_is_honored(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _taxonomy(vault)
    _write(workspace / "CLAUDE.md", _marker("workspace"))

    result = resolve_scope(workspace, vault, ())

    assert result.scope == "workspace"
    assert result.depth == 0
    assert result.warning is None


def test_missing_taxonomy_fails_open_for_inherited_marker(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    cwd = workspace / "child"
    cwd.mkdir(parents=True)
    _write(workspace / "CLAUDE.md", _marker("workspace"))

    result = resolve_scope(cwd, tmp_path / "missing-vault", ())

    assert result.scope == "workspace"
    assert result.depth == 1
    assert result.warning is None


def test_missing_workspace_is_unavailable_without_inventing_trust_policy(
    tmp_path: Path,
) -> None:
    result = resolve_scope(tmp_path / "missing", tmp_path / "vault", ())

    assert result.scope is None
    assert result.source == "none"
    assert result.trusted is False
    assert result.memory_authorized is False
    assert result.warning == "workspace unavailable"


def test_safe_vault_path_rejects_traversal_absolute_and_symlink_escape(
    tmp_path: Path,
) -> None:
    vault = tmp_path / "vault"
    outside = tmp_path / "outside"
    vault.mkdir()
    outside.mkdir()

    assert safe_vault_path(vault, "..", "outside", "secret.md") is None
    assert safe_vault_path(vault, "demo", "..", "other.md") is None
    assert safe_vault_path(vault, "demo/../other.md") is None
    assert safe_vault_path(vault, str((outside / "secret.md").resolve())) is None

    link = vault / "linked"
    try:
        _directory_link(link, outside)
    except OSError as exc:
        pytest.skip(f"directory links unavailable: {exc}")
    assert safe_vault_path(vault, "linked", "secret.md") is None


def test_safe_vault_path_returns_physical_contained_path(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    target = vault / "demo" / "demo.md"
    target.parent.mkdir(parents=True)
    target.write_text("synthetic", encoding="utf-8")

    assert safe_vault_path(vault, "demo", "demo.md") == target.resolve()


def test_secure_read_text_rejects_traversal_and_preexisting_link_escape(
    tmp_path: Path,
) -> None:
    from symbiosis_brain.adapters.codex.secure_io import secure_read_text

    root = tmp_path / "root"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    _write(outside / "secret.md", "outside")

    with pytest.raises((OSError, ValueError)):
        secure_read_text(root, "../outside/secret.md")

    link = root / "linked"
    try:
        _directory_link(link, outside)
    except OSError as exc:
        pytest.skip(f"directory links unavailable: {exc}")

    with pytest.raises(OSError):
        secure_read_text(root, "linked/secret.md")


def test_secure_read_text_reads_the_opened_file_when_path_is_replaced(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from symbiosis_brain.adapters.codex import secure_io

    root = tmp_path / "root"
    target = root / "note.md"
    _write(target, "opened content")
    original_read = secure_io._read_text_from_fd
    replaced = False

    def replace_after_open(fd: int) -> str:
        nonlocal replaced
        if not replaced:
            replaced = True
            target.rename(root / "note.opened.md")
            _write(target, "replacement content")
        return original_read(fd)

    monkeypatch.setattr(secure_io, "_read_text_from_fd", replace_after_open)

    assert secure_io.secure_read_text(root, "note.md") == "opened content"
    assert replaced is True


def test_secure_read_text_rejects_file_symlink_even_inside_root(
    tmp_path: Path,
) -> None:
    from symbiosis_brain.adapters.codex.secure_io import secure_read_text

    root = tmp_path / "root"
    target = root / "target.md"
    link = root / "link.md"
    _write(target, "inside")
    try:
        _file_link(link, target)
    except OSError as exc:
        pytest.skip(f"file symlinks unavailable: {exc}")

    with pytest.raises(OSError):
        secure_read_text(root, "link.md")
