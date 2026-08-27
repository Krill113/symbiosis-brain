---
gist: why the ledger service keeps one shared redis cache instead of a per-replica one
scope: global
tags: [cache, redis]
title: Cache layer — shared Redis, not in-process
type: decision
valid_from: '2026-01-04'
---

## Decision

The ledger service uses a shared redis cache. An in-process cache drifted between
replicas within minutes of a deploy.

## Reasoning

- One eviction policy for every replica: allkeys-lru at 512 MB.
- A cold replica warms from the cache instead of re-reading the ledger.
