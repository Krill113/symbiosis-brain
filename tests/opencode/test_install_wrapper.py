from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from symbiosis_brain.adapters.opencode import install


def _make_artifact(tmp_path: Path) -> tuple[Path, Path]:
    artifact = tmp_path / "resource" / "opencode"
    entry = artifact / "install" / "main.mjs"
    entry.parent.mkdir(parents=True)
    data = b"// synthetic installer entry\n"
    entry.write_bytes(data)
    (artifact / "manifest.json").write_text(json.dumps({
        "schema": 1, "entrypoint": "install/main.mjs", "files": [{
            "path": "install/main.mjs", "sha256": hashlib.sha256(data).hexdigest(), "size": len(data),
        }],
    }), encoding="utf-8")
    skills = tmp_path / "skills"
    skills.mkdir()
    return artifact, skills


def test_config_resolution_honors_explicit_filename_and_environment(tmp_path, monkeypatch):
    selected = tmp_path / "custom" / "opencode.jsonc"
    monkeypatch.setenv("OPENCODE_CONFIG", str(selected))
    monkeypatch.setenv("OPENCODE_CONFIG_DIR", str(tmp_path / "ignored"))
    assert install._config_dir() == (selected.parent, selected.name)
    assert install._config_dir(str(tmp_path / "explicit")) == (tmp_path / "explicit", None)


def test_wrapper_sends_stdin_json_and_no_secrets_in_arguments(tmp_path, monkeypatch):
    artifact, skills = _make_artifact(tmp_path)
    entry = artifact / "install" / "main.mjs"
    vault = tmp_path / "vault"
    vault.mkdir()
    package = tmp_path / "package"
    package.mkdir()
    monkeypatch.setattr(install, "_resources", lambda: (artifact, skills))
    monkeypatch.setattr(install, "_verify_manifest", lambda _artifact: entry)
    calls = []

    class Result:
        returncode = 0
        stdout = '{"ok":true}\n'
        stderr = ""

    def run(command, **kwargs):
        calls.append((command, kwargs))
        if command[-1] == "--version":
            version = type("Version", (), {"returncode": 0, "stdout": "v24.0.0\n", "stderr": ""})()
            return version
        return Result()

    monkeypatch.setattr(install.subprocess, "run", run)
    monkeypatch.setattr(install, "files", lambda _name: package)
    monkeypatch.setattr(install.sys, "executable", str(tmp_path / "python.exe"))
    args = argparse.Namespace(vault=str(vault), config_dir=str(tmp_path / "config"), node="node", repair=False)
    assert install.cmd_setup(args) == 0
    assert len(calls) == 2  # executable version probe, then JSON-over-stdin request
    command, options = calls[1]
    assert command == ["node", str(entry)]
    payload = json.loads(options["input"])
    assert payload["input"]["vault"] == str(vault.resolve())
    assert str(vault) not in " ".join(command)


def test_old_node_is_rejected_without_launching_artifact(tmp_path, monkeypatch, capsys):
    artifact, skills = _make_artifact(tmp_path)
    monkeypatch.setattr(install, "_resources", lambda: (artifact, skills))
    monkeypatch.setattr(install, "_verify_manifest", lambda _artifact: artifact / "install" / "main.mjs")

    class Result:
        returncode = 0
        stdout = "v20.0.0\n"
        stderr = ""

    monkeypatch.setattr(install.subprocess, "run", lambda *_a, **_k: Result())
    args = argparse.Namespace(config_dir=None, node="node")
    assert install.cmd_doctor(args) == 2
    assert "requires Node.js" in capsys.readouterr().err


def test_artifact_manifest_detects_tampered_payload(tmp_path):
    artifact, _ = _make_artifact(tmp_path)
    (artifact / "install" / "main.mjs").write_text("changed", encoding="utf-8")
    try:
        install._verify_manifest(artifact)
    except ValueError as error:
        assert str(error) == "artifact-integrity-failed"
    else:
        raise AssertionError("tampered artifact was accepted")


def test_explicit_checkout_resource_fallback_uses_repo_skills(tmp_path, monkeypatch):
    checkout = tmp_path / "checkout"
    artifact = checkout / "opencode"
    (artifact / "dist").mkdir(parents=True)
    (checkout / "skills").mkdir()
    package = tmp_path / "installed-package"
    package.mkdir()
    monkeypatch.setattr(install, "files", lambda _name: package)
    monkeypatch.setenv("SYMBIOSIS_BRAIN_OPENCODE_DEV_RESOURCES", str(artifact / "dist"))
    assert install._resources() == (artifact / "dist", checkout / "skills")
