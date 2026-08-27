# Install UX Smoke Checklist (v0.1)

Run on three clean VMs: macOS, Ubuntu 24.04, Windows 11 (no WSL).

## Prerequisites

- [ ] `uv` installed (`curl -LsSf https://astral.sh/uv/install.sh | sh` or platform equivalent)
- [ ] Claude Code installed (latest stable)
- [ ] No existing `~/.claude/settings.json` (or backed up)

## Test sequence

For each platform:

1. [ ] `uv tool install symbiosis-brain`
2. [ ] `symbiosis-brain --help` lists `serve`, `setup`, `doctor`, `uninstall`
3. [ ] `symbiosis-brain setup claude-code` — answer with default vault path
4. [ ] Verify: `~/.claude/settings.json` contains hooks block + statusLine + the full
       SB_PERMISSIONS allowlist (14 `mcp__symbiosis-brain__*` names) — `symbiosis-brain
       doctor` names the missing ones
5. [ ] Verify: `~/.claude/CLAUDE.md` ends with `<!-- symbiosis-brain v1: global -->`
6. [ ] Verify: `~/symbiosis-brain-vault/` has full folder structure + README.md + scope-taxonomy.md
7. [ ] Restart Claude Code in any directory
8. [ ] Verify: `brain-welcome` Step 1 pitch appears
9. [ ] Answer Step 2 question, verify CRITICAL_FACTS.md created in vault
10. [ ] Decline Step 3 tour
11. [ ] Step 4 Obsidian offer appears (if Obsidian not installed); decline
12. [ ] Verify: `<vault>/.sb-initialized` exists
13. [ ] Restart Claude Code → `brain-welcome` does NOT re-fire
14. [ ] `symbiosis-brain doctor` — all ✓
15. [ ] Break something (e.g. `rm ~/.claude/hooks/brain-save-trigger.sh`); `doctor` shows ✗ for hooks; `setup --repair` fixes it
16. [ ] `symbiosis-brain uninstall` — settings.json/CLAUDE.md restored from .bak; vault preserved

If ANY step fails on ANY platform — fix before merge.

---

## Post-rollout smoke — Stage 2 (telemetry + provenance)

Run **after** the merge, and in this order: **upgrade the installed package first**, then
`symbiosis-brain setup claude-code --repair`, then the single Claude Code restart. The order is not
cosmetic: `--repair` copies the hooks out of the INSTALLED package (`install_cli.py:248`,
`src_root = _packaged_hooks_dir()`), so repairing before upgrading re-installs the old bash next to
the new Python — the one drift this release actually fears. Name the upgrade method explicitly in
the report (editable install, a built wheel, or PyPI). The restart closes every window, so it is
the last step, not a step in the middle.

1. [ ] `symbiosis-brain doctor` — green, including the new checks: the full MCP allowlist and no
       STALE hooks. `<vault>/.index/serve.log` carries the `SQLite …` line and **no**
       `background init failed`. (Not "no migration WARN": the migration logs no warning at all,
       so that phrasing could never fail. `background init failed` — `server.py:981` — is the one
       line a broken migration actually leaves behind.)
       **Repeat a week later with `--deep`:** a second writing connection per process strengthens
       the preconditions of the known WAL-Reset defect, and its consequences only show at volume.
2. [ ] `brain_status` — the note count is unchanged, `Vector index in sync: yes`.
3. [ ] `brain_search` by hand, with a multi-word Russian question — a non-empty result (before the
       fix the lexical half was empty on 94.3 % of such queries).
4. [ ] Edit any file in a project → the `[recall: …]` block appears; the database has a row
       `retrieval_event(source='hook_pre_action', origin='main', e2e_ms IS NOT NULL)`.
5. [ ] Start a subagent → a row with `tool IN ('Task','Agent')` and `origin='subagent'`.
       The signal is a non-empty `agent_id` in the PreToolUse payload (`detect_origin`,
       retrieval_log.py), not a `subagents` segment in `transcript_path` — the CP-3
       preflight (2026-08-27, `review/preflight-step-b/README.md`) measured that
       hypothesis false: session_id/transcript_path are always the PARENT's.
6. [ ] `brain_write` a scratch note → the file carries
       `written_by: claude-code/<ver> <model> <date>` and the model is **not** `unknown` (if it is,
       the bridge is not working); the response carries `[counter]`, and `[dedup]` only if a similar
       note genuinely exists.
7. [ ] Rewrite that note through `brain_write` **without** a `tags` argument → the old `tags`,
       `scope`, `type` and every third-party key (`umbrella`, `aliases`, …) are still there.
8. [ ] Rewrite it again **with an explicit `tags: []`** → the `tags` key is gone from the
       frontmatter and nothing else moved.
9. [ ] Rewrite it once more with an explicit `note_type` different from the current one → `type:`
       in the file is now the value that was passed. Steps 7 and 8 cannot catch this: they check
       that the type is PRESERVED, and the mistake of reading `"type"` from the arguments instead of
       `"note_type"` produces exactly preservation — the type quietly stops coming from the call.
10. [ ] `brain-cli report` — fits on one screen, the numbers are not all zero.
11. [ ] `brain_report` from the agent — the same text. Plus: `permissions.allow` in
        `~/.claude/settings.json` contains `mcp__symbiosis-brain__brain_report` (otherwise the tool
        exists but asks for permission on every call); step 1 checks this too.
12. [ ] Obsidian opens a note carrying `written_by` without complaint; `brain_lint` has not grown a
        new finding.
13. [ ] A second `serve` start in a row downloads nothing and reindexes nothing — no download line
        and no `full re-build` in `serve.log`. Only relevant if the embedder model was changed.
14. [ ] `git status` in the vault — only the expected files; `.index/` is not in the diff.
15. [ ] The log's off switch really switches it off: set `SYMBIOSIS_BRAIN_RETRIEVAL_LOG=off`,
        restart `serve`, do one recall, and check that `retrieval_event` gained no rows — then set
        it back to `on` and confirm the next recall does add one. A kill switch nobody ever pulls
        is a kill switch nobody knows is broken.

Rollback, if it comes to that: `setup claude-code --repair` with the **old** package — before or
together with `git revert`, never after — then restart Claude Code. The dangerous drift is exactly
one: a new bash hook against an old Python package.
