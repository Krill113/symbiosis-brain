---
gist: timestamps written without a timezone read back as local time and drifted
scope: global
tags: [time, bug]
title: Timestamps written without a timezone
type: mistake
valid_from: '2026-02-02'
---

## What broke

Event timestamps were written without a timezone and read back as local time. The
drift showed up as a six-hour gap in the daily rollup.

## Fix

Store UTC, render local at the edge.
