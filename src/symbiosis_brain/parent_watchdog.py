"""
Parent process death watchdog (Windows-specific kernel-level wait).

Detects when our parent process terminates and fires a callback to initiate
graceful shutdown. Uses Win32 OpenProcess + WaitForSingleObject (kernel wait,
zero CPU while parent alive, immune to PID reuse). On non-Windows returns
an inert no-op handle.

See docs/superpowers/specs/2026-05-14-mcp-zombie-shutdown-design.md.
"""
from __future__ import annotations

import logging
import sys
import threading
from typing import Callable, Optional

logger = logging.getLogger(__name__)


class WatchdogHandle:
    """Carries thread + kernel handle for cleanup. Returned by start_parent_watchdog."""

    def __init__(
        self,
        thread: Optional[threading.Thread] = None,
        kernel_handle: Optional[int] = None,
        fired: Optional[threading.Event] = None,
    ):
        self._thread = thread
        self._handle = kernel_handle
        self._fired = fired or threading.Event()

    def stop(self) -> None:
        """Best-effort handle close. Safe to call multiple times."""
        if self._handle:
            try:
                import ctypes
                ctypes.windll.kernel32.CloseHandle(self._handle)
            except Exception:
                pass
            self._handle = None


_INERT = WatchdogHandle()


def start_parent_watchdog(on_parent_death: Callable[[], None]) -> WatchdogHandle:
    """
    Spawn a daemon thread that fires `on_parent_death` exactly once when our
    parent terminates. On non-Windows returns inert handle.
    """
    if sys.platform != "win32":
        return _INERT

    # Win32 path implemented in Task 2.
    raise NotImplementedError("Win32 path not yet implemented")
