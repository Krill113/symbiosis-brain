# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.7.2] — 2026-10-26

### Changed
- `brain-welcome` uses plain language for non-programmers, a friendly informal tone,
  and a summary of what was understood before saving. The nonexistent `/brain-help`
  command is removed. Storage guidance is neutral: Google Drive, OneDrive or Git
  are optional choices, not a requirement to use Git.
- `brain-autolearn` closes the loop instead of growing the inbox: inbox lines
  carry a status (`candidate` → `ready` → `done`, or `proposed` / `blocked`) and a
  last-seen date; at most one cheap, allowed `ready` item is done in the same pass;
  candidates unseen for 45 days move to an archive section. Mid-session capture of
  expensive failures; retries of one operation no longer count as repetitions.
- `brain-autolearn` learns about the owner's own tools: a registry of in-house
  tools and a per-tool ledger (use, friction with cost and workaround, improvement
  ideas). A data-loss or safety defect is recorded on first sight; recurring friction
  becomes a proposed task for that tool. Guards inside the tool are preferred over
  teaching the agent to dodge the defect.

## [0.7.1] — 2026-10-19

### Added
- OpenCode integration: global memory server plugin, canonical Brain
  skills and command wrappers, a separate minimalist TUI plugin, and client setup,
  diagnostics and ownership-aware removal. OpenCode Git synchronization remains
  a visible deferred command, not an index-refresh alias.
- Codex integration: native lifecycle hooks with project/global hybrid
  recall, canonical skills with small native-client branches, and a semi-manual
  setup guide. Automatic Codex setup/repair/uninstall is deferred.
- Client-neutral `init-vault --vault PATH`, read-only `resources --skills` and
  `resources --routing`, and stdout-only `codex-hooks` configuration rendering.
- Offline OpenCode artifact assets in wheels and source distributions, with
  source/artifact hashes and an independent CI freshness check.

### Changed
- The console entry is lightweight for Codex hook supervision; existing Claude
  commands remain on their original implementation path.
- Client-specific invocation and path branches preserve the existing bootstrap,
  recall, save, retrospective and autolearn procedures in the canonical skills.

### Fixed
- Windows Codex state ACL verification compares resolved trustee SIDs and exact
  access rules instead of literal SDDL spelling, accepting the local administrator
  alias only when it resolves to the current process user.
- OpenCode installer subprocesses explicitly use UTF-8; retrieval warmup explicitly
  excludes closed notes. Cross-platform tests normalize skill line endings and
  isolate platform selection from Python's global path implementation.


## [0.7.0] — 2026-10-12

### Added
- **The embedding model is switchable and every candidate is measured on equal footing.** Every vector — written or queried — is L2-normalised in the one low-level function both paths share, so a candidate model that fastembed doesn't normalise itself is no longer scored with a distance handicap against `notes_vec`'s plain-L2 default. `notes_vec`'s vector width now follows the current model (`TextEmbedding.get_embedding_size`, no network, no weights loaded) instead of a hardcoded `FLOAT[384]`; an unrecognised model name falls back to the default dimension with a logged error instead of crashing startup. Models that need query/document prefixes to perform as published get them from a small built-in table (`intfloat/multilingual-e5-large`: `"query: "` / `"passage: "` — the document and query paths embed different text for the same note, on purpose). `schema_version.embedding_model` in the vault's own database is the single source of truth for which model is active; `SYMBIOSIS_BRAIN_EMBED_MODEL` is a *request* the server alone applies at startup, under the existing reindex lock — hook and CLI subprocesses (which get their environment from `CLAUDE_ENV_FILE`, not the server's) always defer to what the database already says, so they can never embed a query with a model the stored index doesn't match. Default install behaviour is unchanged byte-for-byte: same model, same 384-dimensional vectors, no forced reindex on upgrade.
- **A registry for embedding models fastembed doesn't ship itself.** fastembed 0.8.0's own model list has no entry for `intfloat/multilingual-e5-small` — a small built-in table now registers such models with `TextEmbedding.add_custom_model` (source repo, weights file, dimension, pooling, normalisation) the first time either their dimension or their weights are needed, idempotently and checked against `list_supported_models()` rather than by catching the "already registered" error. **`intfloat/multilingual-e5-small-int8`** is the first entry: the int8 ONNX weights published in the model's own HF repo, with 384-dimensional vectors and per-model query/document prefixes. Opt in with `SYMBIOSIS_BRAIN_EMBED_MODEL=intfloat/multilingual-e5-small-int8`; the default install is untouched.
- **A closed note stays findable but is no longer injected as current.** `valid_to` recorded the day a claim stops holding and nothing read it, so a note closed on purpose kept turning up in recall blocks with nothing to say it had been superseded. Closed notes are now filtered out of the hook paths that inject recall, and only those: `brain_search` still returns them, and the duplicate check on the write path shows them marked as closed — "a live note already says this" means do not write, "a closed one used to" means write it and say what it supersedes. `is_superseded` reads the date instead of testing for a non-empty field, so a `valid_to` still in the future means the claim holds; an unparsable date leaves the note open. A static test requires every call site in `src/`, `scripts/` and `tools/` to state the policy explicitly.
- **`brain_patch` can set frontmatter fields, not only edit the body.** `gist`, `tags`, `valid_to`, `title` and the other fields are named arguments of their own, next to the body `anchor`; either or both in one call. Present-and-empty clears a field and absent keeps it, with the same rules `brain_write` uses; `title`, like `type` and `scope`, cannot be cleared. A title change lists by name the notes whose `[[links]]` written as the old title stop resolving. The anchor still never matches inside the YAML block, and a call that asks for nothing is an error rather than a "Patched" over an unchanged file.
- **`doctor` checks three states it used to print green over.** On 2026-09-02 it printed All OK while `notes_vec` was empty and semantic search was gone. It now checks compiled action rules (zero compiled rules used to pass as "Action-recall OK"), the vector index (`notes` against `notes_vec`, read-only and without running migrations), and a reindex lock left by a dead process — every search waits on that lock for three minutes before giving up. Unreadable is never reported as OK. Process liveness on Windows is probed with `OpenProcess`, never `os.kill(pid, 0)`, which on Windows terminates the process. `brain_status` and `doctor` share one definition of vector-index drift.
- **The retrieval log tells a harness message from a human prompt.** Since sessions started messaging each other, most of what the prompt hook logs is not a prompt: on 2026-09-04, 337 of 589 `hook_prompt` rows began with a cross-session message, a task notification or an agent's report. They are still logged — an early exit in the shell hook would leave no denominator for any recount — and `retrieval_event` gets a `query_kind` column: `human` or `service:<kind>` for the prompt sources, `n/a` for the rest. The label comes from one list of seven prefixes; `tools/eval_search.py` imports it instead of its own three-prefix copy, which caught 95 of those 337 rows. `brain-cli report` and `brain_report` print the ★ agreement share twice, over every search event and without the service-labelled ones, because which is higher depends on the model: over `hook_prompt`, service prompts scored below human ones under `bge-small` (31.8 % vs 42.6 %) and above them under `multilingual-e5-small` (52.5 % vs 40.7 %).
- **One `claude mcp list` per project directory per day, not one per session.** The command health-checks every configured server, which starts every stdio server — including a second `symbiosis-brain serve` on the live vault. SessionStart ran it once per session; on 2026-09-04 a crashed terminal restarted five sessions and produced five full polls in two and a half minutes. The answer is now shared between the sessions of one working directory, keyed on an explicit snapshot of what it depends on — the directory, the config directory, `claude --version`, and the content (not the mtime) of every configuration layer that can be seen — with a one-day TTL for what cannot; a miss logs which field moved. The session-start hook runs the check synchronously under a 3-second timeout and falls back to the old path on any failure. Launch flags and the parent's command line are not in the key: that rests on an assumption nobody has tested, stated in the module where the key is built.
- **The scope-first vault layout is supported alongside the legacy one.** A project's notes live under `<scope>/`, its card at `<scope>/<scope>.md`, its archived handoffs in the scope's own archive; notes may sit flat in the project folder until there are enough of them to be worth splitting into type folders. `brain_lint` checks the rules of both layouts — type folders inside a scope, a card that must be a project note, archives that expect project notes, the legacy top-level rule unchanged — and reports an index row whose file is gone. Handoff rotation finds cards in both layouts and archives each into its own. Nothing moves an existing vault: both layouts stay readable.
- **A fresh vault is scaffolded in the scope-first layout.** Setup used to seed the eight pre-reorg top-level type folders, so a new install got a shape that contradicted the layout the product uses and seven empty directories to reason about on day one. Only the top-level seeding changes; `reference/` stays, because `reference/scope-taxonomy.md` is the taxonomy the code reads at that path. The starter vault's README describes the new layout. An existing vault keeps its folders and nothing is deleted — `scaffold_vault` runs again on every `--repair`, and a test now holds it to that.
- **Material files under `files/` are not indexed as notes.** Markdown inside any `files/` folder is treated as material attached to a project: not ingested, not reported as a sync failure, and not flagged by `brain_lint` as not indexed. A note named `files.md` is still a note. On an existing vault, Markdown that an earlier version indexed under such a folder leaves the index at the first sync, vectors included; the files stay on disk.
- **`tools/eval_search.py --out-runs`** writes one row per (config, query) with its own recall, reciprocal rank and nDCG, so two configurations can be compared on the same queries rather than only by their aggregates.

### Fixed
- **`doctor` accepts a fresh vault with no action rules declared.** A valid empty compiled result is healthy; skipped rules, validation warnings and stale retained output are still reported as issues.
- **The command `doctor` recommends for action rules exists.** It advised `symbiosis-brain compile-action-rules`, which was reachable only as `python -m symbiosis_brain compile-action-rules` for the hooks. It is now a `symbiosis-brain` subcommand (`--vault` optional, defaults to the installed vault) that recompiles and prints the same verdict `doctor` would, with a matching exit code. When the compiler refused a rule, `doctor` lists each one with its reason and the file to fix — recompiling alone refuses it again.
- **`symbiosis-brain --version`.** There was no way to check which version an upgrade left behind short of `uv tool list`.
- **The status line uses scoped context/model fields and runtime effort.** Unavailable or all-zero usage is reported as unknown, not a fabricated 0%. Context observations are session-scoped and expire after 60 seconds; save markers and percentage-based Stop reminders accept only fresh, matching observations. Genuine rounded 0% and decreases after compaction remain valid.
- **A model-change migration no longer risks the working index on an unproven model.** The requested model is now smoke-tested (a real embed call) before `notes_vec` is dropped; a name that fails to load — typo, no network on a first download, a full disk — aborts the migration and leaves the old, working index untouched instead of destroying it first and finding out inside `index_all()`. When the smoke test passes, `schema_version.embedding_model` is written right after the table is recreated for the new dimension, *before* the full re-embed runs, so a crash mid-rebuild leaves the DB and the table's dimension in agreement and the existing drift repair finishes the job on the next start; a failure during the re-embed itself now disables vector search for that process (FTS-only) instead of raising unguarded on every later `brain_search`. Separately, the steady-state startup path (model unchanged — every ordinary process start) now also holds the reindex lock while applying its own targeted index diff, closing a race where its writes could land between another process's migration `DROP` and `CREATE`.
- **Tests no longer depend on whatever `SYMBIOSIS_BRAIN_EMBED_MODEL` happens to be set to in the developer's own shell.** `server._init` reads that variable directly from `os.environ` by design, so a machine where it is exported permanently (to pin a working model outside of tests) turned every "no env request" test red. A blanket autouse fixture now clears it for every test; a test that wants the variable present for its own scenario still sets it itself.
- **Custom-model registration no longer races itself.** `_ensure_custom_model_registered`'s check-then-act (`list_supported_models` then `add_custom_model`) is called from two independently-locked paths (`_get_embedder`, behind the cross-process fastembed-init lock, and `_embedding_dim`, behind none); without a lock of its own, two threads could both see a name as unregistered and both call `add_custom_model`, and fastembed raises on the second call for the same name. The check-then-act is now serialized with a dedicated lock, and `_embedding_dim` retries the size lookup once on that specific "already registered" error instead of treating it as an unknown model and silently returning the wrong dimension.
- **Note age comes from `valid_from`, which a re-sync cannot rewrite.** `staleness_warning` read `updated_at` / `created_at`, which record when the row was written, not when the claim was made. Measured on a live vault: 4 distinct `updated_at` values across 1632 notes, 1554 of them stamped with the day the embedder was migrated, and the warning firing 0 times out of 1635. `valid_from` lives in the note's own frontmatter — 109 distinct days over the same vault — and after the change the warning fires on 53 notes. An undated note reports no age rather than 0, and `brain_lint` counts such notes in a new `undated` category (282 of 1649, 17 %, on that vault). `list_notes` sorts by claim date, undated notes last, with `updated_at` as the tiebreaker.
- **A moved note keeps its dates, and a full reindex no longer restamps the vault.** Both rename routes were DELETE + INSERT, so a move re-aged the note: the 2026-09-02 layout migration re-aged 1525 notes into one 28-minute window. `Storage.move_note` re-keys the row in place — rowid, both stamps, hash and `valid_*` stay — and the vector row is copied under the new path without calling the model. `sync_all` pairs a path that vanished with one that appeared, strictly one to one by content hash, and reports the pair in `SyncResult.moved`. `updated_at` now means the file's content changed: `upsert_note` keeps it when the hash matches, and the forced reindex no longer NULLs every hash to force re-ingest. Dates already overwritten by earlier reindexes are not reconstructed.
- **The stale-lock message in `doctor` says what the lock costs and what happens next** — how long searches wait on it, that it is swept automatically at thirty minutes, and that deleting it is the choice for not waiting — instead of a bare "delete this file". Its first live run found a dead pid holding the reindex lock for 29 minutes; the lock cleared by itself a minute later.
- **The locale-decoding guard covers the whole repository.** It scanned only the installed package, where every call was already right, and reported clean while the violations sat in `tests/` and `tools/`. It now walks `src/`, `scripts/`, `tools/` and `tests/` and covers `open`, `read_text`, `write_text`, `os.fdopen` and `subprocess` in any import form. It found 86 calls without an explicit encoding: 81 in tests, 1 in tools and 4 in `src/` (lock files writing a pid and a timestamp, harmless by content); all of them now declare one. A second test pins that our own entry points reconfigure stdout to UTF-8, which the parent side of every captured call relies on.
- **Hook-side recall searched only `global` notes, in every project.** The recall hook and the status line read the session scope from `SYMBIOSIS_BRAIN_SCOPE`, which SessionStart writes into `CLAUDE_ENV_FILE` — a file Claude Code sources before Bash tool commands only, never before a hook or the status line. Scope is a hard filter in search, so every prompt recall fell back to `global`: 211 of the 213 logged prompt-recall events between 2026-08-27 and 2026-09-02, every day, in every project; the status line showed `scope: global` for the same reason. The hooks now resolve the scope themselves — the project's `CLAUDE.md` marker found by walking up from the payload's `cwd`, then a per-session bridge file, then the environment variable — and when nothing resolves, the search covers every scope and the header says `scope=all*` instead of silently narrowing to `global`. An end-to-end test runs SessionStart, the recall hook and the status line with the variable deliberately unset: 0 of 9 cases passed against the old hooks, 9 of 9 now.
- **A live process stops querying an index migrated to another model.** A server process resolves its model once, and another window or a restart can rebuild `notes_vec` under a different one; the old process then embeds queries with the wrong model and returns confident nonsense. The vector half re-checks the stored model name at most every 30 s and falls back to keyword search until restart when it no longer matches.
- **Archived handoffs link back to the card that exists.** The archive footer was hardcoded as `[[projects/<scope>]]`, so in a scope-first vault every rotation wrote a link to a note that no longer exists. It now follows the card's layout; a legacy vault keeps its legacy link.
- **`serve` refuses a missing vault instead of creating one.** A vault path that is not there is far more likely an unmounted drive or a typo than a first run, and the tree `serve` used to build had no taxonomy, so every note write against it failed. It now exits with the path and points at `setup`.
- **Onboarding writes `CRITICAL_FACTS.md` to the vault root.** The welcome skill wrote it to `wiki/`, where nothing reads it: the profile a new user dictated was never injected at session start and was counted as a note missing its gist.
- **`brain_lint` works on a fresh install.** The shipped starter taxonomy had no folder-to-type table, which the linter requires, so `brain_lint` raised `ValueError` on every new vault.
- **`setup claude-code --repair` no longer fails on a non-UTF-8 locale.** Its guard against registering the MCP server twice read `claude mcp list` decoded by locale; on a localised Windows (cp1251) one unmapped byte killed the reader thread, the output came back empty, the guard saw no server, the registration ran again, and setup failed on "already exists" and rolled itself back. Measured on the same machine: decoded by locale, the output was an empty string; decoded as UTF-8, 701 characters containing the server name. All five capturing calls in the installer now declare the encoding.
- **A PyPI install gets the embedding stack it was verified with, not the newest.** A PyPI install resolves from `pyproject.toml`, not `uv.lock`, and `onnxruntime` was not declared at all — it arrived through fastembed with no upper bound. On one machine a fresh install resolved `onnxruntime` 1.29.0, and embedding a real vault hung inside `InferenceSession.run` after a few hundred calls and ran about 7× slower than 1.24.4. The embedding stack is now bounded to what was verified together: `onnxruntime==1.24.4`, `fastembed>=0.8.0,<0.9`, `tokenizers>=0.22.2,<0.23` (the tokenizer decides what text the model sees, so a different one yields vectors that differ from an index built with this one) and `numpy>=1.26.0,<2.5`. The repository's own lock resolves the same versions as before.
- **A typed frontmatter value costs one note, not the whole vault's sync.** YAML turns an unquoted date into a date object, a timestamp into a datetime, `!!binary` into bytes and `!!set` into a set. Only three date fields were normalised; any other went into the stored frontmatter as it came, and the `TypeError` its serialisation raised was not caught per note, so one such note aborted the entire sync with a message that named no file — on 2026-09-10 it left 357 notes unsynced after a `git pull`. Dates and datetimes anywhere in the frontmatter now become ISO strings and the note is indexed; anything no normalisation can reach — bytes, a set, a date used as a top-level key — fails that one note, reported by its path, while the rest of the vault syncs.
- **`setup claude-code --vault <new>` on an existing install moves the vault instead of claiming to.** It printed "Done. Vault: <new>" and changed nothing: the vault path in `settings.json` is seeded without overwriting, and the MCP registration was kept as soon as the server's name appeared in `claude mcp list`, whatever path it pointed at — every session kept working in the old vault, and `doctor`, which reads the same registration, agreed. An explicit `--vault` now wins in both places: the value is written over an existing one, and a registration pointing elsewhere is replaced; if adding the new one fails, the old one is put back before the error is raised. Setup then says what it did — the vault moved, the notes were not copied, and every Claude Code window has to be restarted. Without `--vault` nothing that already exists is overwritten, as before.

### Changed
- **The `brain-backfill-gists` skill sets frontmatter through `brain_patch`** instead of telling its subagents to hand-edit the file with YAML escaping rules of their own; the batch `brain_sync` at the end is gone, since `brain_patch` reindexes the note it writes.
- **`brain_lint` reports `gist_too_long` only past the hard limit of 140 characters.** A third of a living vault legitimately sits at 101–140 (512 notes, measured 2026-09-02), so flagging the soft recommendation made the category pure noise. `brain_write` still warns past 100 and refuses past 140.
- **The shipped skills and tool descriptions follow the scope-first layout.** Twelve lines across five skills still described the old paths; `brain-project-init` now creates a new scope's card at `<scope>/<scope>.md` instead of `projects/<scope>.md`. `setup claude-code --repair` copies these skills over the installed ones.
- **`sync_all` on every hook call no longer walks `.git` or loads every row.** A run with nothing to do cost about 0.3 s: 73 ms went to a glob that visited every entry of `.git` (4542 of them) only to drop them, 71 ms to loading every note body. The walk now skips excluded directories on the way in and selects exactly the same files — compared name for name against the old walk on all 1724 notes of a real vault — and only a row's path and content hash are read. On an isolated copy of the vault a no-change run went from 345 to 215 ms (median of 8, on a busy machine). Every file is still read and hashed, so nothing `sync_all` decides has changed.

## [0.6.0] — 2026-09-01

### Added
- **Every search the memory serves is now recorded locally.** Both MCP tools and hooks write the query (full text, truncated to 2000 characters — on the hook path, that is your prompt text), the number of hits, the hit paths and the end-to-end hook latency into two new tables inside `<vault>/.index/brain.db`. Nothing leaves the machine; the rows are rotated after 90 days; `SYMBIOSIS_BRAIN_RETRIEVAL_LOG=off` (server and hooks) or `"retrieval_log_enabled": false` in `~/.claude/symbiosis-brain-pre-action.json` (hooks) turn it off. The tables are what makes the next two entries measurable instead of anecdotal. **A first start on an existing vault migrates the database once.**
- **A 14th MCP tool, `brain_report`, and a `brain-cli report` command that prints the same text.** One screen: what the memory served this month, which notes were surfaced and read, which notes nobody has opened or edited in a long time, and how complete the log itself is. Existing installs need one `symbiosis-brain setup claude-code --repair` to add the tool to `permissions.allow` — without it the tool exists but asks for permission on every call, and `doctor` now says so by name.
- **`brain_write` warns when the note being written looks like one that already exists.** A `[dedup]` line names up to two candidates with their gists. It is computed before the write, it never blocks the write, and it never replaces the deduplication step of the `brain-save` skill — it is silent on a cold or disabled vector index and on a rephrasing with no words in common. Tune it with `SYMBIOSIS_BRAIN_DEDUP_MIN` / `SYMBIOSIS_BRAIN_DEDUP_MAX_SHOWN`; `0` in either turns it off.
- **New and rewritten notes carry a `written_by:` line** — client and version, model, date. It records **who last wrote the note through the product**, not who thought it up: `brain_append` and `brain_patch` rebuild the frontmatter, so the line always names the most recent write. Existing notes are left alone: there is nothing to reconstruct them from, and an absent line simply means "written before this release". The model name comes from the status line through a temp-file bridge keyed by window; when the bridge is missing the field reads `unknown` and nothing else changes.
- **Action-recall (Stage 1): warnings from past mistakes fire at the moment of a risky Bash/PowerShell command**, not just against prompt text. `class:"action"` routes in `tool-routing.local.json` compile to `<vault>/.index/action-rules.tsv` plus a per-tool fast-reject pattern file (regex-validated against their own test vectors via `grep -E`), matched by a pure-bash block in the PreToolUse hook before the uv/python path even runs — a single combined `grep -qEf` fork rejects the common non-matching case instead of one fork per rule row. PowerShell now gets the same recall coverage Bash already had — **existing installs need one `symbiosis-brain setup claude-code --repair` (or a fresh `doctor` run, which now flags a stale matcher) to pick up the widened PreToolUse matcher.** A rule with no `test_match` vectors on a tool side is now dropped at compile time instead of shipping unvalidated.
- **Routing hints now fire on a subagent's prompt, not just yours.** `Task`/`Agent` calls match `augment`/`supersede` routes against the subagent's own prompt (a 4000-character window, wider than the recall query) and inject up to `routing_cap` hints before the subagent starts — the memory a subagent needs most is the memory it never had.
- **`brain-autolearn` ships as a product skill**, with both of its reference recipes. `brain-save` Step 0 used to name skills that were not part of the install; the second pass (`brain-self-critique`) is now explicitly optional and skipped in silence when absent.
- **`doctor` reports the SQLite build and can verify the database.** A vulnerable build (WAL-Reset race, fixed upstream in 3.51.3 and backported to 3.50.7) is called out by name; `doctor` runs `PRAGMA quick_check` on every run; `doctor --deep` adds the thorough `PRAGMA integrity_check`. `serve` records the same version line in `<vault>/.index/serve.log`.
- **`brain_lint` gained a `not_indexed` category** — markdown on disk that never made it into the database — and reports forward references (`[[forward:X]]`) separately from broken links.
- **A `/brain-sync` slash command** is now part of the package instead of living only on the maintainer's machine.
- **A GitHub Release is created from the tag** by `publish.yml`, with notes from `docs/release-notes/vX.Y.Z.md` or the matching CHANGELOG section — but only after the PyPI install has been verified.

- **A search-quality harness for maintainers, `tools/eval_search.py`.** It replays a query set against a copy of a vault and reports MRR and recall per configuration — lexical mode, embedding model (with optional L2 normalisation and query/document prefixes), reranking, and the cost of a full reindex, alongside peak RSS and index counts. A small synthetic set ships in `tests/data/eval_search/` and runs in CI; the numbers behind this release's search changes came from it.

### Fixed
- **The lexical half of search stopped demanding every word of the query.** `_sanitize_fts_query` quoted each token and joined them with a space, which FTS5 reads as AND — so a natural-language question returned nothing at all from the keyword side. Measured over 1666 real queries: **the lexical half returned zero rows on 84.5 % of them**. `brain_search` now uses OR; the two injection paths (prompt recall and pre-action recall) try AND first and fall back to OR, and the log records which of the two actually answered.
- **A scoped vector search no longer starves.** It fetched a fixed global neighbourhood and filtered it by scope afterwards, so notes of the requested scope that sat outside that neighbourhood were never returned at all — the caller saw fewer hits than exist. The neighbourhood now grows until the scope is satisfied or the corpus runs out.
- **Rewriting a note through `brain_write` no longer drops the frontmatter fields you did not pass.** `tags`, `scope`, `type` and any third-party keys (`umbrella`, `aliases`, …) survive a rewrite; passing an explicit empty value still clears the field, which is how a tag list is meant to be emptied. The whole read-modify-write now happens under the note lock instead of racing with itself.
- **The route compiler no longer drops a route in silence.** String triggers instead of objects raised a `TypeError` that ended in a temp-file debug log and nowhere else, which is how a freshly created route lost its prompt-side matching without a single visible symptom. Dropped routes and the reason for each are recorded in `action-rules.meta.json` and counted in the report.
- **`doctor` checks the things this release can actually break.** The MCP permission check compared a count instead of the set, so removing one name still passed; it now names the missing ones. Installed hooks are compared with the packaged ones and reported as **STALE** with a `--repair` hint, because upgrading the package does not touch `~/.claude/hooks` — an upgrade without `--repair` used to disable the model bridge and the hook timing while `doctor` printed "All OK".
- The action-rule matcher now honors `enabled: false` and `matchers` in `~/.claude/symbiosis-brain-pre-action.json` (previously only the `SYMBIOSIS_BRAIN_PRE_ACTION_DISABLED` env var could turn it off), decodes JSON string escaping (`\n`/`\"`/`\\`) out of the command before matching so multi-line commands and heredocs are actually visible to the matcher, and tolerates whitespace after `"command":` the same way the adjacent extractions already did.
- **Sessions started with `--resume` or `/clear` get their context back.** SessionStart was registered for `startup` and `compact` only, so a resumed session had no scope, no CRITICAL_FACTS and no session bridge — and the Stop-hook delta-guard measured from zero for the rest of it.
- **The last-save marker is written by a PostToolUse hook**, not by the skill, and takes the session id from the hook's own stdin. It also survives `/compact`, which used to wipe it and make the next threshold fire unconditionally.
- **The status-line data bridges are exported unconditionally.** Setting your own status line silently disabled save reminders and the rate-limit bridge, because both exports lived in the default row-1 script.
- **A vault whose remote has no branch yet can make its first push.** `git pull --rebase` fails with "couldn't find remote ref" against an empty repository, which raised a `stage=pull` alarm and skipped the push forever — so a freshly created GitHub repo never received anything. The hook now detects the missing branch and pushes with `-u`.
- **An unfinished merge or rebase in the vault is no longer completed behind the owner's back.** The not-ours guard ran after `git add -A`, so an interrupted merge got its `<<<<<<<` markers staged and committed — two commits became four and `MERGE_HEAD` disappeared. The guard now fires before the index is touched: alarm, and nothing else.
- **A refused `brain_write` no longer counts as a save.** The server reports refusals as plain text starting with `Error: `, not as `is_error`, so a rejected write still stamped the last-save marker and reset the Stop-hook delta-guard — the 25/35% zones then stayed silent until the context had grown another 10%.
- **`[[Q5: closure]]` is a broken link again, not an "external namespace".** Any `[[Word: text]]` matched the cross-scope prefix pattern and was waved through as a reference to an unknown namespace, which turned the hard block on a bad link into silence. A real external namespace (`[[superpowers:writing-skills]]`) has no space after the colon; `[[forward: X]]` keeps both spellings.
- **The vault sync pulls before it pushes.** SessionEnd was push-only: once the remote moved ahead, every later push failed into a log file nobody read while the hook kept returning 0. It now rebases with autostash, aborts on any conflict outside `log.md`, and raises a marker that both the next session start and the status line show.
- **Wiki links survive handoff rotation.** The archive index truncated snippets mid-`[[link]]`, which made the card partly uneditable and let the unterminated bracket swallow the next line of the index; the link regex no longer spans newlines.
- **`brain_patch` and `brain_append` no longer demand a `gist` they have no parameter for.** A note without a gist gets a soft warning; errors name the tool that was actually called.
- **`[[namespace:skill]]` references are no longer reported as broken.** External namespaces are recognised against the scope taxonomy (cached, so the write path does not re-read it per note).
- **One malformed note no longer aborts the whole vault sync** — it is reported by path in the `failed` list, and `MEMORY.md` is no longer indexed as a note.
- **Balanced index drift is detected and repaired at startup** (equal numbers of missing vectors and orphan rows previously slipped past the count gate), lock cleanup tolerates `OSError`, and unquoted dates from frontmatter no longer reach SQLite as `datetime.date`.
- **Prompt recall deduplicates and filters.** The `UserPromptSubmit` path had no dedup at all and ignored `excluded_note_types`; it now reuses the pre-action pipeline with its own seen-store and backfills fresh hits instead of just dropping repeats.
- **Action rules validate per side, not per pattern.** A rule with two patterns on one tool side was dropped even though the patterns covered every test vector together; a pattern that catches nothing is now reported in `meta.json → unmatched_patterns` as a warning.

### Changed
- **The ★ mark in `[recall:]` means something narrower.** It used to mean "this note appeared in both halves of the search"; after the lexical fix that was true for 54.9 % of queries and stopped distinguishing anything. ★ now means "top-3 in **both** halves" — 10.6 % of queries and 3.5 % of shown hits on the real pre-action profile. The mark is still a label and never a filter, and the underlying `in_both` flag is still recorded.
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
- Stage-4b routes for Serena and two tool-specific MCP servers — a CAD bridge and an IDE bridge — (fixture + gate tests), plus a Serena pre-edit advisory that surfaces dependency awareness before `Edit`/`Write`.
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
