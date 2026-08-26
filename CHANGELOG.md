# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- **Action-recall (Stage 1): warnings from past mistakes fire at the moment of a risky Bash/PowerShell command**, not just against prompt text. `class:"action"` routes in `tool-routing.local.json` compile to `<vault>/.index/action-rules.tsv` plus a per-tool fast-reject pattern file (regex-validated against their own test vectors via `grep -E`), matched by a pure-bash block in the PreToolUse hook before the uv/python path even runs — a single combined `grep -qEf` fork rejects the common non-matching case instead of one fork per rule row. PowerShell now gets the same recall coverage Bash already had — **existing installs need one `symbiosis-brain setup claude-code --repair` (or a fresh `doctor` run, which now flags a stale matcher) to pick up the widened PreToolUse matcher.** A rule with no `test_match` vectors on a tool side is now dropped at compile time instead of shipping unvalidated.
- **Routing hints now fire on a subagent's prompt, not just yours.** `Task`/`Agent` calls match `augment`/`supersede` routes against the subagent's own prompt (a 4000-character window, wider than the recall query) and inject up to `routing_cap` hints before the subagent starts — the memory a subagent needs most is the memory it never had.
- **`brain-autolearn` ships as a product skill**, with both of its reference recipes. `brain-save` Step 0 used to name skills that were not part of the install; the second pass (`brain-self-critique`) is now explicitly optional and skipped in silence when absent.
- **`doctor` reports the SQLite build and can verify the database.** A vulnerable build (WAL-Reset race, fixed upstream in 3.51.3 and backported to 3.50.7) is called out by name; `doctor --deep` runs `PRAGMA quick_check`. `serve` records the same version line in `<vault>/.index/serve.log`.
- **`brain_lint` gained a `not_indexed` category** — markdown on disk that never made it into the database — and reports forward references (`[[forward:X]]`) separately from broken links.
- **A `/brain-sync` slash command** is now part of the package instead of living only on the maintainer's machine.
- **A GitHub Release is created from the tag** by `publish.yml`, with notes from `docs/release-notes/vX.Y.Z.md` or the matching CHANGELOG section — but only after the PyPI install has been verified.

### Fixed
- The action-rule matcher now honors `enabled: false` and `matchers` in `~/.claude/symbiosis-brain-pre-action.json` (previously only the `SYMBIOSIS_BRAIN_PRE_ACTION_DISABLED` env var could turn it off), decodes JSON string escaping (`\n`/`\"`/`\\`) out of the command before matching so multi-line commands and heredocs are actually visible to the matcher, and tolerates whitespace after `"command":` the same way the adjacent extractions already did.
- **Sessions started with `--resume` or `/clear` get their context back.** SessionStart was registered for `startup` and `compact` only, so a resumed session had no scope, no CRITICAL_FACTS and no session bridge — and the Stop-hook delta-guard measured from zero for the rest of it.
- **The last-save marker is written by a PostToolUse hook**, not by the skill, and takes the session id from the hook's own stdin. It also survives `/compact`, which used to wipe it and make the next threshold fire unconditionally.
- **The status-line data bridges are exported unconditionally.** Setting your own status line silently disabled save reminders and the rate-limit bridge, because both exports lived in the default row-1 script.
- **The vault sync pulls before it pushes.** SessionEnd was push-only: once the remote moved ahead, every later push failed into a log file nobody read while the hook kept returning 0. It now rebases with autostash, aborts on any conflict outside `log.md`, and raises a marker that both the next session start and the status line show.
- **Wiki links survive handoff rotation.** The archive index truncated snippets mid-`[[link]]`, which made the card partly uneditable and let the unterminated bracket swallow the next line of the index; the link regex no longer spans newlines.
- **`brain_patch` and `brain_append` no longer demand a `gist` they have no parameter for.** A note without a gist gets a soft warning; errors name the tool that was actually called.
- **`[[namespace:skill]]` references are no longer reported as broken.** External namespaces are recognised against the scope taxonomy (cached, so the write path does not re-read it per note).
- **One malformed note no longer aborts the whole vault sync** — it is reported by path in the `failed` list, and `MEMORY.md` is no longer indexed as a note.
- **Balanced index drift is detected and repaired at startup** (equal numbers of missing vectors and orphan rows previously slipped past the count gate), lock cleanup tolerates `OSError`, and unquoted dates from frontmatter no longer reach SQLite as `datetime.date`.
- **Prompt recall deduplicates and filters.** The `UserPromptSubmit` path had no dedup at all and ignored `excluded_note_types`; it now reuses the pre-action pipeline with its own seen-store and backfills fresh hits instead of just dropping repeats.
- **Action rules validate per side, not per pattern.** A rule with two patterns on one tool side was dropped even though the patterns covered every test vector together; a pattern that catches nothing is now reported in `meta.json → unmatched_patterns` as a warning.

### Changed
- **`brain_lint` prints a short report by default.** Stylistic findings (orphans, weak links, gist too long, gist equals title, forward refs) move behind `brain_lint(verbose=true)` — but every counter stays in the summary line in both modes, so nothing goes quiet.
- **The installer speaks English.** All user-facing output of `setup`, `doctor` and `uninstall` was partly Russian.
- **`--repair` keeps the three newest `.bak.*` copies** of each file it replaces instead of accumulating one per run forever, and it now refreshes every skill directory — including reference files, which it used to skip.
- **The installer registers a seventh hook event, `PostToolUse`** (the save marker), and stopped overwriting hook entries it does not own. `--repair` used to replace the whole list for each of its events, so a third-party hook on the same event vanished without a word; it now replaces only its own entries — matched by the name of the hook script in the command, so the same entry is recognised whatever path prefix it carries — and leaves everything else untouched.

## [0.5.0] — 2026-08-11

### Fixed
- **`serve` no longer re-embeds the whole vault when the index has drifted.** A count-drift safety net called `index_all()` whenever the note count and the vector count disagreed by even one, and fastembed's silent default `batch_size=256` let the ONNX arena grow without bound while it did. Measured on a 1300-note vault: two servers at **11 GB private commit and ~750 s of CPU each** on startup, against 506 MB / 6 s for the same package a day earlier. Drift is now repaired incrementally — only the affected notes are re-embedded — and the batch size is capped. The same vault now starts in **0.2–2.5 s at ~296 MB**.
- **Concurrent cold starts no longer multiply that work.** Nothing serialized reindexing between processes, so two editor windows opening at once each cleared `notes_vec` under the other and re-did the whole job. A single-flight lock file now lets one process do the work while the others wait, with a stale-lock break that re-stats before unlinking so a live holder is never displaced.
- **Renames, deletes, handoff rotation and hook-driven writes keep the vector index in sync.** Those paths updated the note store without touching `notes_vec`, which is what produced the drift the safety net then over-corrected for.
- **`brain_sync` indexes the diff.** It re-embedded every note on every call; a full re-embed now happens only under `full=true`, which doubles as the escape hatch if an index is ever genuinely beyond repair.
- **The subagent tool's rename from `Task` to `Agent` no longer silently disables hooks.** The PreToolUse matcher, the pre-action recall query builder and the routing tool set matched the old name only, so on newer Claude Code clients memory hits and route hints stopped firing for subagent calls. Both names are accepted now.

### Added
- **A rotating log for `serve` at `<vault>/.index/serve.log`.** Diagnosing the startup blow-up above meant reconstructing 750 seconds of work from Task Manager, because the server wrote no log anywhere. Index repair, lock acquisition and shutdown are now recorded with note counts and timings. A bad `SB_LOG_LEVEL` degrades to the default instead of killing the server, and several processes sharing one log file tolerate each other's rollover.

### Changed
- **`test_brain_save_trigger_routing.py` now runs on windows-latest** instead of being `--ignore`d there — the first Windows CI coverage these hook tests have ever had. They shelled out to bash via a bare `subprocess.run(["bash", ...])`, which on windows-latest resolves to the WSL stub in `System32` rather than Git Bash. The suite now resolves an absolute, health-checked Git-for-Windows path (the `bin\` wrapper, which provisions a POSIX PATH — the raw `usr\bin\bash.exe` does not) and, on CI, fails loudly rather than skipping if none is found.
- **CI scans the full git history for leaked secrets on every push and pull request** (gitleaks, default rules).

## [0.4.3] — 2026-08-05

### Added
- `SB_PERMISSIONS` now covers all thirteen MCP tools. `brain_rename`, `brain_delete` and `brain_rotate_handoffs` were shipping without an entry, so the first call to any of them hit a permission prompt right after setup reported success. A test pins the list against the server's `list_tools()` — nothing did before, and `doctor` was never a safety net here: it only asserts the list holds at least seven entries, which the incomplete list satisfied.
  - **Worth knowing before you upgrade:** `brain_delete` removes the note file outright — no trash, no backup. Its default `mode="safe"` refuses while other notes still link to the target, and a vault kept under git can always restore, but from now on a fresh setup auto-approves that call.

### Fixed
- Setup no longer dies when the vault's mode cannot be changed. Mounts without POSIX metadata (CIFS, drvfs, some FUSE/NFS) make `chmod` raise, and `scaffold_vault` is the first statement inside setup's try block — unguarded, it took the whole install down before anything had been touched.

### Changed
- CI parses every shipped hook with `bash -n` on both runners. At the time, the cases that actually execute the hooks were skipped on the Windows runner (a bare `bash` there is the WSL stub), so without this a syntax error could reach a Windows user through a green build. (Windows coverage for those cases was restored in an unreleased fix — see [Unreleased] → Fixed.)
- The setup tests no longer point at the real `~/.claude/hooks`: the mock returned a tilde path that setup expanded into the developer's own home directory.

## [0.4.2] — 2026-08-05

### Fixed
- **The wheel now carries `hooks/`, `skills/` and `templates/`.** They were listed for the sdist only, so the documented quickstart — `uv tool install symbiosis-brain` then `symbiosis-brain setup claude-code` — died on `templates/vault-readme.md` for anyone not installing from a git checkout, on every OS. All three resource dirs now resolve through one helper that handles the wheel layout and a dev checkout alike.
- **`mcp` is capped below 2.0.** The 2.x line removed the decorator API (`@app.list_tools()` / `@app.call_tool()`) this server is built on, and a fresh resolve was already picking it up. The port to 2.x is tracked separately.
- **Setup no longer claims success when the package is incomplete.** A missing skill or hook now raises before the MCP server is registered, so the existing rollback runs instead of printing a final "done" over a half-installed state.
- **Headless setup explains itself** instead of raising a bare `EOFError` when there is no interactive stdin to ask for the vault path.
- **The status line clock is resolved once per render**, with a fallback for bash 3.2 — macOS's stock shell, where `$EPOCHSECONDS` does not exist and the rate-limit snapshot came out as invalid JSON.
- **The JSON extractors in `brain-save-trigger.sh` fall back to `python3`.** Distributions that follow PEP 394 ship no unversioned `python`, and the failures were swallowed by `2>/dev/null` — memory hits and route hints came back empty on every prompt with nothing to show for it.

### Security
- The vault directory is created with mode `0700` on POSIX. Under a typical umask it was `0755`, leaving personal notes readable by every local user. Windows is untouched — there it is ACLs, not mode bits.

### Changed
- CI: a new `test.yml` runs pytest, the bash hook suites and an install-smoke — build the wheel, install it into a venv outside the checkout, then run setup, doctor and serve — on ubuntu-latest and windows-latest. A plain import smoke cannot catch a missing `templates/vault-readme.md`; this does.
- The release smoke now imports `symbiosis_brain.server` instead of the version-only `__init__`, so it actually exercises the dependency graph.
- Dropped the `Operating System :: OS Independent` classifier until CI has earned it back.

## [0.4.1] — 2026-08-04

### Fixed
- **Status line no longer leaks suspended `bash.exe` orphans on Windows.** Claude Code re-runs the status line on every event with a 300 ms debounce and cancels the in-flight script when a new event arrives. The chain took ~3.0 s per render — every value went through a `grep`/`sed`/`cut`/`seq`/`date` pipeline, and each fork+exec costs ~86 ms under MSYS — so the cancel landed on nearly every render, and a child caught mid-`fork()` is created suspended and never resumed. All three scripts (`sb-statusline.sh`, `sb-base-statusline.sh`, `sb-line.sh`) are now fork-free: bash regex instead of `grep`/`sed`, `printf -v` plus parameter expansion instead of `seq`, `$EPOCHSECONDS` instead of `date`, `read <` instead of `cat`. The wrapper `source`s its own scripts instead of piping into a fresh bash. **3033 ms → 124 ms per render**, output verified byte-identical across five input sets.
- The model's effort level never appeared in the status line — the pattern required `"effortLevel":"` with no space, while `settings.json` is formatted with one.
- Progress bars drew an extra glyph at 0 % and 100 %: `printf '█%.0s' $(seq 1 0)` emits its format once even when given zero arguments.

### Added
- `SYMBIOSIS_BRAIN_RATE_LIMITS_FILE` — opt-in path for a per-tick JSON snapshot of the session's rate limits, for limit-watcher agents. Unset by default; nothing is written unless you set it.

## [0.4.0] — 2026-06-24

### Added
- **Stage-4 tool routing** — a data-driven routing engine (load/merge, when-gate, priority cap, dedup, Tier-0/Tier-1 event appenders) with a seed catalog of tool hints. On each prompt the `UserPromptSubmit` hook composes a `[route]` advisory; routing folds into the `search-gist` envelope under an opt-in flag.
- `brain-tools` skill — onboards per-install MCP/tool routes (stored in `$VAULT/tool-routing.local.json`, git-ignored and never indexed) without editing package code; registered by the installer.
- Stage-4b routes for Serena / Civil3D / VS-MCP (fixture + gate tests), plus a Serena pre-edit advisory that surfaces dependency awareness before `Edit`/`Write`.
- Pre-action recall hardening — dedup + relevance metadata on the `PreToolUse` hook, a space-tolerant Tier-1 parse, and a shared seen-store so recall and routing reuse the same dedup.
- Write-time DX — code-region skip validator, heading-append, live-lint orphan resolution, and a write counter.
- `brain-save` retrospective now scans for routing gaps; it writes a `brain-last-save-pct` marker so the Stop-hook delta-guard works across turns.
- Vendored `hooks/brain-sync.sh` (SessionEnd vault `git add/commit/push`, soft-fail) into the repo — previously it was deploy-only and untracked.
- `brain-pre-action-trigger.sh` is now shipped by the installer; `brain_append` + `brain_patch` added to the default permission set.
- `tests/test-stop-hook.sh` — bash coverage for save-trigger stop/precompact zones, env thresholds, delta-guard and SAVE_LATER (previously covered only by the removed python-shim tests).
- Expanded README — tool routing, the full hook / tool / skill reference, a `SYMBIOSIS_BRAIN_*` configuration table, contributing guidelines, and license.

### Changed
- Hooks are now **bash-only** — a single source of truth matching the live `~/.claude` install. The installer wires all six hook events (`SessionStart` startup+compact, `Stop`, `PreCompact`, `UserPromptSubmit`, `PreToolUse` recall, `SessionEnd` sync) to the `.sh` hooks, and seeds the behavioural `SYMBIOSIS_BRAIN_*` env block (non-clobbering — existing user values are preserved).
- `SessionStart` resolves the active scope from the `CLAUDE.md` marker (Layer 2).
- Packaging — ship `tool-routing.json` in the wheel; unify the hook temp-path on `${TMPDIR:-${TEMP:-/tmp}}`; handoff rotation resolves the project card by frontmatter scope, not filename.

### Fixed
- UTF-8 emission in the `brain-save` python extractors and `search-gist` on Windows (cp1251 byte `0xe4` / lone-surrogate fail-open that could drop both memory and routing on an affected prompt).

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
