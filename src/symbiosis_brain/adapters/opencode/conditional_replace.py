"""Crash-recoverable, Windows-only conditional file commit helper.

The validated target is moved by handle into a journaled capture file. A new
pathname is published only with no-replace semantics, so an editor's file is
never overwritten. Recovery deliberately preserves ambiguous states.
"""

from __future__ import annotations

import base64
import ctypes
import hashlib
import json
import os
import secrets
import sys
import time
from ctypes import wintypes
from pathlib import Path

GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
DELETE = 0x00010000
FILE_SHARE_READ = 0x00000001
OPEN_EXISTING = 3
CREATE_NEW = 1
FILE_ATTRIBUTE_NORMAL = 0x80
MOVEFILE_WRITE_THROUGH = 0x8
FILE_RENAME_INFO_CLASS = 3
MAX_FILE_SIZE = 16 * 1024 * 1024
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value


def _emit(ok: bool, state: str, **extra: object) -> None:
    result: dict[str, object] = {"ok": ok, "state": state, **extra}
    if not ok:
        result["code"] = state
    print(json.dumps(result, separators=(",", ":")), flush=True)
    raise SystemExit(0 if ok else 2)


def _win_api() -> dict[str, object]:
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    api: dict[str, object] = {}
    api["create"] = k.CreateFileW
    api["create"].argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                              wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    api["create"].restype = wintypes.HANDLE
    api["close"] = k.CloseHandle
    api["close"].argtypes = [wintypes.HANDLE]
    api["close"].restype = wintypes.BOOL
    api["read"] = k.ReadFile
    api["read"].argtypes = [wintypes.HANDLE, wintypes.LPVOID, wintypes.DWORD,
                            ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID]
    api["read"].restype = wintypes.BOOL
    api["write"] = k.WriteFile
    api["write"].argtypes = [wintypes.HANDLE, wintypes.LPCVOID, wintypes.DWORD,
                             ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID]
    api["write"].restype = wintypes.BOOL
    api["flush"] = k.FlushFileBuffers
    api["flush"].argtypes = [wintypes.HANDLE]
    api["flush"].restype = wintypes.BOOL
    api["size"] = k.GetFileSizeEx
    api["size"].argtypes = [wintypes.HANDLE, ctypes.POINTER(ctypes.c_longlong)]
    api["size"].restype = wintypes.BOOL
    api["rename"] = k.SetFileInformationByHandle
    api["rename"].argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPVOID, wintypes.DWORD]
    api["rename"].restype = wintypes.BOOL
    api["move"] = k.MoveFileExW
    api["move"].argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD]
    api["move"].restype = wintypes.BOOL
    api["delete"] = k.DeleteFileW
    api["delete"].argtypes = [wintypes.LPCWSTR]
    api["delete"].restype = wintypes.BOOL
    api["attributes"] = k.GetFileAttributesW
    api["attributes"].argtypes = [wintypes.LPCWSTR]
    api["attributes"].restype = wintypes.DWORD
    return api


def _canonical_target(raw: str) -> Path:
    p = Path(raw).absolute()
    parent = p.parent.resolve(strict=True)
    target = parent / p.name
    if p.is_symlink() or not target.parent.is_dir():
        raise ValueError("invalid target parent or reparse target")
    if target.exists() and target.is_symlink():
        raise ValueError("target is a reparse point")
    return target


def _absolute_target(raw: str) -> Path:
    """Normalize a recovery target without requiring or creating its parent."""
    return Path(os.path.abspath(raw))


def _sidecars(path: Path) -> tuple[Path, Path]:
    digest = hashlib.sha256(os.path.normcase(str(path)).encode("utf-8")).hexdigest()
    stem = f".sb-commit-{digest}"
    return path.with_name(stem + ".json"), path.with_name(stem + ".capture")


def _is_reparse(api: dict[str, object], path: Path) -> bool:
    attrs = api["attributes"](str(path))
    if attrs == 0xFFFFFFFF:
        return False
    return bool(attrs & 0x400)  # FILE_ATTRIBUTE_REPARSE_POINT (symlinks, junctions, other tags)


def _check_sidecars(api: dict[str, object], journal: Path, capture: Path,
                    temp: Path | None = None) -> None:
    candidates = [journal, capture, journal.with_suffix(".committed")]
    if temp is not None:
        candidates.append(temp)
    for candidate in candidates:
        if _is_reparse(api, candidate):
            _emit(False, "recovery-conflict", recovery_path=str(candidate), detail="reparse-sidecar")


def _hash_file(path: Path) -> str | None:
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_FILE_SIZE:
            return None
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def _write_new(api: dict[str, object], path: Path, data: bytes) -> None:
    h = api["create"](str(path), GENERIC_WRITE, 0, None, CREATE_NEW, FILE_ATTRIBUTE_NORMAL, None)
    if h == INVALID_HANDLE_VALUE:
        raise OSError(ctypes.get_last_error(), "CREATE_NEW failed", str(path))
    try:
        written = wintypes.DWORD()
        if not api["write"](h, data, len(data), ctypes.byref(written), None) or written.value != len(data):
            raise OSError(ctypes.get_last_error(), "WriteFile failed", str(path))
        if not api["flush"](h):
            raise OSError(ctypes.get_last_error(), "FlushFileBuffers failed", str(path))
    finally:
        api["close"](h)


def _read_handle(api: dict[str, object], h: int) -> bytes:
    size = ctypes.c_longlong()
    if not api["size"](h, ctypes.byref(size)) or size.value < 0 or size.value > MAX_FILE_SIZE:
        raise ValueError("target size invalid")
    buf = ctypes.create_string_buffer(size.value)
    read = wintypes.DWORD()
    if size.value and (not api["read"](h, buf, size.value, ctypes.byref(read), None) or read.value != size.value):
        raise OSError(ctypes.get_last_error(), "ReadFile failed")
    return buf.raw[:size.value]


def _rename_handle(api: dict[str, object], h: int, destination: Path) -> None:
    encoded = str(destination).encode("utf-16-le")
    class RenameInfo(ctypes.Structure):
        # The first DWORD is the union of BOOLEAN ReplaceIfExists and DWORD Flags.
        _fields_ = [("Flags", wintypes.DWORD), ("RootDirectory", wintypes.HANDLE),
                    ("FileNameLength", wintypes.DWORD), ("FileName", ctypes.c_wchar * 1)]
    offset = RenameInfo.FileName.offset
    # Include a trailing WCHAR terminator as well as the documented byte length;
    # this also avoids filesystem implementations reading beyond the counted name.
    raw = ctypes.create_string_buffer(offset + len(encoded) + ctypes.sizeof(ctypes.c_wchar))
    wintypes.DWORD.from_buffer(raw, RenameInfo.Flags.offset).value = 0
    ctypes.c_void_p.from_buffer(raw, RenameInfo.RootDirectory.offset).value = None
    wintypes.DWORD.from_buffer(raw, RenameInfo.FileNameLength.offset).value = len(encoded)
    ctypes.memmove(ctypes.addressof(raw) + offset, encoded, len(encoded))
    if not api["rename"](h, FILE_RENAME_INFO_CLASS, raw, offset + len(encoded)):
        raise OSError(ctypes.get_last_error(), "SetFileInformationByHandle(FileRenameInfo) failed")


def _load_journal(journal: Path, target: Path) -> dict[str, object]:
    if journal.is_symlink() or not journal.is_file():
        raise ValueError("journal is not a regular file")
    data = json.loads(journal.read_text(encoding="utf-8"))
    expected_journal, capture = _sidecars(target)
    expected_temp = target.with_name(data.get("temp", ""))
    if (data.get("version") != 1 or os.path.normcase(data.get("target", "")) != os.path.normcase(str(target))
            or journal != expected_journal or data.get("capture") != str(capture)
            or expected_temp.parent != target.parent or expected_temp.name != data.get("temp")
            or not expected_temp.name.startswith(f".{target.name}.sb-commit-")
            or data.get("before") is not None and not _valid_hash(data["before"])
            or data.get("after") is not None and not _valid_hash(data["after"])):
        raise ValueError("journal identity invalid")
    return data


def _valid_hash(value: object) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(c in "0123456789abcdef" for c in value)


def _intent_token(intent: dict[str, object]) -> str:
    # phase is descriptive only; the committed marker authenticates immutable intent.
    stable = {key: value for key, value in intent.items() if key != "phase"}
    return hashlib.sha256(json.dumps(stable, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _recover(api: dict[str, object], target: Path, journal: Path, capture: Path) -> str:
    _check_sidecars(api, journal, capture)
    if not journal.exists():
        return "clean"
    try:
        intent = _load_journal(journal, target)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        _emit(False, "recovery-conflict", recovery_path=str(journal), detail=type(exc).__name__)
    current = _hash_file(target)
    captured = _hash_file(capture)
    before, after = intent["before"], intent["after"]
    temp = target.with_name(intent["temp"])
    marker = journal.with_suffix(".committed")
    _check_sidecars(api, journal, capture, temp)
    marker_value = _intent_token(intent)
    if marker.is_file() and not marker.is_symlink() and marker.read_text(encoding="ascii") == marker_value:
        _cleanup(api, journal, capture, temp, marker)
        return "after" if after is not None else "absent"
    if after is not None and current == after:
        _cleanup(api, journal, capture, temp, marker)
        return "after"
    if current == before and captured is None:
        _cleanup(api, journal, capture, temp)
        return "before"
    if current is None and captured == before:
        if api["move"](str(capture), str(target), MOVEFILE_WRITE_THROUGH):
            _cleanup(api, journal, capture, temp)
            return "before"
    if before is None and current is None and captured is None:
        _cleanup(api, journal, capture, temp, marker)
        return "absent"
    if after is None and current is None and captured is None and journal.exists():
        # Delete committed and cleanup had already removed its capture. Do not restore.
        _cleanup(api, journal, capture, temp, marker)
        return "absent"
    _emit(False, "recovery-conflict", recovery_path=str(capture if captured else journal),
          target_hash=current, capture_hash=captured, expected_before=before, expected_after=after)


def _cleanup(api: dict[str, object], journal: Path, capture: Path, temp: Path,
             marker: Path | None = None) -> None:
    if marker is None:
        marker = journal.with_suffix(".committed")
    _check_sidecars(api, journal, capture, temp)
    if _is_reparse(api, marker):
        _emit(False, "recovery-conflict", recovery_path=str(marker), detail="reparse-sidecar")
    if _is_reparse(api, journal):
        _emit(False, "recovery-conflict", recovery_path=str(journal), detail="reparse-sidecar")
    for item in (capture, temp, marker, journal):
        if item is None:
            continue
        if item.exists() and not api["delete"](str(item)):
            _emit(False, "recovery-conflict", recovery_path=str(item))


def _barrier(request: dict[str, object], phase: str) -> None:
    barrier = request.get("testBarrier")
    sandbox = os.environ.get("SB_TEST_SANDBOX")
    if not isinstance(barrier, dict) or not sandbox or barrier.get("phase") != phase:
        return
    root = Path(sandbox).resolve()
    ready, release = Path(str(barrier["ready"])).resolve(), Path(str(barrier["release"])).resolve()
    if root not in ready.parents or root not in release.parents:
        _emit(False, "test-barrier-outside-sandbox")
    ready.write_text(phase, encoding="utf-8")
    deadline = time.monotonic() + 30
    while not release.exists() and time.monotonic() < deadline:
        time.sleep(.01)
    if not release.exists():
        _emit(False, "test-barrier-timeout")


def main() -> None:
    if os.name != "nt":
        _emit(False, "conditional-commit-unsupported-platform")
    api: dict[str, object] | None = None
    handle = None
    try:
        request = json.load(sys.stdin)
        if request.get("operation") == "recover":
            target = _absolute_target(request["path"])
            if not target.parent.exists():
                _emit(True, "clean")
        else:
            target = _canonical_target(request["path"])
        journal, capture = _sidecars(target)
        api = _win_api()
        if request.get("operation") == "recover":
            state = _recover(api, target, journal, capture)
            _emit(True, state)
        if journal.exists():
            recovered = _recover(api, target, journal, capture)
            if recovered != "clean":
                _emit(False, "conditional-commit-recovery-required", recovered_state=recovered)
        expected = request.get("expectedSha256")
        replacement_value = request.get("replacementBase64")
        replacement = None if replacement_value is None else base64.b64decode(replacement_value, validate=True)
        if expected is not None and not _valid_hash(expected):
            _emit(False, "conditional-commit-invalid-request")
        if replacement is not None and len(replacement) > MAX_FILE_SIZE:
            _emit(False, "conditional-commit-invalid-request")
        handle = api["create"](str(target), GENERIC_READ | DELETE, FILE_SHARE_READ, None,
                               OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, None)
        if handle == INVALID_HANDLE_VALUE:
            handle = None
            error = ctypes.get_last_error()
            if error != 2 or expected is not None:
                _emit(False, "conditional-commit-conflict")
            if replacement is None:
                _emit(True, "before")
            # Initial create is still journaled and published without replacement.
            before_data = None
        else:
            before_data = None
            try:
                before_bytes = _read_handle(api, handle)
                before_data = hashlib.sha256(before_bytes).hexdigest()
                if before_data != expected:
                    _emit(False, "conditional-commit-conflict")
                _barrier(request, "validated")
            except BaseException:
                api["close"](handle)
                handle = None
                raise
        after = hashlib.sha256(replacement).hexdigest() if replacement is not None else None
        temp = target.with_name(f".{target.name}.sb-commit-{secrets.token_hex(12)}.tmp")
        _check_sidecars(api, journal, capture, temp)
        if replacement is not None:
            _write_new(api, temp, replacement)
        intent: dict[str, object] = {"version": 1, "target": str(target), "capture": str(capture),
                                     "temp": temp.name, "before": before_data, "after": after,
                                     "phase": "prepared"}
        _write_new(api, journal, json.dumps(intent, sort_keys=True, separators=(",", ":")).encode())
        _barrier(request, "prepared")
        if before_data is not None:
            _rename_handle(api, handle, capture)
            api["close"](handle)
            handle = None
        intent["phase"] = "captured"
        _barrier(request, "captured")
        if replacement is not None:
            if not api["move"](str(temp), str(target), MOVEFILE_WRITE_THROUGH):
                _emit(False, "conditional-commit-conflict", recovery_path=str(capture))
        marker = journal.with_suffix(".committed")
        marker_value = _intent_token(intent).encode("ascii")
        _write_new(api, marker, marker_value)
        intent["phase"] = "committed"
        _barrier(request, "committed")
        _cleanup(api, journal, capture, temp, marker)
        _emit(True, "after" if replacement is not None else "absent")
    except SystemExit:
        raise
    except Exception as exc:
        _emit(False, "conditional-commit-failed", detail=type(exc).__name__)
    finally:
        if handle is not None and handle != INVALID_HANDLE_VALUE and api is not None:
            api["close"](handle)


if __name__ == "__main__":
    main()
