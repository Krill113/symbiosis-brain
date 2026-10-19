"""Public Python entry points for managing the OpenCode adapter."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from importlib.resources import files
from pathlib import Path


def _config_dir(explicit: str | None = None) -> tuple[Path, str | None]:
    configured_file = os.environ.get("OPENCODE_CONFIG")
    if explicit:
        path = Path(explicit).expanduser()
        return (path.parent, path.name) if path.suffix.lower() in {".json", ".jsonc"} else (path, None)
    if configured_file:
        path = Path(configured_file).expanduser()
        return path.parent, path.name
    if os.environ.get("OPENCODE_CONFIG_DIR"):
        return Path(os.environ["OPENCODE_CONFIG_DIR"]).expanduser(), None
    if os.environ.get("XDG_CONFIG_HOME"):
        return Path(os.environ["XDG_CONFIG_HOME"]).expanduser() / "opencode", None
    return Path.home() / ".config/opencode", None


def _resources() -> tuple[Path, Path]:
    package = Path(str(files("symbiosis_brain"))).resolve()
    artifact = package / "opencode"
    skills = package / "skills"
    if not artifact.is_dir() or not skills.is_dir():
        # Source-checkout fallback is opt-in to avoid accidental checkout installs.
        dev_opencode = os.environ.get("SYMBIOSIS_BRAIN_OPENCODE_DEV_RESOURCES")
        candidate = Path(dev_opencode).expanduser().resolve() if dev_opencode else None
        checkout = candidate.parent.parent if candidate else None
        source_skills = checkout / "skills" if checkout else None
        if not candidate or not source_skills or not candidate.is_dir() or not source_skills.is_dir():
            raise RuntimeError("packaged OpenCode adapter resources are missing")
        artifact, skills = candidate, source_skills
    return artifact, skills


def _verify_manifest(artifact: Path) -> Path:
    manifest = json.loads((artifact / "manifest.json").read_text(encoding="utf-8"))
    if manifest.get("schema") != 1 or manifest.get("entrypoint") != "install/main.mjs":
        raise ValueError("invalid-artifact-manifest")
    expected: set[str] = set()
    entries = manifest.get("files", [])
    if not isinstance(entries, list):
        raise ValueError("invalid-artifact-manifest")
    for item in entries:
        if not isinstance(item, dict) or not isinstance(item.get("path"), str):
            raise ValueError("invalid-artifact-manifest")
        rel = Path(item["path"])
        if rel.is_absolute() or ".." in rel.parts:
            raise ValueError("invalid-artifact-manifest")
        target = artifact / rel
        resolved = target.resolve(strict=True)
        if not resolved.is_relative_to(artifact.resolve()) or target.is_symlink():
            raise ValueError("invalid-artifact-manifest")
        data = target.read_bytes()
        if (not isinstance(item.get("size"), int) or not isinstance(item.get("sha256"), str)
                or len(data) != item["size"] or hashlib.sha256(data).hexdigest() != item["sha256"]):
            raise ValueError("artifact-integrity-failed")
        key = rel.as_posix()
        if key in expected:
            raise ValueError("invalid-artifact-manifest")
        expected.add(key)
    if "install/main.mjs" not in expected:
        raise ValueError("invalid-artifact-manifest")
    actual = set()
    for directory, _, filenames in os.walk(artifact, followlinks=False):
        for filename in filenames:
            path = Path(directory) / filename
            relative_path = path.relative_to(artifact).as_posix()
            if relative_path != "manifest.json":
                actual.add(relative_path)
    if actual != expected:
        raise ValueError("invalid-artifact-manifest")
    return artifact / "install/main.mjs"


def _prepare_config_dir(config_dir: Path) -> None:
    absolute = config_dir.absolute()
    for ancestor in (absolute, *absolute.parents):
        if ancestor.is_symlink() or (hasattr(ancestor, "is_junction") and ancestor.is_junction()):
            raise ValueError("unsafe-config-directory")
    missing: list[Path] = []
    probe = absolute
    while not probe.exists():
        missing.append(probe)
        probe = probe.parent
    if probe.is_symlink() or (hasattr(probe, "is_junction") and probe.is_junction()):
        raise ValueError("unsafe-config-directory")
    for child in reversed(missing):
        child.mkdir()
        if child.is_symlink() or (hasattr(child, "is_junction") and child.is_junction()):
            raise ValueError("unsafe-config-directory")


def _run_impl(command: str, args: argparse.Namespace) -> int:
    config_dir, explicit_name = _config_dir(getattr(args, "config_dir", None))
    artifact, skills = _resources()
    request: dict[str, object] = {"command": command}
    if command in {"install", "plan"}:
        vault = Path(args.vault).expanduser().resolve()
        if not vault.is_dir():
            print("OpenCode install failed: vault directory does not exist", file=sys.stderr)
            return 2
        python = str(Path(sys.executable).resolve())
        package = Path(str(files("symbiosis_brain"))).resolve()
        input_data = {
            "configDir": str(config_dir.resolve()), "artifactDir": str(artifact.resolve()),
            "pythonExe": python, "vault": str(vault), "packageDir": str(package),
            "skillsDir": str(skills.resolve()),
            "mcpCommand": [python, "-c", "from symbiosis_brain.install_cli import main; main()", "serve", "--vault", str(vault)],
        }
        request.update(command="install" if command == "install" else "plan", input=input_data)
    else:
        request["input"] = {"configDir": str(config_dir.resolve())}
    if explicit_name:
        # The current installer contract manages the canonical opencode.json/jsonc pair.
        # Never silently edit another explicitly selected file.
        if explicit_name not in {"opencode.json", "opencode.jsonc"}:
            print("OpenCode install failed: OPENCODE_CONFIG must name opencode.json or opencode.jsonc", file=sys.stderr)
            return 2
        if explicit_name == "opencode.json" and (config_dir / "opencode.jsonc").exists():
            print("OpenCode install failed: OPENCODE_CONFIG selects a lower-precedence config file", file=sys.stderr)
            return 2
    if command in {"install", "plan"}:
        _prepare_config_dir(config_dir)
    node = getattr(args, "node", None) or "node"
    try:
        entrypoint = _verify_manifest(artifact)
    except (OSError, ValueError, KeyError, TypeError):
        print("OpenCode installer artifact is missing or failed integrity verification", file=sys.stderr)
        return 2
    try:
        version = subprocess.run([node, "--version"], text=True, encoding="utf-8", capture_output=True,
                                  check=False, timeout=10)
        match = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)", version.stdout.strip())
        if version.returncode or not match or tuple(map(int, match.groups())) < (22, 6, 0):
            print("OpenCode installer requires Node.js 22.6 or newer", file=sys.stderr)
            return 2
        proc = subprocess.run([node, str(entrypoint)], input=json.dumps(request), text=True, encoding="utf-8",
                              capture_output=True, check=False, timeout=120)
    except (OSError, subprocess.TimeoutExpired):
        print("OpenCode installer could not start or timed out", file=sys.stderr)
        return 2
    if proc.stdout:
        # The Node protocol is a single structured result; don't print config contents.
        try:
            result = json.loads(proc.stdout)
        except ValueError:
            print("OpenCode installer returned an invalid response", file=sys.stderr)
            return 2
        print(json.dumps(result, ensure_ascii=False))
    if proc.returncode:
        error_code = "operation-failed"
        try:
            response = json.loads(proc.stderr)
            candidate = response.get("error") if isinstance(response, dict) else None
            if isinstance(candidate, str) and re.fullmatch(r"[a-z0-9:_-]{1,160}", candidate, re.IGNORECASE):
                error_code = candidate
        except ValueError:
            pass
        print(f"OpenCode installer operation failed ({error_code})", file=sys.stderr)
    return proc.returncode


def _run(command: str, args: argparse.Namespace) -> int:
    try:
        return _run_impl(command, args)
    except Exception:
        print("OpenCode installer could not resolve its inputs", file=sys.stderr)
        return 2


def cmd_setup(args: argparse.Namespace) -> int:
    return _run("install", args)


def cmd_doctor(args: argparse.Namespace) -> int:
    return _run("doctor", args)


def cmd_uninstall(args: argparse.Namespace) -> int:
    return _run("remove", args)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="symbiosis-brain opencode")
    subparsers = parser.add_subparsers(dest="command", required=True)
    setup = subparsers.add_parser("setup", help="Install the OpenCode adapter")
    setup.add_argument("--vault", required=True)
    setup.add_argument("--repair", action="store_true")
    setup.add_argument("--config-dir")
    setup.add_argument("--node")
    setup.set_defaults(func=cmd_setup)
    for name, func in (("doctor", cmd_doctor), ("uninstall", cmd_uninstall)):
        child = subparsers.add_parser(name)
        child.add_argument("--config-dir")
        child.add_argument("--node")
        child.set_defaults(func=func)
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
