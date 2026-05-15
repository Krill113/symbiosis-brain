# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
