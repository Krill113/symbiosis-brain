# Release notes

One file per released version, named after its git tag: `v0.5.0.md` for tag `v0.5.0`.

The `release` job in `.github/workflows/publish.yml` publishes the contents of this file
as the body of the GitHub Release. When the file is absent (or empty), the job falls back
to the matching `## [X.Y.Z]` section of `CHANGELOG.md`, so a release is never published
without notes.

Write the file in the release PR, next to the CHANGELOG entry — that is where it gets
reviewed. Keep the CHANGELOG factual and complete; keep these notes readable:

- **Highlights** — what actually changed for a user, with numbers where they exist.
- **Worth knowing before you upgrade** — behaviour changes, manual steps
  (e.g. `symbiosis-brain setup claude-code --repair`), anything that can surprise.
- Skip the internal churn — that is what the CHANGELOG is for.

Extraction can be checked locally:

```bash
python3 tools/changelog_section.py 0.5.0 --repo-root .
```
