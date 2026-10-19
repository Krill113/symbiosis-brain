"""Private, process-safe state for Codex hook sessions."""
from __future__ import annotations

import errno
import hashlib
import json
import math
import ntpath
import os
import re
import stat
import subprocess
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from symbiosis_brain import atomic_write
from symbiosis_brain.recall_dedup import _safe_session

_STATE_SUFFIX = ".json"
_LOCK_SUFFIX = ".lock"
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_GC_AGE_SECONDS = 24 * 60 * 60
_POLL_SECONDS = 0.01
_FILE_ATTRIBUTE_REPARSE_POINT = 0x0400
_SID_RE = re.compile(r"^S-\d+(?:-\d+)+$")
_PRIVACY_ERROR = "persistent Codex state privacy could not be guaranteed"
_VERIFIED_PRIVATE_DIRS: set[tuple[Path, Path]] = set()


class PersistentStatePrivacyError(RuntimeError):
    """Persistent state cannot be used without a verified private directory."""


@dataclass
class SessionState:
    session_key: str
    turn: int = 0
    last_save_turn: int = 0
    save_reminded_turn: int = 0
    compaction_pending: bool = False


def _native_path(path: Path) -> str:
    value = str(path)
    if os.name == "nt" and not value.startswith("\\\\?\\"):
        absolute = ntpath.abspath(value)
        if absolute.startswith("\\\\"):
            return "\\\\?\\UNC\\" + absolute.lstrip("\\")
        return "\\\\?\\" + absolute
    return value


def session_key(session_id: str) -> str:
    """Return a collision-resistant, hash-only identifier for an external ID."""
    if not isinstance(session_id, str):
        raise TypeError("session_id must be a string")
    if not session_id:
        raise ValueError("session_id must not be empty")
    safe = _safe_session(session_id)
    material = f"{safe}\0{session_id}".encode("utf-8")
    return hashlib.sha256(material).hexdigest()


def _raw_codex_home() -> Path:
    configured = os.environ.get("CODEX_HOME", "").strip()
    path = Path(configured).expanduser() if configured else Path.home() / ".codex"
    return Path(os.path.abspath(os.fspath(path)))


def _is_reparse(stat_result: os.stat_result) -> bool:
    attributes = getattr(stat_result, "st_file_attributes", 0)
    return bool(attributes & _FILE_ATTRIBUTE_REPARSE_POINT)


def _checked_lstat(path: Path) -> os.stat_result:
    result = os.lstat(_native_path(path))
    if stat.S_ISLNK(result.st_mode) or _is_reparse(result):
        raise OSError(f"refusing symlink/reparse path component: {path}")
    return result


def _path_components(path: Path) -> list[Path]:
    components: list[Path] = []
    current = path
    while current != current.parent:
        components.append(current)
        current = current.parent
    components.append(current)
    components.reverse()
    return components


def _ensure_directory(path: Path, *, private: bool) -> bool:
    created = False
    try:
        result = _checked_lstat(path)
    except FileNotFoundError:
        try:
            os.makedirs(_native_path(path), mode=0o700, exist_ok=True)
            created = True
        except FileExistsError:
            pass
        result = _checked_lstat(path)
    if not stat.S_ISDIR(result.st_mode):
        raise OSError(f"state path component is not a directory: {path}")
    if private and os.name != "nt":
        os.chmod(_native_path(path), 0o700)
    return created


def _privacy_failure() -> PersistentStatePrivacyError:
    return PersistentStatePrivacyError(_PRIVACY_ERROR)


def _windows_system_directory() -> Path:
    import ctypes
    from ctypes import wintypes

    buffer = (wintypes.WCHAR * 32768)()
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.GetSystemDirectoryW.argtypes = (wintypes.LPWSTR, wintypes.UINT)
        kernel32.GetSystemDirectoryW.restype = wintypes.UINT
        length = kernel32.GetSystemDirectoryW(buffer, len(buffer))
    except Exception:
        raise _privacy_failure() from None
    if length == 0 or length >= len(buffer):
        raise _privacy_failure()
    return Path(buffer.value)


def _current_windows_sid(system_directory: Path) -> str:
    del system_directory
    import ctypes
    from ctypes import wintypes

    class _SidAndAttributes(ctypes.Structure):
        _fields_ = [
            ("sid", ctypes.c_void_p),
            ("attributes", wintypes.DWORD),
        ]

    class _TokenUser(ctypes.Structure):
        _fields_ = [("user", _SidAndAttributes)]

    token_query = 0x0008
    token_user = 1
    try:
        advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        current_process = kernel32.GetCurrentProcess
        current_process.restype = wintypes.HANDLE
        open_process_token = advapi32.OpenProcessToken
        open_process_token.argtypes = (
            wintypes.HANDLE,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.HANDLE),
        )
        open_process_token.restype = wintypes.BOOL
        get_token_information = advapi32.GetTokenInformation
        get_token_information.argtypes = (
            wintypes.HANDLE,
            wintypes.DWORD,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
        )
        get_token_information.restype = wintypes.BOOL
        convert_sid = advapi32.ConvertSidToStringSidW
        convert_sid.argtypes = (
            ctypes.c_void_p,
            ctypes.POINTER(wintypes.LPWSTR),
        )
        convert_sid.restype = wintypes.BOOL
        close_handle = kernel32.CloseHandle
        close_handle.argtypes = (wintypes.HANDLE,)
        close_handle.restype = wintypes.BOOL
        local_free = kernel32.LocalFree
        local_free.argtypes = (ctypes.c_void_p,)
        local_free.restype = ctypes.c_void_p
    except Exception:
        raise _privacy_failure() from None

    token = wintypes.HANDLE()
    if not open_process_token(
        current_process(), token_query, ctypes.byref(token)
    ):
        raise _privacy_failure()
    try:
        size = wintypes.DWORD()
        get_token_information(
            token, token_user, None, 0, ctypes.byref(size)
        )
        if not size.value:
            raise _privacy_failure()
        buffer = ctypes.create_string_buffer(size.value)
        if not get_token_information(
            token,
            token_user,
            buffer,
            size.value,
            ctypes.byref(size),
        ):
            raise _privacy_failure()
        user = ctypes.cast(buffer, ctypes.POINTER(_TokenUser)).contents
        sid_text = wintypes.LPWSTR()
        if not convert_sid(user.user.sid, ctypes.byref(sid_text)):
            raise _privacy_failure()
        try:
            sid = sid_text.value
        finally:
            local_free(sid_text)
    finally:
        close_handle(token)
    if not sid or not _SID_RE.fullmatch(sid):
        raise _privacy_failure()
    return sid


def _windows_access_sddl(path: Path) -> str:
    import ctypes
    from ctypes import wintypes

    security_descriptor = ctypes.c_void_p()
    sddl_text = wintypes.LPWSTR()
    length = wintypes.DWORD()
    try:
        advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        get_named_security_info = advapi32.GetNamedSecurityInfoW
        get_named_security_info.argtypes = (
            wintypes.LPWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_void_p),
        )
        get_named_security_info.restype = wintypes.DWORD
        convert_security_descriptor = (
            advapi32.ConvertSecurityDescriptorToStringSecurityDescriptorW
        )
        convert_security_descriptor.argtypes = (
            ctypes.c_void_p,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.LPWSTR),
            ctypes.POINTER(wintypes.DWORD),
        )
        convert_security_descriptor.restype = wintypes.BOOL
        local_free = kernel32.LocalFree
        local_free.argtypes = (ctypes.c_void_p,)
        local_free.restype = ctypes.c_void_p
        error = get_named_security_info(
            _native_path(path),
            1,  # SE_FILE_OBJECT
            0x00000004,  # DACL_SECURITY_INFORMATION
            None,
            None,
            None,
            None,
            ctypes.byref(security_descriptor),
        )
        if error != 0 or not security_descriptor.value:
            raise _privacy_failure()
        if not convert_security_descriptor(
            security_descriptor,
            1,  # SDDL_REVISION_1
            0x00000004,  # DACL_SECURITY_INFORMATION
            ctypes.byref(sddl_text),
            ctypes.byref(length),
        ):
            raise _privacy_failure()
        return sddl_text.value
    except PersistentStatePrivacyError:
        raise
    except Exception:
        raise _privacy_failure() from None
    finally:
        if sddl_text:
            try:
                local_free(sddl_text)
            except Exception:
                pass
        if security_descriptor:
            try:
                local_free(security_descriptor)
            except Exception:
                pass


def _windows_acl_is_expected(first: Path, second: Path) -> bool:
    try:
        sid = _current_windows_sid(Path())
        expected = re.compile(
            rf"^D:P(?:AI)?\(A;OICI;FA;;;{re.escape(sid)}\)$"
        )
        return all(
            expected.fullmatch(_windows_access_sddl(path).strip())
            for path in (first, second)
        )
    except Exception:
        return False


def _apply_and_verify_windows_acls(first: Path, second: Path) -> None:
    if _windows_acl_is_expected(first, second):
        return
    system_directory = _windows_system_directory()
    sid = _current_windows_sid(system_directory)
    powershell = (
        system_directory / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    )
    script = (
        "& { param([string]$p1,[string]$p2,[string]$s) "
        "$ErrorActionPreference='Stop'; "
        "$sid=New-Object System.Security.Principal.SecurityIdentifier($s); "
        "$out=foreach($p in @($p1,$p2)) { "
        "$acl=New-Object System.Security.AccessControl.DirectorySecurity; "
        "$acl.SetAccessRuleProtection($true,$false); "
        "$rule=New-Object System.Security.AccessControl.FileSystemAccessRule("
        "$sid,'FullControl','ContainerInherit,ObjectInherit','None','Allow'); "
        "$acl.AddAccessRule($rule) | Out-Null; "
        "[System.IO.Directory]::SetAccessControl($p,$acl); "
        "$check=[System.IO.Directory]::GetAccessControl($p); "
        "$check.GetSecurityDescriptorSddlForm("
        "[System.Security.AccessControl.AccessControlSections]::Access) }; "
        "[Console]::Out.Write(($out -join \"`n\")) }"
    )
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        result = subprocess.run(
            [
                powershell,
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                script,
                _native_path(first),
                _native_path(second),
                sid,
            ],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="strict",
            timeout=10,
            creationflags=creationflags,
        )
    except (OSError, subprocess.SubprocessError, UnicodeError):
        raise _privacy_failure() from None
    expected = re.compile(
        rf"^D:P(?:AI)?\(A;OICI;FA;;;{re.escape(sid)}\)$"
    )
    descriptors = result.stdout.splitlines()
    if result.returncode != 0 or result.stderr.strip() or len(descriptors) != 2:
        raise _privacy_failure()
    if any(not expected.fullmatch(descriptor.strip()) for descriptor in descriptors):
        raise _privacy_failure()


def _state_dir() -> Path:
    codex_home = _raw_codex_home()
    components = _path_components(codex_home)
    for component in components:
        _ensure_directory(component, private=False)

    private_root = codex_home / ".symbiosis-brain"
    directory = private_root / "state"
    _ensure_directory(private_root, private=True)
    _ensure_directory(directory, private=True)
    if os.name == "nt":
        key = (private_root, directory)
        if key not in _VERIFIED_PRIVATE_DIRS:
            _apply_and_verify_windows_acls(private_root, directory)
            _VERIFIED_PRIVATE_DIRS.add(key)
    _garbage_collect(directory)
    return directory


def _safe_entry_stat(entry: os.DirEntry[str]) -> os.stat_result | None:
    try:
        result = entry.stat(follow_symlinks=False)
    except OSError:
        return None
    if stat.S_ISLNK(result.st_mode) or _is_reparse(result):
        return None
    return result


def _garbage_collect(
    directory: Path,
    *,
    now: float | None = None,
) -> None:
    current_time = time.time() if now is None else now
    cutoff = current_time - _GC_AGE_SECONDS
    try:
        entries = list(os.scandir(_native_path(directory)))
    except OSError:
        return
    for entry in entries:
        name = entry.name
        stem, suffix = os.path.splitext(name)
        if suffix not in {_STATE_SUFFIX, _LOCK_SUFFIX} or not _HASH_RE.fullmatch(stem):
            continue
        if suffix == _LOCK_SUFFIX:
            # Persistent advisory-lock guards are deliberately never unlinked.
            # Reusing one inode avoids check/unlink and replacement races.
            continue
        result = _safe_entry_stat(entry)
        if result is None or not stat.S_ISREG(result.st_mode) or result.st_mtime >= cutoff:
            continue
        try:
            os.unlink(entry.path)
        except OSError:
            pass


def _validate_key(value: str) -> str:
    if not isinstance(value, str) or not _HASH_RE.fullmatch(value):
        raise ValueError("session_key must be a 64-character lowercase hex digest")
    return value


def _path_for_key(value: str, suffix: str) -> Path:
    return _state_dir() / f"{_validate_key(value)}{suffix}"


def state_path(session_id: str) -> Path:
    return _path_for_key(session_key(session_id), _STATE_SUFFIX)


def lock_path(session_id: str) -> Path:
    return _path_for_key(session_key(session_id), _LOCK_SUFFIX)


def _fresh(key: str) -> SessionState:
    return SessionState(session_key=key)


def _counter(value: object) -> int | None:
    if type(value) is int and value >= 0:
        return value
    return None


def _state_from_payload(key: str, payload: object) -> SessionState | None:
    if not isinstance(payload, dict):
        return None
    expected = {
        "turn",
        "last_save_turn",
        "save_reminded_turn",
        "compaction_pending",
    }
    if set(payload) != expected:
        return None
    turn = _counter(payload.get("turn"))
    last_save_turn = _counter(payload.get("last_save_turn"))
    save_reminded_turn = _counter(payload.get("save_reminded_turn"))
    compaction_pending = payload.get("compaction_pending")
    if (
        turn is None
        or last_save_turn is None
        or save_reminded_turn is None
        or type(compaction_pending) is not bool
    ):
        return None
    return SessionState(
        session_key=key,
        turn=turn,
        last_save_turn=last_save_turn,
        save_reminded_turn=save_reminded_turn,
        compaction_pending=compaction_pending,
    )


def _reject_special_file(path: Path) -> None:
    try:
        result = _checked_lstat(path)
    except FileNotFoundError:
        return
    if not stat.S_ISREG(result.st_mode):
        raise OSError(f"state target is not a regular file: {path}")


def load_state(session_id: str) -> SessionState:
    key = session_key(session_id)
    path = _path_for_key(key, _STATE_SUFFIX)
    _reject_special_file(path)
    try:
        with open(_native_path(path), encoding="utf-8") as stream:
            payload = json.load(stream)
    except (OSError, json.JSONDecodeError, ValueError):
        return _fresh(key)
    return _state_from_payload(key, payload) or _fresh(key)


def _validated_counter(name: str, value: object) -> int:
    parsed = _counter(value)
    if parsed is None:
        raise ValueError(f"{name} must be a non-negative integer")
    return parsed


def save_state(state: SessionState) -> None:
    key = _validate_key(state.session_key)
    path = _path_for_key(key, _STATE_SUFFIX)
    _reject_special_file(path)
    payload = {
        "turn": _validated_counter("turn", state.turn),
        "last_save_turn": _validated_counter("last_save_turn", state.last_save_turn),
        "save_reminded_turn": _validated_counter(
            "save_reminded_turn", state.save_reminded_turn
        ),
        "compaction_pending": state.compaction_pending,
    }
    if type(state.compaction_pending) is not bool:
        raise ValueError("compaction_pending must be a boolean")
    atomic_write.atomic_write_text(
        path,
        json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")),
    )
    if os.name != "nt":
        os.chmod(_native_path(path), 0o600)


def _unlink_regular(path: Path) -> None:
    try:
        result = _checked_lstat(path)
    except FileNotFoundError:
        return
    if not stat.S_ISREG(result.st_mode):
        raise OSError(f"refusing non-regular state file: {path}")
    os.unlink(_native_path(path))


def delete_state(session_id: str) -> None:
    key = session_key(session_id)
    directory = _path_for_key(key, _STATE_SUFFIX).parent
    _unlink_regular(directory / f"{key}{_STATE_SUFFIX}")
    seen_token = _safe_session(key[:32])
    for prefix in (
        "brain-codex-prompt-seen-",
        "brain-codex-action-seen-",
        "brain-codex-route-seen-",
    ):
        _unlink_regular(directory / f"{prefix}{seen_token}{_STATE_SUFFIX}")
    # The real route producer passes the full session digest to SeenStore,
    # which applies its own collision suffix. Delete that exact filename rather
    # than the historical key[:32] guess.
    route_token = _safe_session(key)
    _unlink_regular(
        directory / f"brain-route-seen-{route_token}{_STATE_SUFFIX}"
    )


def _identity(result: os.stat_result) -> tuple[int, int]:
    return result.st_dev, result.st_ino


def _open_lock_file(path: Path) -> int:
    flags = os.O_CREAT | os.O_RDWR
    flags |= getattr(os, "O_BINARY", 0)
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(_native_path(path), flags, 0o600)
    try:
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode):
            raise OSError(f"lock target is not a regular file: {path}")
        current = _checked_lstat(path)
        if not stat.S_ISREG(current.st_mode) or _identity(current) != _identity(opened):
            raise OSError(f"lock target changed while opening: {path}")
        if os.name != "nt":
            os.fchmod(fd, 0o600)
        return fd
    except BaseException:
        os.close(fd)
        raise


def _try_lock_file(fd: int) -> bool:
    if os.name == "nt":
        import msvcrt

        if os.fstat(fd).st_size < 1:
            os.lseek(fd, 1, os.SEEK_SET)
            try:
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                if exc.errno in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                    return False
                raise
            try:
                if os.fstat(fd).st_size < 1:
                    os.lseek(fd, 0, os.SEEK_SET)
                    if os.write(fd, b"\0") != 1:
                        raise OSError("failed to initialize session lock file")
                    os.fsync(fd)
            finally:
                os.lseek(fd, 1, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        os.lseek(fd, 0, os.SEEK_SET)
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            if exc.errno in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                return False
            raise
        return True

    import fcntl

    if os.fstat(fd).st_size < 1:
        os.lseek(fd, 0, os.SEEK_SET)
        if os.write(fd, b"\0") != 1:
            raise OSError("failed to initialize session lock file")
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        if exc.errno in {errno.EACCES, errno.EAGAIN}:
            return False
        raise
    return True


def _unlock_file(fd: int) -> None:
    if os.name == "nt":
        import msvcrt

        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        return

    import fcntl

    fcntl.flock(fd, fcntl.LOCK_UN)


@contextmanager
def session_lock(session_id: str, *, timeout_s: float = 1.0) -> Iterator[None]:
    if (
        isinstance(timeout_s, bool)
        or not isinstance(timeout_s, (int, float))
        or not math.isfinite(timeout_s)
        or timeout_s < 0
    ):
        raise ValueError("timeout_s must be a non-negative finite number")
    key = session_key(session_id)
    path = _path_for_key(key, _LOCK_SUFFIX)
    deadline = time.monotonic() + timeout_s

    while True:
        fd = _open_lock_file(path)
        acquired = False
        try:
            acquired = _try_lock_file(fd)
            if acquired:
                yield
                return
        finally:
            try:
                if acquired:
                    _unlock_file(fd)
            finally:
                os.close(fd)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError(f"timed out acquiring session lock: {path.name}")
        time.sleep(min(_POLL_SECONDS, remaining))
