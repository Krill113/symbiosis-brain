"""Cross-client source distribution contract (synthetic, no build side effects)."""
from pathlib import Path
import tomllib

ROOT = Path(__file__).resolve().parents[1]


def test_console_entry_keeps_hook_import_boundary_lightweight():
    config = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert config["project"]["scripts"]["symbiosis-brain"] == "symbiosis_brain.entry_cli:main"


def test_wheel_has_offline_opencode_artifact_mapping():
    config = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    wheel = config["tool"]["hatch"]["build"]["targets"]["wheel"]
    assert wheel["force-include"]["opencode/dist"] == "symbiosis_brain/opencode"


def test_sdist_keeps_artifact_and_public_guides_for_wheel_rebuild():
    config = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    sdist = config["tool"]["hatch"]["build"]["targets"]["sdist"]
    assert sdist["force-include"]["opencode/dist"] == "opencode/dist"
    assert "opencode" in sdist["include"]
    assert "docs/codex-quickstart.md" in sdist["include"]
    assert "opencode/node_modules" in sdist["exclude"]


def test_manifest_artifact_bytes_survive_windows_autocrlf_checkout():
    attributes = (ROOT / ".gitattributes").read_text(encoding="utf-8")
    assert "opencode/** text eol=lf" in attributes.splitlines()
