"""Stdlib-only bounded supervisor for the Codex hook runtime.

This module is the cold command boundary.  It must remain free of imports from
the Symbiosis Brain product runtime: request bytes and the absolute deadline are
validated before the private worker imports the adapter.
"""
from __future__ import annotations

import json
import math
import os
import queue
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence


_MAX_STDIN_BYTES = 4 * 1024 * 1024
_READ_CHUNK_CHARS = 64 * 1024
_INTERNAL_DEADLINES: dict[str, float] = {
    "SessionStart": 25.0,
    "UserPromptSubmit": 12.0,
    "PreToolUse": 12.0,
    "PostToolUse": 3.0,
    "PreCompact": 3.0,
    "PostCompact": 3.0,
    "Stop": 3.0,
    "SessionEnd": 2.0,
}


@dataclass
class _OwnedProcess:
    process: subprocess.Popen[bytes]
    job_handle: int | None = None


def _safe_emit(value: Mapping[str, object]) -> None:
    payload = json.dumps(dict(value), ensure_ascii=True) + "\n"
    try:
        sys.stdout.write(payload)
        sys.stdout.flush()
    except Exception:
        try:
            sys.stdout.write("{}\n")
            sys.stdout.flush()
        except Exception:
            pass


def _continue_response() -> dict[str, object]:
    return {"continue": True}


def _emit_and_exit(value: Mapping[str, object]) -> None:
    """Finish the one-shot CLI without finalizing a blocked stdin thread."""
    _safe_emit(value)
    os._exit(0)


def _parse_cli_arguments(
    argv: Sequence[str],
    *,
    allow_deadline: bool,
) -> tuple[str, str, float | None] | None:
    allowed = {"--vault", "--event"}
    if allow_deadline:
        allowed.add("--deadline")
    values: dict[str, str] = {}
    items = list(argv)
    index = 0
    while index < len(items):
        option = items[index]
        if option not in allowed or option in values:
            return None
        index += 1
        if index >= len(items):
            return None
        value = items[index]
        if not isinstance(value, str) or not value or "\x00" in value:
            return None
        values[option] = value
        index += 1
    required = {"--vault", "--event"}
    if not required.issubset(values) or set(values) - allowed:
        return None
    event = values["--event"]
    if event not in _INTERNAL_DEADLINES:
        return None
    deadline: float | None = None
    if "--deadline" in values:
        try:
            deadline = float(values["--deadline"])
        except (TypeError, ValueError):
            return None
        if not math.isfinite(deadline) or deadline <= 0:
            return None
    return values["--vault"], event, deadline


def _read_limited_input() -> tuple[bytes, bool]:
    binary = getattr(sys.stdin, "buffer", None)
    if binary is not None:
        raw = binary.read(_MAX_STDIN_BYTES + 1)
        return raw, len(raw) > _MAX_STDIN_BYTES

    chunks: list[bytes] = []
    total = 0
    while total <= _MAX_STDIN_BYTES:
        text = sys.stdin.read(_READ_CHUNK_CHARS)
        if not text:
            break
        encoded = text.encode("utf-8", errors="replace")
        total += len(encoded)
        if total > _MAX_STDIN_BYTES:
            return b"", True
        chunks.append(encoded)
    return b"".join(chunks), False


def _read_input_before(deadline: float) -> tuple[bytes, bool] | None:
    result: queue.Queue[tuple[bytes, bool] | BaseException] = queue.Queue(
        maxsize=1
    )

    def read() -> None:
        try:
            result.put(_read_limited_input())
        except BaseException as exc:
            result.put(exc)

    reader = threading.Thread(target=read, daemon=True)
    reader.start()
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return None
    try:
        value = result.get(timeout=remaining)
    except queue.Empty:
        return None
    if isinstance(value, BaseException):
        raise value
    return value


def _valid_request(raw: bytes, expected_event: str) -> bool:
    try:
        payload = json.loads(raw.decode("utf-8", errors="replace"))
    except (json.JSONDecodeError, TypeError, UnicodeError):
        return False
    return (
        isinstance(payload, dict)
        and payload.get("hook_event_name") == expected_event
    )


def _worker_command(deadline: float) -> list[str]:
    source_root = Path(__file__).parents[3]
    entry = (
        "import sys;"
        "sys.path.insert(0,sys.argv.pop(1));"
        "from symbiosis_brain.adapters.codex.supervisor import worker_main;"
        "raise SystemExit(worker_main(sys.argv[1:]))"
    )
    return [
        sys.executable,
        "-I",
        "-B",
        "-c",
        entry,
        str(source_root),
        "--deadline",
        repr(deadline),
    ]


def _create_windows_job(process: subprocess.Popen[bytes]) -> int | None:
    if os.name != "nt":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        create_job = kernel32.CreateJobObjectW
        create_job.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        create_job.restype = wintypes.HANDLE
        assign = kernel32.AssignProcessToJobObject
        assign.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        assign.restype = wintypes.BOOL
        close = kernel32.CloseHandle
        close.argtypes = [wintypes.HANDLE]
        close.restype = wintypes.BOOL

        job = create_job(None, None)
        if not job:
            return None
        job_handle = int(job)
        if not _set_job_kill_on_close(job_handle, enabled=True):
            close(job)
            return None
        process_handle = wintypes.HANDLE(
            int(process._handle)  # type: ignore[attr-defined]
        )
        if not assign(job, process_handle):
            close(job)
            return None
        return job_handle
    except (AttributeError, OSError, TypeError, ValueError):
        return None


def _close_windows_job(job_handle: int | None) -> None:
    if os.name != "nt" or job_handle is None:
        return
    try:
        import ctypes
        from ctypes import wintypes

        close = ctypes.WinDLL("kernel32", use_last_error=True).CloseHandle
        close.argtypes = [wintypes.HANDLE]
        close.restype = wintypes.BOOL
        close(wintypes.HANDLE(job_handle))
    except (AttributeError, OSError, TypeError, ValueError):
        pass


def _set_job_kill_on_close(job_handle: int, *, enabled: bool) -> bool:
    import ctypes
    from ctypes import wintypes

    class _IoCounters(ctypes.Structure):
        _fields_ = [
            ("ReadOperationCount", ctypes.c_ulonglong),
            ("WriteOperationCount", ctypes.c_ulonglong),
            ("OtherOperationCount", ctypes.c_ulonglong),
            ("ReadTransferCount", ctypes.c_ulonglong),
            ("WriteTransferCount", ctypes.c_ulonglong),
            ("OtherTransferCount", ctypes.c_ulonglong),
        ]

    class _BasicLimitInformation(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_longlong),
            ("PerJobUserTimeLimit", ctypes.c_longlong),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class _ExtendedLimitInformation(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", _BasicLimitInformation),
            ("IoInfo", _IoCounters),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    info = _ExtendedLimitInformation()
    info.BasicLimitInformation.LimitFlags = 0x00002000 if enabled else 0
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    set_info = kernel32.SetInformationJobObject
    set_info.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
    ]
    set_info.restype = wintypes.BOOL
    return bool(set_info(
        wintypes.HANDLE(job_handle),
        9,
        ctypes.byref(info),
        ctypes.sizeof(info),
    ))


def _terminate_owned_process(owned: _OwnedProcess) -> None:
    process = owned.process
    if os.name == "nt" and owned.job_handle is not None:
        try:
            import ctypes
            from ctypes import wintypes

            terminate = ctypes.WinDLL(
                "kernel32",
                use_last_error=True,
            ).TerminateJobObject
            terminate.argtypes = [wintypes.HANDLE, wintypes.UINT]
            terminate.restype = wintypes.BOOL
            terminate(wintypes.HANDLE(owned.job_handle), 1)
        except (AttributeError, OSError, TypeError, ValueError):
            pass
        finally:
            _close_windows_job(owned.job_handle)
            owned.job_handle = None
    elif os.name != "nt":
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (OSError, ProcessLookupError):
            pass
    if process.poll() is None:
        try:
            process.kill()
        except OSError:
            pass
    for stream in (process.stdin, process.stdout):
        try:
            if stream is not None:
                stream.close()
        except OSError:
            pass
    try:
        process.wait(timeout=0.5)
    except (OSError, subprocess.SubprocessError):
        pass


def _release_successful_process(owned: _OwnedProcess) -> bool:
    if os.name == "nt" and owned.job_handle is not None:
        try:
            if not _set_job_kill_on_close(
                owned.job_handle,
                enabled=False,
            ):
                return False
        except (AttributeError, OSError, TypeError, ValueError):
            return False
    _close_windows_job(owned.job_handle)
    owned.job_handle = None
    return True


def _launch_owned_process(
    command: Sequence[str],
    *,
    cancelled: threading.Event,
    claimed: threading.Event,
    result: queue.Queue[_OwnedProcess | BaseException],
) -> None:
    try:
        kwargs: dict[str, object] = {
            "stdin": subprocess.PIPE,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.DEVNULL,
            "close_fds": True,
        }
        if os.name == "nt":
            kwargs["creationflags"] = (
                getattr(subprocess, "CREATE_NO_WINDOW", 0)
                | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            )
        else:
            kwargs["start_new_session"] = True
        process = subprocess.Popen(list(command), **kwargs)
        job_handle = _create_windows_job(process)
        owned = _OwnedProcess(process=process, job_handle=job_handle)
        if os.name == "nt" and job_handle is None:
            _terminate_owned_process(owned)
            result.put(RuntimeError("worker containment unavailable"))
            return
        if cancelled.is_set():
            _terminate_owned_process(owned)
            return
        result.put(owned)
        if cancelled.is_set() and not claimed.is_set():
            _terminate_owned_process(owned)
    except BaseException as exc:
        try:
            result.put(exc)
        except Exception:
            pass


def _start_worker_before(
    command: Sequence[str],
    deadline: float,
) -> tuple[_OwnedProcess | None, threading.Event]:
    # The OS CreateProcess call itself is not cancellable from Python.  The
    # launcher therefore owns any late handle: cancellation withholds stdin,
    # and the stdlib-only worker exits on EOF without importing the adapter.
    result: queue.Queue[_OwnedProcess | BaseException] = queue.Queue(maxsize=1)
    cancelled = threading.Event()
    claimed = threading.Event()
    launcher = threading.Thread(
        target=_launch_owned_process,
        kwargs={
            "command": command,
            "cancelled": cancelled,
            "claimed": claimed,
            "result": result,
        },
        daemon=True,
    )
    launcher.start()
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        cancelled.set()
        return None, cancelled
    try:
        value = result.get(timeout=remaining)
    except queue.Empty:
        cancelled.set()
        try:
            late = result.get_nowait()
        except queue.Empty:
            return None, cancelled
        if isinstance(late, _OwnedProcess):
            claimed.set()
            _terminate_owned_process(late)
        return None, cancelled
    if isinstance(value, BaseException):
        return None, cancelled
    claimed.set()
    return value, cancelled


def _decode_worker_output(stdout: bytes) -> dict[str, object] | None:
    try:
        value = json.loads(stdout.decode("utf-8", errors="replace"))
    except (json.JSONDecodeError, TypeError, UnicodeError):
        return None
    return value if isinstance(value, dict) else None


def run_cli(argv: Sequence[str]) -> int:
    """Run one Codex hook with a deadline starting at this stdlib boundary."""
    started = time.monotonic()
    parsed = _parse_cli_arguments(argv, allow_deadline=False)
    if parsed is None:
        _safe_emit({})
        return 0
    vault, event, _ = parsed
    deadline = started + _INTERNAL_DEADLINES[event]
    owned: _OwnedProcess | None = None
    try:
        received = _read_input_before(deadline)
        if received is None:
            _emit_and_exit(_continue_response())
        raw, oversized = received
        if oversized or not raw or not _valid_request(raw, event):
            _safe_emit({})
            return 0
        command = [
            *_worker_command(deadline),
            "--vault",
            vault,
            "--event",
            event,
        ]
        owned, _cancelled = _start_worker_before(command, deadline)
        if owned is None:
            _safe_emit(_continue_response())
            return 0
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            _terminate_owned_process(owned)
            owned = None
            _safe_emit(_continue_response())
            return 0
        try:
            stdout, _stderr = owned.process.communicate(
                input=raw,
                timeout=remaining,
            )
        except subprocess.TimeoutExpired:
            _terminate_owned_process(owned)
            owned = None
            _safe_emit(_continue_response())
            return 0
        response = _decode_worker_output(stdout)
        if (
            time.monotonic() >= deadline
            or owned.process.returncode != 0
            or response is None
        ):
            _terminate_owned_process(owned)
            owned = None
            _safe_emit(_continue_response())
            return 0
        if not _release_successful_process(owned):
            _terminate_owned_process(owned)
        owned = None
        _safe_emit(response)
        return 0
    except Exception:
        if owned is not None:
            _terminate_owned_process(owned)
        _safe_emit(_continue_response())
        return 0


def worker_main(argv: Sequence[str]) -> int:
    """Read and validate input before importing the product adapter."""
    parsed = _parse_cli_arguments(argv, allow_deadline=True)
    if parsed is None:
        _safe_emit({})
        return 0
    vault, event, deadline = parsed
    try:
        raw, oversized = _read_limited_input()
    except Exception:
        _safe_emit(_continue_response())
        return 0
    if not raw:
        return 0
    if oversized or not _valid_request(raw, event):
        _safe_emit({})
        return 0
    if deadline is not None and time.monotonic() >= deadline:
        _safe_emit(_continue_response())
        return 0

    from symbiosis_brain.adapters.codex import adapter

    if deadline is not None and time.monotonic() >= deadline:
        _safe_emit(_continue_response())
        return 0
    return adapter._worker_main_from_raw(
        ["--vault", vault, "--event", event],
        raw.decode("utf-8", errors="replace"),
    )
