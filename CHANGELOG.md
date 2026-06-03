# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Changed
- Hooks are now **bash-only** — a single source of truth matching the live `~/.claude` install. The installer wires all six hook events (`SessionStart` startup+compact, `Stop`, `PreCompact`, `UserPromptSubmit`, `PreToolUse` recall, `SessionEnd` sync) to the `.sh` hooks, and seeds the behavioural `SYMBIOSIS_BRAIN_*` env block (non-clobbering — existing user values are preserved).

### Added
- Vendored `hooks/brain-sync.sh` (SessionEnd vault `git add/commit/push`, soft-fail) into the repo — previously it was deploy-only and untracked.
- `brain-pre-action-trigger.sh` is now shipped by the installer; `brain_append` + `brain_patch` added to the default permission set.
- `tests/test-stop-hook.sh` — bash coverage for save-trigger stop/precompact zones, env thresholds, delta-guard and SAVE_LATER (previously covered only by the removed python-shim tests).

### Removed
- Lagging Python hook shims `hooks/brain-session-start.py` and `hooks/brain-save-trigger.py` — the bash hooks are canonical; dual maintenance was the source of hook drift.
- `migrate-hooks` CLI command (bash↔python cutover) — obsolete under bash-only.

## [0.3.0] — 2026-05-21

### Added
- B2 handoff rotation: `brain_rotate_handoffs(scope, dry_run, inline_days)` MCP tool — auto-discovers `## Handoff` sections in project cards and archives stale ones into `archive/handoffs/<scope>-<date>[-<slug>].md`. Idempotent, conflict-detecting, concurrency-safe (per-note write lock + atomic writes). `brain-save` invokes it after writing a handoff section.
- Save-trigger thresholds are now configurable via `SYMBIOSIS_BRAIN_SAVE_THRESHOLDS` and `SYMBIOSIS_BRAIN_SAVE_DELTA_GUARD` env vars (previously hardcoded and ignored).

### Changed
- Save-trigger defaults recalibrated `40/70/90` → `25/35/45` (delta-guard `20` → `10`) for the 1M-context envelope, where sessions typically stay in the 0-50% band and quality degrades around 40%. Zone boundaries (soft / serious / last-chance) and the SAVE_LATER window are now derived from the threshold list instead of magic numbers.

### Fixed
- Archive handoff frontmatter: `gist` and `title` values are YAML-quoted to prevent parse errors when they start with `- `, a digit, or contain a colon.
- `VAULT_DIRS` includes `archive/` so fresh-vault init and sync create and track the archive tree.

## [0.2.0] — 2026-05-15

### Added
- Phase 6: concurrency-safety hardening (SQLite WAL, per-note write locks, atomic upsert, fastembed singleton-guard, hooks atomic writes).
- Phase 7: active-recall hardening (UserPromptSubmit hook reliability fix, first-turn rules roster injection, fastembed prewarm).
- Phase 8: hygiene prevention (lint resolver fix, write-time gates with hard-block on broken outgoing wiki-links, `brain_rename` and `brain_delete` MCP tools).
- B1: pre-action recall hook on `Task|Edit|Write|MultiEdit|NotebookEdit|Bash` matchers (auto-injects `[recall: N hits]` before tool execution).
- Round 1 quick fixes: `reference` enum in `brain_write`, malformed forward-link error messages, `doctor` path resolution with spaces and `SYMBIOSIS_BRAIN_VAULT` env-var fallback, marker template in `brain-init`, concurrent test for `brain_patch`, `reference/` folder taxonomy.
- MCP zombie shutdown: parent-process watchdog + graceful server shutdown + `tools/reap-zombies.ps1` cleanup utility (Windows orphan MCP cleanup).
- Q5 hard limit: `gist` ≤ 140 chars validated at write time.

### Changed
- `version` migrated to hatch dynamic source (`src/symbiosis_brain/__init__.py`).
- PyPI publishing automated via GitHub Actions Trusted Publisher.

## [0.1.0] — 2026-05-05

### Added
- Initial public release on PyPI.
