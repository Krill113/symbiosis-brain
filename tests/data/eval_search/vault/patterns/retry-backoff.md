---
gist: outbound calls retry on an exponential backoff with jitter, never in lockstep
scope: global
tags: [retry, resilience]
title: Retry with exponential backoff and jitter
type: pattern
valid_from: '2026-01-18'
---

## Pattern

Outbound calls retry with exponential backoff. The delay doubles from 200 ms, and a
random jitter of up to 30 percent keeps a fleet from retrying in lockstep.
