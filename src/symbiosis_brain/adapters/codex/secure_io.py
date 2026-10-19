"""Handle-bound, read-only file access for the Codex adapter."""
from __future__ import annotations

import ntpath
import os
import stat
from pathlib import Path, PurePosixPath, PureWindowsPath


def _relative_parts(relative: str | os.PathLike[str]) -> tuple[str, ...]:
    raw = os.fspath(relative)
    if not isinstance(raw, str) or not raw or "\x00" in raw:
        raise ValueError("relative path must be a non-empty string")
    posix = PurePosixPath(raw.replace("\\", "/"))
    windows = PureWindowsPath(raw)
    if posix.is_absolute() or windows.is_absolute() or windows.drive:
        raise ValueError("path must be relative")
    parts = posix.parts
    if not parts or any(part in {"", ".", ".."} for part in parts):
        raise ValueError("path traversal is not allowed")
    return tuple(parts)


def _read_text_from_fd(fd: int) -> str:
    """Read UTF-8 from an already validated descriptor without reopening its path."""
    os.lseek(fd, 0, os.SEEK_SET)
    chunks: list[bytes] = []
    while True:
        chunk = os.read(fd, 64 * 1024)
        if not chunk:
            break
        chunks.append(chunk)
    return b"".join(chunks).decode("utf-8")


def _secure_read_posix(root: Path, parts: tuple[str, ...]) -> str:
    nofollow = getattr(os, "O_NOFOLLOW", None)
    directory = getattr(os, "O_DIRECTORY", None)
    if nofollow is None or directory is None:
        raise OSError("secure component walk is unavailable")
    common = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | nofollow
    root_fd = os.open(os.fspath(root), common | directory)
    current_fd = root_fd
    try:
        opened = os.fstat(root_fd)
        if not stat.S_ISDIR(opened.st_mode):
            raise OSError("secure read root is not a directory")
        for component in parts[:-1]:
            next_fd = os.open(
                component,
                common | directory,
                dir_fd=current_fd,
            )
            try:
                if not stat.S_ISDIR(os.fstat(next_fd).st_mode):
                    raise OSError("path component is not a directory")
            except BaseException:
                os.close(next_fd)
                raise
            if current_fd != root_fd:
                os.close(current_fd)
            current_fd = next_fd

        file_fd = os.open(
            parts[-1],
            common | getattr(os, "O_BINARY", 0),
            dir_fd=current_fd,
        )
        try:
            if not stat.S_ISREG(os.fstat(file_fd).st_mode):
                raise OSError("secure read target is not a regular file")
            return _read_text_from_fd(file_fd)
        finally:
            os.close(file_fd)
    finally:
        if current_fd != root_fd:
            os.close(current_fd)
        os.close(root_fd)


def _windows_api():
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    create_file.restype = wintypes.HANDLE

    final_path = kernel32.GetFinalPathNameByHandleW
    final_path.argtypes = [
        wintypes.HANDLE,
        wintypes.LPWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
    ]
    final_path.restype = wintypes.DWORD

    close_handle = kernel32.CloseHandle
    close_handle.argtypes = [wintypes.HANDLE]
    close_handle.restype = wintypes.BOOL
    return ctypes, create_file, final_path, close_handle


def _open_windows_handle(path: Path, *, directory: bool) -> int:
    ctypes, create_file, _final_path, _close_handle = _windows_api()
    generic_read = 0x80000000
    file_read_attributes = 0x0080
    share_read_write = 0x00000001 | 0x00000002
    share_all = share_read_write | 0x00000004
    open_existing = 3
    backup_semantics = 0x02000000
    open_reparse_point = 0x00200000
    handle = create_file(
        os.fspath(path),
        file_read_attributes if directory else generic_read | file_read_attributes,
        share_read_write if directory else share_all,
        None,
        open_existing,
        (backup_semantics if directory else 0) | open_reparse_point,
        None,
    )
    invalid = ctypes.c_void_p(-1).value
    if handle == invalid:
        error = ctypes.get_last_error()
        raise OSError(error, os.strerror(error), os.fspath(path))
    return int(handle)


def _windows_file_attributes(handle: int) -> int:
    import ctypes
    from ctypes import wintypes

    class FileAttributeTagInfo(ctypes.Structure):
        _fields_ = [
            ("file_attributes", wintypes.DWORD),
            ("reparse_tag", wintypes.DWORD),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    get_file_information = kernel32.GetFileInformationByHandleEx
    get_file_information.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
    ]
    get_file_information.restype = wintypes.BOOL
    info = FileAttributeTagInfo()
    file_attribute_tag_info = 9
    if not get_file_information(
        handle,
        file_attribute_tag_info,
        ctypes.byref(info),
        ctypes.sizeof(info),
    ):
        error = ctypes.get_last_error()
        raise OSError(error, os.strerror(error))
    return int(info.file_attributes)


def _reject_windows_reparse_point(handle: int) -> None:
    file_attribute_reparse_point = 0x00000400
    if _windows_file_attributes(handle) & file_attribute_reparse_point:
        raise OSError("secure read target is a reparse point")


def _windows_final_path(handle: int) -> str:
    ctypes, _create_file, final_path, _close_handle = _windows_api()
    size = 1024
    while True:
        buffer = ctypes.create_unicode_buffer(size)
        length = final_path(handle, buffer, size, 0)
        if length == 0:
            error = ctypes.get_last_error()
            raise OSError(error, os.strerror(error))
        if length < size:
            return buffer.value
        size = length + 1


def _close_windows_handle(handle: int) -> None:
    ctypes, _create_file, _final_path, close_handle = _windows_api()
    if not close_handle(handle):
        error = ctypes.get_last_error()
        raise OSError(error, os.strerror(error))


def _windows_contains(root: str, candidate: str) -> bool:
    normalized_root = ntpath.normcase(ntpath.normpath(root))
    normalized_candidate = ntpath.normcase(ntpath.normpath(candidate))
    try:
        common = ntpath.commonpath([normalized_root, normalized_candidate])
    except ValueError:
        return False
    return common == normalized_root and normalized_candidate != normalized_root


def _same_windows_path(left: str, right: str) -> bool:
    return ntpath.normcase(ntpath.normpath(left)) == ntpath.normcase(
        ntpath.normpath(right)
    )


def _secure_read_windows(root: Path, parts: tuple[str, ...]) -> str:
    import msvcrt

    root_handle = _open_windows_handle(root, directory=True)
    file_handle: int | None = None
    fd: int | None = None
    try:
        _reject_windows_reparse_point(root_handle)
        root_final = _windows_final_path(root_handle)
        candidate = root.joinpath(*parts)
        file_handle = _open_windows_handle(candidate, directory=False)
        _reject_windows_reparse_point(file_handle)
        candidate_final = _windows_final_path(file_handle)
        if not _same_windows_path(root_final, _windows_final_path(root_handle)):
            raise OSError("secure read root changed while opening target")
        if not _windows_contains(root_final, candidate_final):
            raise OSError("secure read target escaped its root")

        fd = msvcrt.open_osfhandle(
            file_handle,
            os.O_RDONLY | getattr(os, "O_BINARY", 0),
        )
        file_handle = None  # ownership transferred to the CRT descriptor
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError("secure read target is not a regular file")
        return _read_text_from_fd(fd)
    finally:
        if fd is not None:
            os.close(fd)
        if file_handle is not None:
            _close_windows_handle(file_handle)
        _close_windows_handle(root_handle)


def secure_read_text(
    root: Path,
    relative: str | os.PathLike[str],
) -> str:
    """Read one root-relative UTF-8 file from the same handle that was validated.

    POSIX walks every component with ``openat``/``dir_fd`` and ``O_NOFOLLOW``.
    Windows opens root and file handles with ``CreateFileW``, checks the final
    handle path is contained by the opened root, then reads through that handle.
    """
    root_path = Path(root)
    if not root_path.is_absolute():
        raise ValueError("secure read root must be absolute")
    parts = _relative_parts(relative)
    if os.name == "nt":
        return _secure_read_windows(root_path, parts)
    return _secure_read_posix(root_path, parts)
