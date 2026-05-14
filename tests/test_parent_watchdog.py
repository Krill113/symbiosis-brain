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


def _make_win32_kernel32_mock(open_process_returns=0xDEADBEEF, immediate_signal=False):
    """Build a fake kernel32 module for mocking ctypes.WinDLL.

    Returns (mock_kernel32, fire_signal_callable). Call fire_signal() to
    unblock WaitForSingleObject(handle, INFINITE) from the test.
    """
    signal_event = threading.Event()

    def fake_wait(handle, timeout):
        from symbiosis_brain.parent_watchdog import WAIT_OBJECT_0
        if timeout == 0:
            # Race-window check call: return non-signaled UNLESS test wants immediate fire
            return WAIT_OBJECT_0 if immediate_signal else 0xFFFFFFFF  # WAIT_TIMEOUT
        # INFINITE wait — block until test fires the signal
        signal_event.wait(timeout=10)
        return WAIT_OBJECT_0

    mock = MagicMock()
    mock.OpenProcess = MagicMock(return_value=open_process_returns)
    mock.WaitForSingleObject = MagicMock(side_effect=fake_wait)
    mock.CloseHandle = MagicMock(return_value=True)
    return mock, signal_event.set


def test_win32_happy_path_fires_callback_when_parent_signals():
    """On Win32, when WaitForSingleObject returns WAIT_OBJECT_0, callback fires."""
    kernel32_mock, fire_signal = _make_win32_kernel32_mock()
    callback_called = threading.Event()

    with patch.object(sys, "platform", "win32"), \
         patch("ctypes.WinDLL", return_value=kernel32_mock), \
         patch("os.getppid", return_value=12345):
        from symbiosis_brain.parent_watchdog import start_parent_watchdog

        handle = start_parent_watchdog(lambda: callback_called.set())

        # Watchdog thread should now be waiting on the signal
        assert handle._thread is not None
        assert handle._thread.is_alive()

        # Fire the simulated parent-death signal
        fire_signal()

        # Callback must run within 5s
        assert callback_called.wait(timeout=5), "callback did not fire after signal"

        # Thread should finish soon after firing
        handle._thread.join(timeout=2)
        assert not handle._thread.is_alive()


def test_win32_thread_is_daemon_with_correct_name():
    """Diagnostic affordance for crash dumps — thread must be named and daemonized."""
    kernel32_mock, fire_signal = _make_win32_kernel32_mock()

    with patch.object(sys, "platform", "win32"), \
         patch("ctypes.WinDLL", return_value=kernel32_mock), \
         patch("os.getppid", return_value=12345):
        from symbiosis_brain.parent_watchdog import start_parent_watchdog

        handle = start_parent_watchdog(lambda: None)

        assert handle._thread.name == "parent-watchdog"
        assert handle._thread.daemon is True

        fire_signal()
        handle._thread.join(timeout=2)


def test_win32_openprocess_called_with_synchronize_and_ppid():
    """OpenProcess(SYNCHRONIZE=0x00100000, FALSE, ppid) — exact API contract."""
    from symbiosis_brain.parent_watchdog import SYNCHRONIZE

    kernel32_mock, fire_signal = _make_win32_kernel32_mock()

    with patch.object(sys, "platform", "win32"), \
         patch("ctypes.WinDLL", return_value=kernel32_mock), \
         patch("os.getppid", return_value=99999):
        from symbiosis_brain.parent_watchdog import start_parent_watchdog

        start_parent_watchdog(lambda: None)
        fire_signal()

    kernel32_mock.OpenProcess.assert_called_once()
    call_args = kernel32_mock.OpenProcess.call_args[0]
    assert call_args[0] == SYNCHRONIZE
    assert call_args[1] is False  # bInheritHandle
    assert call_args[2] == 99999  # PPID
