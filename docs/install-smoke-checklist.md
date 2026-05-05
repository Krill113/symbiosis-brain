# Install UX Smoke Checklist (v0.1)

Run on three clean VMs: macOS, Ubuntu 24.04, Windows 11 (no WSL).

## Prerequisites

- [ ] `uv` installed (`curl -LsSf https://astral.sh/uv/install.sh | sh` or platform equivalent)
- [ ] Claude Code installed (latest stable)
- [ ] No existing `~/.claude/settings.json` (or backed up)

## Test sequence

For each platform:

1. [ ] `uv tool install symbiosis-brain`
2. [ ] `symbiosis-brain --help` lists `serve`, `setup`, `doctor`, `uninstall`, `migrate-hooks`
3. [ ] `symbiosis-brain setup claude-code` — answer with default vault path
4. [ ] Verify: `~/.claude/settings.json` contains hooks block + statusLine + ≥7 permissions
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
15. [ ] Break something (e.g. `rm ~/.claude/hooks/brain-save-trigger.py`); `doctor` shows ✗ for hooks; `setup --repair` fixes it
16. [ ] `symbiosis-brain uninstall` — settings.json/CLAUDE.md restored from .bak; vault preserved

If ANY step fails on ANY platform — fix before merge.
