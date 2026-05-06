# Concurrency reproducer

These scripts reproduce the parallel cold-start / write-contention scenario
that motivated the 2026-05-06 concurrency-safety work.

## Usage

```bash
# Copy the production vault to a temp location (don't touch live data)
cp -r ~/.../symbiosis-brain-vault /tmp/repro-vault

# Run N parallel cold-starts of the full _init path
REPRO_VAULT=/tmp/repro-vault \
  .venv/Scripts/python.exe tools/repro/repro_runner.py 3 tools/repro/repro_t1_full_init.py
```

## Expected (post-fix)

- N=1: total ≤ 2s (unchanged vault — no re-embed)
- N=3: all 3 succeed within ~3s wall-clock; zero "database is locked" errors
- N=5: all 5 succeed; max worker total ≤ 5s

## Pre-fix baseline (recorded 2026-05-06)

- N=1: 63.2s (full re-embed of 395 notes on every cold-start)
- N=3: 1 succeeds (55s); 2 crash with `OperationalError: database is locked`

## Smoke S1 — parallel agents via bulk-run.ps1

In `..\IterisDiagnostics`:

```powershell
pwsh -File .\tools\bulk-run.ps1 -BatchSize 3 -SampleN 3
```

Expected: all 3 reports produced, zero "database is locked" in any agent's stderr.

## Smoke S2 — three parallel Claude Code sessions

1. Open three Claude Code windows in different scopes (e.g., this repo,
   Iteris-Сети, LazyDesigner).
2. In each, immediately invoke a tool that touches brain (e.g., type a
   prompt requiring brain-recall, or run `/brain-status`).
3. Watch for hangs past 5 seconds. None should occur.
4. After ~30s, run `brain_status` in one window — `WAL pages pending`
   should be small (<50), `Vector index in sync: yes`.
