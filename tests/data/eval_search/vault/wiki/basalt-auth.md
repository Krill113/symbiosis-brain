---
gist: basalt access and refresh token windows, and what happens when one expires
scope: global
tags: [basalt, auth]
title: Basalt auth tokens
type: wiki
valid_from: '2026-02-23'
---

## Tokens

A basalt access token lives 15 minutes. The refresh token lives 14 days and is
rotated on every use, so a stolen one is good for a single call.
