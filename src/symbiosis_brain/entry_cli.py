"""Lightweight console entry; hook budgets start before installer imports."""
from __future__ import annotations

import sys


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "codex-hook":
        from importlib import import_module

        backend = import_module("symbiosis_brain.adapters.codex.supervisor")
        sys.exit(backend.run_cli(sys.argv[2:]) or 0)
    from symbiosis_brain.install_cli import main as install_main

    return install_main()
