# Synthetic search-eval set (Stage 2, CP-8a)

Fourteen hand-labelled pairs "query -> note" over a fourteen-note invented corpus,
run by `tests/test_eval_search.py` as a plain pytest regression. Before this set
existed, nothing in the suite caught a change in search ranking at all.

**Everything here is fabricated.** No real note, no real project, no real path, no
person. `pyproject.toml:59` ships the whole `tests` directory inside the sdist, so
this set goes to PyPI with the package — `tests/test_no_private_markers.py` checks
that mechanically rather than on trust.

## Files

- `queries.jsonl` — one JSON object per line, schema I-33 plus `gold`:
  `{"query", "source", "origin", "scope", "lang", "shown", "read_after", "gold"}`.
  `shown` and `read_after` are empty by construction: this set is labelled by hand,
  it has no transcript proxy to mine.
- `vault/**.md` — the corpus. Indexed by `VaultSync` into a throwaway database in
  a pytest `tmp_path`; nothing is ever written back here.

## Two properties the tests depend on

1. **Every query shares at least one LITERAL token with its gold note.** The FTS5
   table is `tokenize='porter'`, which stems English only — Russian word forms are
   matched as written. Change a word form on either side and `fts-any` starts
   returning nothing.
2. **Seven queries carry one word that appears nowhere in the corpus.** Those are
   the queries that starve under AND and survive under OR, which is what makes the
   set able to tell the two lexical modes apart.

`origin` is `"unknown"` everywhere on purpose: the journal has no session channel
on server-side paths, so `origin=subagent` from the transcript-mined set is NOT
the journal's `origin` and must never be imitated here (plan section 2, F6).
