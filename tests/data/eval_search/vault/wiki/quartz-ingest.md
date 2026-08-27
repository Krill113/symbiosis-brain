---
gist: how the quartz ingest pipeline batches and writes incoming records
scope: quartz
tags: [quartz, ingest]
title: Quartz ingest pipeline
type: wiki
valid_from: '2026-02-16'
---

## Overview

The quartz ingest pipeline reads batches of at most 500 records. Конвейер разбирает
пакет, отбрасывает дубликаты по ключу и пишет остаток одной транзакцией.
