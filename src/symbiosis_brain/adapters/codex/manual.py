"""Render hook configuration on stdout; never install or mutate user profiles."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import shlex
import sys
from typing import Sequence

from symbiosis_brain.install_lib import packaged_dir


def _checked_path(value: Path) -> Path:
    path = Path(value)
    if not path.is_absolute():
        raise ValueError("hook paths must be absolute")
    # A deliberately narrow supported shell-path domain, not batch '%' escaping.
    # Reject rather than advertise a cross-shell argv contract we cannot preserve.
    if re.fullmatch(r"[\w .:/\\-]+", str(path)) is None:
        raise ValueError("hook path contains unsupported shell characters")
    return path


def _quote_windows(path: Path) -> str:
    # The pinned native Windows host executes commandWindows in PowerShell.
    # Apostrophes and expansion characters are excluded by _checked_path.
    return "'" + str(path) + "'"


def render_hooks(executable: Path, vault: Path) -> dict:
    """Return a fresh JSON-compatible template, without touching either path."""
    executable = _checked_path(executable)
    vault = _checked_path(vault)
    package_init = Path(__file__).resolve().parents[2] / "__init__.py"
    template = packaged_dir(str(package_init), "templates") / "codex" / "hooks.json"
    result = json.loads(template.read_text(encoding="utf-8"))
    posix = f"{shlex.quote(str(executable))} codex-hook --vault {shlex.quote(str(vault))}"
    windows = f"& {_quote_windows(executable)} codex-hook --vault {_quote_windows(vault)}"
    for groups in result["hooks"].values():
        for group in groups:
            for hook in group["hooks"]:
                hook["command"] = hook["command"].replace("{{COMMAND}}", posix)
                hook["commandWindows"] = hook["commandWindows"].replace(
                    "{{COMMAND_WINDOWS}}", windows
                )
    return result


def run_cli(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m symbiosis_brain.adapters.codex.manual",
        description="Print hook JSON for manual review. No profile files are written.",
    )
    parser.add_argument("operation", choices=["hooks"])
    parser.add_argument("--executable", required=True)
    parser.add_argument("--vault", required=True)
    args = parser.parse_args(argv)
    try:
        output = render_hooks(Path(args.executable), Path(args.vault))
    except (OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(run_cli())
