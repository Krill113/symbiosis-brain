"""
Parent process death watchdog (Windows-specific kernel-level wait).

Detects when our parent process terminates and fires a callback to initiate
graceful shutdown. Uses Win32 OpenProcess + WaitForSingleObject (kernel wait,
zero CPU while parent alive, immune to PID reuse). On non-Windows returns
an inert no-op handle.

See docs/superpowers/specs/2026-05-14-mcp-zombie-shutdown-design.md.
"""
from __future__ import annotations

import ctypes
import logging
import os
import sys
import threading
from ctypes import wintypes
from typing import Callable, Optional

logger = logging.getLogger(__name__)

SYNCHRONIZE = 0x00100000
INFINITE = 0xFFFFFFFF
WAIT_OBJECT_0 = 0x0


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

    ppid = os.getppid()
    if ppid <= 0:
        on_parent_death()  # already orphaned at startup
        return _INERT

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

    handle = kernel32.OpenProcess(SYNCHRONIZE, False, ppid)
    if not handle:
        # Degraded path — Task 3 covers tests for this.
        err = ctypes.get_last_error()
        logger.warning(
            "watchdog: OpenProcess(%d) failed err=%d — degraded", ppid, err
        )
        return _INERT

    # Race-window check — Task 3 covers tests for this.
    if kernel32.WaitForSingleObject(handle, 0) == WAIT_OBJECT_0:
        on_parent_death()
        kernel32.CloseHandle(handle)
        return _INERT

    logger.info("watchdog: active, PPID=%d", ppid)
    fired = threading.Event()

    def _wait_loop():
        try:
            kernel32.WaitForSingleObject(handle, INFINITE)
            if not fired.is_set():
                fired.set()
                logger.info(
                    "watchdog: parent %d terminated, initiating shutdown", ppid
                )
                try:
                    on_parent_death()
                except Exception:
                    logger.exception("watchdog callback raised")
        finally:
            try:
                kernel32.CloseHandle(handle)
            except Exception:
                pass

    t = threading.Thread(target=_wait_loop, daemon=True, name="parent-watchdog")
    t.start()
    return WatchdogHandle(thread=t, kernel_handle=handle, fired=fired)
