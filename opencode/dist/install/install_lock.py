"""Hold an OS advisory lock until the owning installer closes stdin."""
import errno
import base64
import contextlib
import importlib.util
import io
import json
import os
import secrets
import stat
import sys
import time
from pathlib import Path


def _assert_physical(path: str, allow_missing: bool = True) -> None:
    absolute = Path(os.path.abspath(path))
    parts = absolute.parts
    current = Path(parts[0])
    for index, part in enumerate(parts[1:]):
        current = current / part
        try:
            info = os.lstat(current)
        except FileNotFoundError:
            if allow_missing:
                break
            raise
        reparse = getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        if stat.S_ISLNK(info.st_mode) or reparse:
            raise ValueError("reparse path rejected")
        if index < len(parts) - 2 and not stat.S_ISDIR(info.st_mode):
            raise ValueError("non-directory parent rejected")


def _io_path(path: str) -> str:
    """Use Win32 extended-length syntax for validated absolute paths on Windows."""
    absolute = os.path.abspath(path)
    if os.name != "nt" or absolute.startswith("\\\\?\\"):
        return absolute
    if absolute.startswith("\\\\"):
        return "\\\\?\\UNC\\" + absolute[2:]
    return "\\\\?\\" + absolute


def _test_gate(variable: str) -> None:
    base_value = os.environ.get(variable)
    sandbox = os.environ.get("SB_TEST_SANDBOX")
    if not base_value or not sandbox:
        return
    base = Path(base_value).resolve()
    root = Path(sandbox).resolve()
    if root not in base.parents:
        raise ValueError("test gate outside sandbox")
    entered = base.with_name(base.name + ".entered")
    proceed = base.with_name(base.name + ".proceed")
    entered.write_text("entered", encoding="utf-8")
    deadline = time.monotonic() + 30
    while not proceed.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    if not proceed.exists():
        raise TimeoutError("test gate timed out")


def _rpc_test_gate(path: str, lock_path: str) -> None:
    sandbox = os.environ.get("SB_TEST_SANDBOX")
    if not sandbox:
        raise ValueError("RPC test gate requires verified sandbox")
    gate = Path(path).resolve()
    lock_root = Path(lock_path).resolve().parent
    sandbox_root = Path(sandbox).resolve()
    if lock_root not in gate.parents or sandbox_root not in gate.parents:
        raise ValueError("RPC test gate outside locked sandbox")
    proceed = gate.with_name(gate.name + ".proceed")
    gate.write_text("entered", encoding="utf-8")
    deadline = time.monotonic() + 30
    while not proceed.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    if not proceed.exists():
        raise TimeoutError("RPC test gate timed out")


def main() -> int:
    path = sys.argv[1]
    package_dir = Path(sys.argv[2]).resolve(strict=True)
    _assert_physical(str(package_dir), allow_missing=False)
    sys.path.insert(0, str(package_dir.parent))
    package_spec = importlib.util.find_spec("symbiosis_brain")
    if not package_spec or not package_spec.submodule_search_locations:
        raise RuntimeError("expected package not importable")
    actual_package = Path(next(iter(package_spec.submodule_search_locations))).resolve(strict=True)
    if os.path.normcase(str(actual_package)) != os.path.normcase(str(package_dir)):
        raise RuntimeError("package origin mismatch")
    _assert_physical(os.path.dirname(path))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    _assert_physical(path)
    handle = open(path, "a+b")
    try:
        if os.name == "nt":
            import msvcrt

            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                if exc.errno in (errno.EACCES, errno.EDEADLK, errno.EAGAIN):
                    print("LOCKED", file=sys.stderr, flush=True)
                    return 3
                raise
            unlock = lambda: (handle.seek(0), msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1))
        else:
            import fcntl

            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                print("LOCKED", file=sys.stderr, flush=True)
                return 3
            unlock = lambda: fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        print("READY " + json.dumps({"package": str(actual_package)}, separators=(",", ":")), flush=True)
        for line in sys.stdin:
            try:
                request = json.loads(line)
                request_id = request.get("id")
                operation = request.get("operation")
                if operation == "release":
                    print(json.dumps({"id": request_id, "ok": True}), flush=True)
                    _test_gate("SB_TEST_INSTALL_LOCK_RELEASE_GATE")
                    break
                if operation == "mkdir":
                    _assert_physical(request["path"])
                    os.makedirs(request["path"], exist_ok=True)
                    response = {"ok": True}
                elif operation == "write":
                    path = Path(request["path"])
                    _assert_physical(str(path))
                    if request.get("testGate"):
                        _rpc_test_gate(request["testGate"], sys.argv[1])
                    else:
                        _test_gate("SB_TEST_INSTALL_LOCK_WRITE_GATE")
                    # Keep the sibling basename short: backup names may already
                    # contain a digest and approach the legacy MAX_PATH limit.
                    temp = path.with_name(f".sb-lock-{secrets.token_hex(12)}.tmp")
                    data = base64.b64decode(request["data"], validate=True)
                    with open(_io_path(str(temp)), "xb") as output:
                        output.write(data)
                        output.flush()
                        os.fsync(output.fileno())
                    os.replace(_io_path(str(temp)), _io_path(str(path)))
                    response = {"ok": True}
                elif operation == "remove":
                    _assert_physical(request["path"])
                    try:
                        os.unlink(request["path"])
                    except FileNotFoundError:
                        pass
                    response = {"ok": True}
                elif operation in ("conditional", "recover"):
                    from symbiosis_brain.adapters.opencode import conditional_replace

                    expected_helper = package_dir / "adapters" / "opencode" / "conditional_replace.py"
                    actual_helper = Path(conditional_replace.__file__).resolve(strict=True)
                    if os.path.normcase(str(actual_helper)) != os.path.normcase(str(expected_helper.resolve(strict=True))):
                        raise RuntimeError("conditional helper origin mismatch")

                    payload = dict(request["payload"])
                    if operation == "recover":
                        payload["operation"] = "recover"
                    old_stdin = conditional_replace.sys.stdin
                    output = io.StringIO()
                    conditional_replace.sys.stdin = io.StringIO(json.dumps(payload))
                    try:
                        with contextlib.redirect_stdout(output):
                            conditional_replace.main()
                        result = json.loads(output.getvalue())
                        response = {"ok": result.get("ok") is True, "result": result}
                    except SystemExit as exc:
                        try:
                            result = json.loads(output.getvalue())
                            response = {"ok": exc.code == 0 and result.get("ok") is True, "result": result, "exit": exc.code}
                        except json.JSONDecodeError:
                            response = {"ok": False, "error": "conditional-helper-failed"}
                    finally:
                        conditional_replace.sys.stdin = old_stdin
                else:
                    response = {"ok": False, "error": "unknown-lock-operation"}
                print(json.dumps({"id": request_id, **response}, separators=(",", ":")), flush=True)
            except Exception as exc:
                print(json.dumps({"id": request.get("id") if isinstance(request, dict) else None,
                                  "operation": request.get("operation") if isinstance(request, dict) else None,
                                  "path": request.get("path") if isinstance(request, dict) else None,
                                  "ok": False, "error": type(exc).__name__}), flush=True)
        unlock()
        return 0
    finally:
        handle.close()


if __name__ == "__main__":
    raise SystemExit(main())
