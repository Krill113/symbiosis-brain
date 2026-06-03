# symbiosis-brain

<!-- symbiosis-brain v1: scope=symbiosis-brain -->

## Test data & privacy (keep this a clean public repo)

- **Test fixtures MUST be synthetic.** Never commit a real snapshot of a private
  vault, real notes/handoffs, or production data as a fixture — fabricate minimal
  structural data instead. (A real vault-card snapshot leaked here once; history
  was scrubbed 2026-06-03.)
- **No personal data in tracked files.** No local absolute paths (`C:\Users\...`,
  `C:\Repos\...`), usernames, emails, or private project names. Use `sys.executable`
  / env vars / relative paths and generic placeholders.
- The private vault lives OUTSIDE this repo (sibling dir, gitignored) — never
  `git add` vault content.
- Planned automated guard: Gitleaks pre-commit + CI with custom rules for the
  patterns above.
