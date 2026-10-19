"""Private subprocess entry point for the bounded Codex hook worker."""
from __future__ import annotations

import sys
from collections.abc import Sequence

from symbiosis_brain.adapters.codex.supervisor import worker_main


def main(argv: Sequence[str] | None = None) -> int:
    return worker_main(sys.argv[1:] if argv is None else argv)


if __name__ == "__main__":
    raise SystemExit(main())
