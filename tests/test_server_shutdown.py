"""Integration test: server exits when parent terminates (Windows-only).

Setup: pytest spawns a launcher subprocess. The launcher in turn spawns the
MCP server with the launcher as its parent. Pytest terminates the launcher.
The server's watchdog must detect parent death and exit within 5 seconds.

Not running pytest as direct parent because terminating pytest = killing self.
"""
from __future__ import annotations

import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform != "win32",
    reason="parent watchdog is Windows-specific",
)


LAUNCHER_SCRIPT_TEMPLATE = textwrap.dedent("""
    import os, subprocess, sys, time
    server = subprocess.Popen(
        [sys.executable, '-m', 'symbiosis_brain', '--vault', r'{vault}'],
        stdin=subprocess.PIPE,
    )
    # Write server PID so pytest can monitor it
    with open(r'{pid_file}', 'w') as f:
        f.write(str(server.pid))
    # Block forever — pytest will terminate this process
    while True:
        time.sleep(1)
""")


def _process_alive(pid: int) -> bool:
    """Return True if a process with given PID is currently running."""
    try:
        if sys.platform == "win32":
            import ctypes
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            kernel32 = ctypes.windll.kernel32
            STILL_ACTIVE = 259
            handle = kernel32.OpenProcess(
                PROCESS_QUERY_LIMITED_INFORMATION, False, pid
            )
            if not handle:
                return False
            try:
                exit_code = ctypes.c_ulong()
                kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code))
                return exit_code.value == STILL_ACTIVE
            finally:
                kernel32.CloseHandle(handle)
        else:
            os.kill(pid, 0)
            return True
    except Exception:
        return False


def test_server_exits_when_parent_terminated(tmp_path):
    """End-to-end: terminate intermediate parent → server detects + exits."""
    vault = tmp_path / "vault"
    vault.mkdir()
    pid_file = tmp_path / "server.pid"

    launcher_script = LAUNCHER_SCRIPT_TEMPLATE.format(
        vault=str(vault), pid_file=str(pid_file)
    )

    launcher = subprocess.Popen(
        [sys.executable, "-c", launcher_script],
    )

    try:
        # Wait for the launcher to write the server's PID
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if pid_file.exists():
                content = pid_file.read_text().strip()
                if content.isdigit():
                    server_pid = int(content)
                    break
            time.sleep(0.2)
        else:
            pytest.fail("server PID file not created within 30s")

        # Give the server ~5s to settle. Watchdog is active from the very
        # start of _run_server (before _background_init), so even mid-init
        # the process should respond to parent death. 5s is a comfort
        # margin against fastembed import / sqlite-vec extension load.
        time.sleep(5)
        assert _process_alive(server_pid), "server died before parent terminate"

        # Now kill the launcher → watchdog should fire in the server
        launcher.terminate()
        launcher.wait(timeout=5)

        # Server must exit within 5 seconds of parent death
        shutdown_deadline = time.monotonic() + 5
        while time.monotonic() < shutdown_deadline:
            if not _process_alive(server_pid):
                return  # success
            time.sleep(0.2)

        pytest.fail(
            f"server (pid={server_pid}) did not exit within 5s of parent terminate"
        )
    finally:
        # Cleanup — kill anything still running
        if launcher.poll() is None:
            launcher.kill()
        if pid_file.exists():
            try:
                pid = int(pid_file.read_text().strip())
                if _process_alive(pid):
                    subprocess.run(
                        ["taskkill", "/F", "/PID", str(pid)],
                        check=False,
                        capture_output=True,
                    )
            except Exception:
                pass
