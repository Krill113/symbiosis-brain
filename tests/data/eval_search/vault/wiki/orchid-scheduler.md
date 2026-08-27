---
gist: what the orchid scheduler owns and what it deliberately does not
scope: global
tags: [orchid, scheduler]
title: Orchid scheduler basics
type: wiki
valid_from: '2026-03-30'
---

## Overview

Orchid is the scheduler that owns every recurring trigger. It does not own retries —
a job that fails is a job the caller re-submits.
