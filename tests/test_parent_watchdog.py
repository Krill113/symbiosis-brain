"""Unit tests for parent_watchdog. All platforms via mock — no real Win32 needed."""
from __future__ import annotations

import sys
import threading
from unittest.mock import MagicMock, patch

import pytest


def test_start_on_non_windows_returns_inert_handle():
    """On Linux/macOS the watchdog must be a no-op and never fire."""
    callback = MagicMock()
    with patch.object(sys, "platform", "linux"):
        from symbiosis_brain.parent_watchdog import start_parent_watchdog

        handle = start_parent_watchdog(callback)

    assert handle is not None
    assert handle._thread is None
    assert handle._handle is None
    callback.assert_not_called()


def test_inert_handle_stop_is_idempotent():
    """Calling stop() on inert (or already-stopped) handle must not raise."""
    from symbiosis_brain.parent_watchdog import WatchdogHandle

    h = WatchdogHandle()
    h.stop()
    h.stop()  # second call also fine
