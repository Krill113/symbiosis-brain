# Concurrency reproducer

These scripts reproduce the parallel cold-start / write-contention scenario
that motivated the 2026-05-06 concurrency-safety work.

## Usage

```bash
# Copy the production vault to a temp location (don't touch live data)
cp -r ~/.../symbiosis-brain-vault /tmp/repro-vault

# Warm the vault once (registers embedding_model, builds index if dirty).
# This is a one-time event after upgrading; sessions opened thereafter are fast.
REPRO_VAULT=/tmp/repro-vault \
  .venv/Scripts/python.exe tools/repro/repro_runner.py 1 tools/repro/repro_t1_full_init.py

# Now run N parallel cold-starts of the full _init path
REPRO_VAULT=/tmp/repro-vault \
  .venv/Scripts/python.exe tools/repro/repro_runner.py 5 tools/repro/repro_t1_full_init.py
```

## Measured (post-fix, 2026-05-06)

After single-process warm-up to register the embedding model, parallel `_init` runs
exercise the bootstrap-by-inference fast path — no re-embed, no write contention.

| N | Wall clock (max worker) | `init_s` (median) | Result |
|---|---|---|---|
| 1 (cold, registers model) | 3.7 s | 2.1 s | ok — model registered, first search 1.3 s |
| 3 (warm) | 2.3 s | 0.7 s | all 3 ok, zero "database is locked" |
| 5 (warm) | 3.2 s | 0.9 s | all 5 ok, zero "database is locked" |

## Pre-fix baseline (recorded 2026-05-06)

The original failure: every cold-start did a full re-embed of all notes (~60 s),
holding the SQLite write lock the whole time. Concurrent processes timed out
on the default 5 s `busy_timeout`.

| N | Result |
|---|---|
| 1 | 63.2 s — full re-embed of 395 notes |
| 3 | 1 succeeds (55 s), 2 crash with `OperationalError: database is locked` after ~6 s |

## First-time-after-upgrade caveat

If `schema_version[embedding_model]` is unset (first run after upgrading from
pre-fix code) AND `notes_vec` is inconsistent with `notes`, the bootstrap
path runs a one-time full re-embed (~50 s for ~400 notes). Multiple parallel
processes hitting this state simultaneously serialize via `BEGIN IMMEDIATE` —
the first process wins; later ones may exceed the 30 s `busy_timeout` while
waiting. **Mitigation:** open a single Claude Code session first to warm the
vault; subsequent parallel sessions are fast.

If `notes_vec` is already consistent with `notes` (the typical legacy upgrade
case — old code re-indexed every startup, so existing entries are valid for
the current model), bootstrap-by-inference skips the re-embed: just registers
the model name. Sub-second.

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
4. After ~30 s, run `brain_status` in one window — `WAL pages pending`
   should be small (<50), `Vector index in sync: yes`.
