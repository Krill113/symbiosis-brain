# Demo Project — card snapshot (synthetic test fixture)
type: project | scope: demo | tags: demo, fixture, rotation

Synthetic project card used by `test_rotation_integration`. Contains no real
data — purely structural: a roadmap plus several dated `## Handoff` sections so
the handoff-rotation logic has something to archive.

## Roadmap

| Phase | What | Status |
|-------|------|--------|
| 1 | Bootstrap | Done |
| 2 | Core engine | In progress |
| 3 | Polish | Planned |

## Handoff 2025-03-01

**Shipped:** initial package scaffolding and CI wired.
- set up package layout
- added a smoke test

**Next step:** flesh out the core module.

## Handoff 2025-03-02

**Shipped:** core data model and storage layer.
- defined the record schema
- wrote the load/save round-trip

**Next step:** add the query path.

## Handoff 2025-03-03

**Shipped:** query path with basic filtering.
- keyword lookup
- range filter

**Next step:** add ranking.

## Handoff 2025-03-04

**Shipped:** ranking and result ordering.
- score fusion
- stable sort

**Next step:** wire the CLI.

## Handoff 2025-03-05

**Shipped:** command-line interface over the engine.
- subcommands for read/write
- help text

**Next step:** harden error handling.

## Handoff 2025-03-06

**Shipped:** error handling and input validation.
- friendly messages
- exit codes

**Next step:** performance pass.

## Handoff 2025-03-07

**Shipped:** performance pass on the hot path.
- cached the index
- removed a redundant scan

**Next step:** docs.

## Handoff 2025-03-08

**Shipped:** user-facing docs and examples.
- quickstart
- API reference stub

**Next step:** cut the first release.
