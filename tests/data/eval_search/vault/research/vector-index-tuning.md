---
gist: measurements from tuning the vector index — width, rebuild time, memory
scope: global
tags: [vector, index]
title: Vector index tuning notes
type: research
valid_from: '2026-03-02'
---

## Measurements

Размерность вектора — 384. Tuning the vector index changed rebuild time more than it
changed ranking: a full rebuild of the corpus took 143 seconds and peaked near 1.1 GB.
