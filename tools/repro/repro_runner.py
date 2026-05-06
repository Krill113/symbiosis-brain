"""Parallel reproducer runner.

Usage:
  python runner.py <N> <worker_script>
  e.g.: python runner.py 5 worker_t0.py

Spawns N copies of <worker_script> simultaneously using the brain repo's venv python,
collects their JSON output (one line each), reports per-process timings + wall clock.
Hard timeout = 180s per process. If any worker hangs past timeout, marked as TIMEOUT.
"""
import concurrent.futures
import json
import os
import subprocess
import sys
import time
from pathlib import Path

VENV_PY = Path(
    r"C:\Repos\Voznesenskiy\IterisNetwork\My\others\symbiosis-brain\.venv\Scripts\python.exe"
)
TIMEOUT_S = 180


def run_one(worker_path: Path, worker_idx: int) -> dict:
    t_spawn = time.perf_counter()
    try:
        proc = subprocess.run(
            [str(VENV_PY), str(worker_path)],
            capture_output=True,
            text=True,
            timeout=TIMEOUT_S,
            cwd=str(worker_path.parent),
        )
        wall = round(time.perf_counter() - t_spawn, 3)
        if proc.returncode != 0:
            return {
                "worker_idx": worker_idx,
                "wall_s": wall,
                "error": "non-zero exit",
                "rc": proc.returncode,
                "stdout": proc.stdout[-400:],
                "stderr": proc.stderr[-400:],
            }
        out = proc.stdout.strip().splitlines()
        last_json = next((ln for ln in reversed(out) if ln.startswith("{")), "")
        try:
            data = json.loads(last_json)
        except json.JSONDecodeError:
            return {
                "worker_idx": worker_idx,
                "wall_s": wall,
                "error": "no JSON output",
                "stdout": proc.stdout[-400:],
                "stderr": proc.stderr[-400:],
            }
        data["worker_idx"] = worker_idx
        data["wall_s"] = wall
        return data
    except subprocess.TimeoutExpired:
        return {
            "worker_idx": worker_idx,
            "error": f"TIMEOUT after {TIMEOUT_S}s — process hung",
        }
    except Exception as exc:
        return {
            "worker_idx": worker_idx,
            "error": f"{type(exc).__name__}: {exc}",
        }


def main():
    if len(sys.argv) < 3:
        print("Usage: python runner.py <N> <worker_script>", file=sys.stderr)
        sys.exit(2)
    n = int(sys.argv[1])
    worker_path = Path(sys.argv[2]).resolve()
    if not worker_path.exists():
        print(f"Worker script not found: {worker_path}", file=sys.stderr)
        sys.exit(2)

    if not VENV_PY.exists():
        print(f"venv python not found: {VENV_PY}", file=sys.stderr)
        sys.exit(2)

    print(f"Running N={n} parallel workers ({worker_path.name}) ...", file=sys.stderr)
    t0 = time.perf_counter()
    with concurrent.futures.ThreadPoolExecutor(max_workers=n) as ex:
        futures = [ex.submit(run_one, worker_path, i) for i in range(n)]
        results = [f.result() for f in concurrent.futures.as_completed(futures)]
    wall = round(time.perf_counter() - t0, 3)

    results.sort(key=lambda r: r.get("worker_idx", 999))

    summary = {
        "N": n,
        "worker": worker_path.name,
        "wall_clock_s": wall,
        "ok": sum(1 for r in results if "error" not in r),
        "errors": sum(1 for r in results if "error" in r),
        "results": results,
    }
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
