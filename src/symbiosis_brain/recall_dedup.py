"""Session-scoped recall dedup (Stage 1).

Suppresses recall hits already shown earlier in the same session within a short
TTL window, so the per-tool-call recall block stops re-emitting the same hits
(which trains the agent to ignore recall — see
[[feedback/symbiosis-brain-usage-self-critique-2026-05-15]]).

Keyed by session_id so parallel sessions don't clobber each other's state
(see [[patterns/statusline-data-bridge]]). Self-pruning by TTL on every load, plus
opportunistic reaping of dead-session files → no dependency on external GC, fully
live without redeploy. Best-effort and fail-open: any I/O error degrades to
"no dedup", never blocks recall.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from pathlib import Path
from typing import Iterable, Optional

from symbiosis_brain.atomic_write import atomic_write_text
from symbiosis_brain.pre_action_config import _tmp_dir

_SAFE = re.compile(r"[^A-Za-z0-9._-]+")
# A dead session's file is never reloaded (so its entries never re-prune); reap
# files untouched for longer than this so the temp dir can't grow unbounded.
_ORPHAN_GRACE_SECONDS = 3600


def _safe_session(session_id: str) -> str:
    """Map session_id (external PreToolUse input) to a filename-safe, collision-free
    token: sanitized prefix (path-traversal-safe, human-readable) + short hash of
    the RAW id, so two distinct ids never share a seen-file — avoids cross-session
    dedup bleed even for non-UUID ids."""
    raw = session_id or ""
    token = _SAFE.sub("_", raw)[:80] or "nosession"
    digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:8]
    return f"{token}-{digest}"


def _seen_path(session_id: str, base_dir: Path, prefix: str = "brain-recall-seen-") -> Path:
    return Path(base_dir) / f"{prefix}{_safe_session(session_id)}.json"


class SeenStore:
    """File-backed, TTL-pruned set of recall paths already shown this session.

    Stores `{path: last_shown_epoch}`. `now` is captured once per instance — and
    since each PreToolUse hook is a fresh short-lived subprocess (one tool call ==
    one process), that equals "this recall's wall-clock", which is what both the
    prune cutoff and the record timestamp want.
    """

    def __init__(
        self,
        session_id: str,
        ttl_seconds: int = 120,
        base_dir: Optional[Path] = None,
        now: Optional[float] = None,
        prefix: str = "brain-recall-seen-",
    ):
        self._prefix = prefix
        self._path = _seen_path(session_id, base_dir or _tmp_dir(), prefix)
        self._ttl = ttl_seconds
        self._now = time.time() if now is None else now
        self._data: dict[str, float] = self._load_pruned()
        self._reap_orphans()

    def _load_pruned(self) -> dict[str, float]:
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, ValueError):
            return {}
        if not isinstance(raw, dict):
            return {}
        cutoff = self._now - self._ttl
        # bool is an int subclass — reject hand-edited true/false timestamps.
        return {
            p: ts
            for p, ts in raw.items()
            if isinstance(ts, (int, float)) and not isinstance(ts, bool) and ts >= cutoff
        }

    def _reap_orphans(self) -> None:
        """Delete sibling seen-files from dead sessions (mtime older than the grace
        window). Best-effort, fail-open; never touches our own file."""
        cutoff = self._now - _ORPHAN_GRACE_SECONDS
        try:
            siblings = list(self._path.parent.glob(f"{self._prefix}*.json"))
        except OSError:
            return
        for f in siblings:
            if f == self._path:
                continue
            try:
                if f.stat().st_mtime < cutoff:
                    f.unlink()
            except OSError:
                pass

    def is_seen(self, path: str) -> bool:
        return path in self._data

    def record(self, paths: Iterable[str]) -> None:
        for p in paths:
            if p:
                self._data[p] = self._now
        try:
            atomic_write_text(self._path, json.dumps(self._data, ensure_ascii=False))
        except OSError:
            pass  # fail-open: dedup is best-effort, never block recall
