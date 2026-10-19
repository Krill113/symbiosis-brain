from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from symbiosis_brain.adapters.codex.protocol import HookEvent
from symbiosis_brain.adapters.codex.session_state import (
    SessionState,
    load_state,
    save_state,
    session_key,
)


SOURCE_ROOT = Path(__file__).resolve().parents[2] / "src"
REPO_ROOT = SOURCE_ROOT.parent
ADAPTER_IMPORT = (
    "import sys,time;"
    "sys.path.insert(0, sys.argv.pop(1));"
    "from symbiosis_brain.adapters.codex import supervisor as adapter;"
)


def _python(*args: str, **kwargs) -> subprocess.Popen[str]:
    creationflags = (
        getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    )
    return subprocess.Popen(
        [sys.executable, "-I", "-B", "-c", *args],
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=creationflags,
        **kwargs,
    )


def test_hook_template_passes_event_before_stdin_is_read():
    template = json.loads(
        (REPO_ROOT / "templates" / "codex" / "hooks.json").read_text(
            encoding="utf-8"
        )
    )

    for event in HookEvent:
        command = template["hooks"][event.value][0]["hooks"][0]
        assert command["command"] == f"{{{{COMMAND}}}} --event {event.value}"
        assert (
            command["commandWindows"]
            == f"{{{{COMMAND_WINDOWS}}}} --event {event.value}"
        )


def test_whole_cli_deadline_covers_blocked_stdin_and_emits_fail_open():
    code = (
        ADAPTER_IMPORT
        + "adapter._INTERNAL_DEADLINES['SessionEnd']=0.2;"
        + "result=adapter.run_cli(['--vault',sys.argv[1],"
        "'--event','SessionEnd']);"
        + "raise SystemExit(result)"
    )
    started = time.monotonic()
    process = _python(
        code,
        str(SOURCE_ROOT),
        str(REPO_ROOT / "synthetic-vault"),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    process.wait(timeout=2)
    elapsed = time.monotonic() - started
    stdout = process.stdout.read() if process.stdout is not None else ""
    stderr = process.stderr.read() if process.stderr is not None else ""

    assert process.returncode == 0, stderr
    assert elapsed < 1.0
    assert json.loads(stdout) == {"continue": True}


def test_deadline_kills_stalled_config_worker_before_it_can_mutate(tmp_path: Path):
    late_marker = tmp_path / "late-config-mutation"
    helper = textwrap.dedent(
        f"""
        import sys
        import time
        from pathlib import Path
        sys.path.insert(0, sys.argv.pop(1))
        from symbiosis_brain.adapters.codex import adapter

        def slow_config():
            time.sleep(0.6)
            Path({str(late_marker)!r}).write_text("late", encoding="utf-8")
            raise RuntimeError("late config")

        adapter.load_codex_config = slow_config
        raise SystemExit(adapter._worker_main(sys.argv[1:]))
        """
    )
    outer = textwrap.dedent(
        f"""
        import io
        import json
        import sys
        import time
        sys.path.insert(0, sys.argv.pop(1))
        from symbiosis_brain.adapters.codex import supervisor as adapter

        adapter._INTERNAL_DEADLINES["SessionEnd"] = 0.15
        adapter._worker_command = lambda deadline: [
            sys.executable, "-I", "-B", "-c", {helper!r}, {str(SOURCE_ROOT)!r}
        ]
        sys.stdin = io.StringIO(json.dumps({{
            "hook_event_name": "SessionEnd",
            "session_id": "deadline-config",
        }}))
        started = time.monotonic()
        code = adapter.run_cli([
            "--vault", sys.argv[1], "--event", "SessionEnd"
        ])
        sys.stderr.write(f"INNER_ELAPSED={{time.monotonic() - started}}\\n")
        raise SystemExit(code)
        """
    )
    completed = subprocess.run(
        [
            sys.executable,
            "-I",
            "-B",
            "-c",
            outer,
            str(SOURCE_ROOT),
            str(tmp_path / "vault"),
        ],
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        timeout=2,
        check=False,
    )
    elapsed = float(
        completed.stderr.strip().split("INNER_ELAPSED=", 1)[1].splitlines()[0]
    )
    time.sleep(0.7)

    assert completed.returncode == 0, completed.stderr
    assert elapsed < 1.0
    assert json.loads(completed.stdout) == {"continue": True}
    assert not late_marker.exists()


def test_parent_does_not_resolve_vault_path_outside_deadline_worker(tmp_path: Path):
    late_marker = tmp_path / "late-parent-path-resolution"
    helper = (
        "import json,sys;"
        "sys.stdin.read();"
        "sys.stdout.write(json.dumps({'continue':True})+'\\n')"
    )
    outer = textwrap.dedent(
        f"""
        import io
        import json
        import sys
        import time
        from pathlib import Path
        sys.path.insert(0, sys.argv.pop(1))
        from symbiosis_brain.adapters.codex import supervisor as adapter

        def slow_resolve(self, *args, **kwargs):
            time.sleep(0.6)
            Path({str(late_marker)!r}).write_text("late", encoding="utf-8")
            return self

        adapter.Path.resolve = slow_resolve
        adapter._INTERNAL_DEADLINES["SessionEnd"] = 0.15
        adapter._worker_command = lambda deadline: [
            sys.executable, "-I", "-B", "-c", {helper!r}
        ]
        sys.stdin = io.StringIO(json.dumps({{
            "hook_event_name": "SessionEnd",
            "session_id": "deadline-parent-path",
        }}))
        started = time.monotonic()
        code = adapter.run_cli([
            "--vault", "relative-vault", "--event", "SessionEnd"
        ])
        sys.stderr.write(f"INNER_ELAPSED={{time.monotonic() - started}}\\n")
        raise SystemExit(code)
        """
    )
    completed = subprocess.run(
        [sys.executable, "-I", "-B", "-c", outer, str(SOURCE_ROOT)],
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        timeout=2,
        check=False,
    )
    elapsed = float(
        completed.stderr.strip().split("INNER_ELAPSED=", 1)[1].splitlines()[0]
    )

    assert completed.returncode == 0, completed.stderr
    assert elapsed < 0.5
    assert json.loads(completed.stdout) == {"continue": True}
    assert not late_marker.exists()


def test_deadline_kills_stalled_scope_worker_before_it_can_mutate(tmp_path: Path):
    late_marker = tmp_path / "late-scope-mutation"
    helper = textwrap.dedent(
        f"""
        import sys
        import time
        from pathlib import Path
        from types import SimpleNamespace
        sys.path.insert(0, sys.argv.pop(1))
        from symbiosis_brain.adapters.codex import adapter
        from symbiosis_brain.pre_action_config import PreActionConfig

        adapter.load_codex_config = lambda: SimpleNamespace(
            bootstrap_visibility="silent",
            handoff_stale_days=30,
            save_reminder_turns=10,
            status_line_policy="disabled",
            sync_mode="manual",
            sync_bash_path=None,
            recall=PreActionConfig(),
            trusted_projects=(),
        )

        def slow_scope(*args, **kwargs):
            time.sleep(0.6)
            Path({str(late_marker)!r}).write_text("late", encoding="utf-8")
            raise RuntimeError("late scope")

        adapter.resolve_scope = slow_scope
        raise SystemExit(adapter._worker_main(sys.argv[1:]))
        """
    )
    outer = textwrap.dedent(
        f"""
        import io
        import json
        import sys
        import time
        sys.path.insert(0, sys.argv.pop(1))
        from symbiosis_brain.adapters.codex import supervisor as adapter

        adapter._INTERNAL_DEADLINES["SessionStart"] = 0.15
        adapter._worker_command = lambda deadline: [
            sys.executable, "-I", "-B", "-c", {helper!r}, {str(SOURCE_ROOT)!r}
        ]
        sys.stdin = io.StringIO(json.dumps({{
            "hook_event_name": "SessionStart",
            "session_id": "deadline-scope",
            "cwd": sys.argv[1],
        }}))
        started = time.monotonic()
        code = adapter.run_cli([
            "--vault", sys.argv[2], "--event", "SessionStart"
        ])
        sys.stderr.write(f"INNER_ELAPSED={{time.monotonic() - started}}\\n")
        raise SystemExit(code)
        """
    )
    completed = subprocess.run(
        [
            sys.executable,
            "-I",
            "-B",
            "-c",
            outer,
            str(SOURCE_ROOT),
            str(tmp_path),
            str(tmp_path / "vault"),
        ],
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        timeout=2,
        check=False,
    )
    elapsed = float(
        completed.stderr.strip().split("INNER_ELAPSED=", 1)[1].splitlines()[0]
    )
    time.sleep(0.7)

    assert completed.returncode == 0, completed.stderr
    assert elapsed < 1.0
    assert json.loads(completed.stdout) == {"continue": True}
    assert not late_marker.exists()


def test_supervisor_import_is_cold_and_import_stall_is_inside_deadline(
    tmp_path: Path,
):
    late_marker = tmp_path / "late-import-mutation"
    helper = textwrap.dedent(
        f"""
        import sys
        import time
        from pathlib import Path

        raw = sys.stdin.buffer.read()
        if not raw:
            raise SystemExit(0)
        time.sleep(0.6)
        Path({str(late_marker)!r}).write_text("late", encoding="utf-8")
        """
    )
    outer = textwrap.dedent(
        f"""
        import io
        import json
        import sys
        import time
        sys.path.insert(0, sys.argv.pop(1))
        from symbiosis_brain.adapters.codex import supervisor

        allowed_cold_modules = {{
            "symbiosis_brain",
            "symbiosis_brain.adapters",
            "symbiosis_brain.adapters.codex",
            "symbiosis_brain.adapters.codex.supervisor",
        }}
        unexpected_product_modules = sorted(
            name for name in sys.modules
            if name.startswith("symbiosis_brain")
            and name not in allowed_cold_modules
        )
        supervisor._INTERNAL_DEADLINES["SessionEnd"] = 0.15
        supervisor._worker_command = lambda deadline: [
            sys.executable, "-I", "-B", "-c", {helper!r}
        ]
        sys.stdin = io.StringIO(json.dumps({{
            "hook_event_name": "SessionEnd",
            "session_id": "deadline-import",
        }}))
        started = time.monotonic()
        code = supervisor.run_cli([
            "--vault", sys.argv[1], "--event", "SessionEnd"
        ])
        sys.stderr.write(
            f"COLD={{int(not unexpected_product_modules)}} "
            f"INNER_ELAPSED={{time.monotonic() - started}}\\n"
        )
        raise SystemExit(code)
        """
    )
    completed = subprocess.run(
        [
            sys.executable,
            "-I",
            "-B",
            "-c",
            outer,
            str(SOURCE_ROOT),
            str(tmp_path / "vault"),
        ],
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        timeout=2,
        check=False,
    )
    time.sleep(0.7)

    assert completed.returncode == 0, completed.stderr
    assert "COLD=1" in completed.stderr
    elapsed = float(
        completed.stderr.split("INNER_ELAPSED=", 1)[1].splitlines()[0]
    )
    assert elapsed < 1.0
    assert json.loads(completed.stdout) == {"continue": True}
    assert not late_marker.exists()


def test_delayed_process_launch_never_feeds_late_worker(tmp_path: Path):
    late_marker = tmp_path / "late-launch-mutation"
    helper = textwrap.dedent(
        f"""
        import sys
        from pathlib import Path

        raw = sys.stdin.buffer.read()
        if raw:
            Path({str(late_marker)!r}).write_text("late", encoding="utf-8")
        """
    )
    outer = textwrap.dedent(
        f"""
        import io
        import json
        import sys
        import time
        sys.path.insert(0, sys.argv.pop(1))
        from symbiosis_brain.adapters.codex import supervisor

        real_popen = supervisor.subprocess.Popen
        def delayed_popen(*args, **kwargs):
            time.sleep(0.35)
            return real_popen(*args, **kwargs)

        supervisor.subprocess.Popen = delayed_popen
        supervisor._INTERNAL_DEADLINES["SessionEnd"] = 0.1
        supervisor._worker_command = lambda deadline: [
            sys.executable, "-I", "-B", "-c", {helper!r}
        ]
        sys.stdin = io.StringIO(json.dumps({{
            "hook_event_name": "SessionEnd",
            "session_id": "deadline-launch",
        }}))
        code = supervisor.run_cli([
            "--vault", sys.argv[1], "--event", "SessionEnd"
        ])
        time.sleep(0.7)
        raise SystemExit(code)
        """
    )
    completed = subprocess.run(
        [
            sys.executable,
            "-I",
            "-B",
            "-c",
            outer,
            str(SOURCE_ROOT),
            str(tmp_path / "vault"),
        ],
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        timeout=2,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout) == {"continue": True}
    assert not late_marker.exists()


def test_timeout_kills_descendant_after_worker_leader_exits(tmp_path: Path):
    late_marker = tmp_path / "late-descendant-mutation"
    descendant = textwrap.dedent(
        f"""
        import time
        from pathlib import Path
        time.sleep(0.5)
        Path({str(late_marker)!r}).write_text("late", encoding="utf-8")
        """
    )
    helper = textwrap.dedent(
        f"""
        import subprocess
        import sys
        import time

        if not sys.stdin.buffer.read():
            raise SystemExit(0)
        subprocess.Popen([
            sys.executable, "-I", "-B", "-c", {descendant!r}
        ])
        time.sleep(0.12)
        """
    )
    outer = textwrap.dedent(
        f"""
        import io
        import json
        import sys
        sys.path.insert(0, sys.argv.pop(1))
        from symbiosis_brain.adapters.codex import supervisor

        supervisor._INTERNAL_DEADLINES["SessionEnd"] = 0.15
        supervisor._worker_command = lambda deadline: [
            sys.executable, "-I", "-B", "-c", {helper!r}
        ]
        sys.stdin = io.StringIO(json.dumps({{
            "hook_event_name": "SessionEnd",
            "session_id": "deadline-descendant",
        }}))
        raise SystemExit(supervisor.run_cli([
            "--vault", sys.argv[1], "--event", "SessionEnd"
        ]))
        """
    )
    completed = subprocess.run(
        [
            sys.executable,
            "-I",
            "-B",
            "-c",
            outer,
            str(SOURCE_ROOT),
            str(tmp_path / "vault"),
        ],
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        timeout=2,
        check=False,
    )
    time.sleep(0.7)

    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout) == {"continue": True}
    assert not late_marker.exists()


def test_successful_worker_does_not_kill_intentional_detached_child(tmp_path: Path):
    child_marker = tmp_path / "successful-detached-child"
    child = textwrap.dedent(
        f"""
        import time
        from pathlib import Path
        time.sleep(0.2)
        Path({str(child_marker)!r}).write_text("complete", encoding="utf-8")
        """
    )
    helper = textwrap.dedent(
        f"""
        import json
        import subprocess
        import sys

        if not sys.stdin.buffer.read():
            raise SystemExit(0)
        subprocess.Popen([
            sys.executable, "-I", "-B", "-c", {child!r}
        ])
        sys.stdout.write(json.dumps({{"continue": True}}) + "\\n")
        """
    )
    outer = textwrap.dedent(
        f"""
        import io
        import json
        import sys
        sys.path.insert(0, sys.argv.pop(1))
        from symbiosis_brain.adapters.codex import supervisor

        supervisor._INTERNAL_DEADLINES["SessionEnd"] = 1.0
        supervisor._worker_command = lambda deadline: [
            sys.executable, "-I", "-B", "-c", {helper!r}
        ]
        sys.stdin = io.StringIO(json.dumps({{
            "hook_event_name": "SessionEnd",
            "session_id": "successful-detached",
        }}))
        raise SystemExit(supervisor.run_cli([
            "--vault", sys.argv[1], "--event", "SessionEnd"
        ]))
        """
    )
    completed = subprocess.run(
        [
            sys.executable,
            "-I",
            "-B",
            "-c",
            outer,
            str(SOURCE_ROOT),
            str(tmp_path / "vault"),
        ],
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        timeout=2,
        check=False,
    )
    deadline = time.monotonic() + 1
    while not child_marker.exists() and time.monotonic() < deadline:
        time.sleep(0.01)

    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout) == {"continue": True}
    assert child_marker.read_text(encoding="utf-8") == "complete"


@pytest.mark.skipif(os.name == "nt", reason="POSIX process-group regression")
def test_posix_timeout_kills_real_adapter_prewarm_child(tmp_path: Path):
    late_marker = tmp_path / "late-prewarm-child"
    child = textwrap.dedent(
        f"""
        import time
        from pathlib import Path
        time.sleep(0.5)
        Path({str(late_marker)!r}).write_text("late", encoding="utf-8")
        """
    )
    helper = textwrap.dedent(
        f"""
        import subprocess
        import sys
        import time
        from pathlib import Path
        sys.path.insert(0, sys.argv.pop(1))
        from symbiosis_brain.adapters.codex import adapter

        real_popen = adapter.subprocess.Popen
        def intercept(command, **kwargs):
            return real_popen(
                [sys.executable, "-I", "-B", "-c", {child!r}],
                **kwargs,
            )

        adapter.subprocess.Popen = intercept
        adapter._launch_prewarm(Path(sys.argv[1]))
        time.sleep(1.0)
        """
    )
    outer = textwrap.dedent(
        f"""
        import io
        import json
        import sys
        sys.path.insert(0, sys.argv.pop(1))
        from symbiosis_brain.adapters.codex import supervisor

        supervisor._INTERNAL_DEADLINES["SessionStart"] = 0.15
        supervisor._worker_command = lambda deadline: [
            sys.executable, "-I", "-B", "-c", {helper!r}, {str(SOURCE_ROOT)!r}
        ]
        sys.stdin = io.StringIO(json.dumps({{
            "hook_event_name": "SessionStart",
            "session_id": "prewarm-timeout",
        }}))
        raise SystemExit(supervisor.run_cli([
            "--vault", sys.argv[1], "--event", "SessionStart"
        ]))
        """
    )
    completed = subprocess.run(
        [
            sys.executable,
            "-I",
            "-B",
            "-c",
            outer,
            str(SOURCE_ROOT),
            str(tmp_path / "vault"),
        ],
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        timeout=2,
        check=False,
    )
    time.sleep(0.7)

    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout) == {"continue": True}
    assert not late_marker.exists()


def _windows_lifecycle_helper(mode: str, marker: Path, *, success: bool) -> str:
    child = textwrap.dedent(
        f"""
        import time
        from pathlib import Path
        time.sleep(0.5)
        Path({str(marker)!r}).write_text("late", encoding="utf-8")
        """
    )
    if mode == "prewarm":
        launch = textwrap.dedent(
            """
            from symbiosis_brain.adapters.codex import adapter
            real_popen = adapter.subprocess.Popen
            adapter.subprocess.Popen = lambda command, **kwargs: real_popen(
                [sys.executable, "-I", "-B", "-c", child], **kwargs
            )
            adapter._launch_prewarm(Path(sys.argv[1]))
            """
        )
    else:
        launch = textwrap.dedent(
            """
            from symbiosis_brain.adapters.codex import sync_launcher
            real_popen = sync_launcher.subprocess.Popen
            sync_launcher.find_bash = lambda **kwargs: Path("bash.exe")
            sync_launcher.packaged_sync_script = lambda: Path("brain-sync.sh")
            sync_launcher.subprocess.Popen = lambda command, **kwargs: real_popen(
                [sys.executable, "-I", "-B", "-c", child], **kwargs
            )
            sync_launcher.launch_auto_sync(Path(sys.argv[1]), detach=False)
            """
        )
    ending = (
        'print(json.dumps({"continue": True}), flush=True)'
        if success
        else "time.sleep(1.0)"
    )
    return (
        "import json\n"
        "import sys\n"
        "import time\n"
        "from pathlib import Path\n"
        "sys.path.insert(0, sys.argv.pop(1))\n"
        f"child = {child!r}\n"
        f"{launch}"
        f"{ending}\n"
    )


@pytest.mark.skipif(os.name != "nt", reason="Windows Job regression")
@pytest.mark.parametrize("mode", ["prewarm", "sync"])
def test_windows_timeout_kills_real_supervised_lifecycle_child(
    tmp_path: Path,
    mode: str,
):
    late_marker = tmp_path / f"late-{mode}-child"
    helper = _windows_lifecycle_helper(mode, late_marker, success=False)
    outer = textwrap.dedent(
        f"""
        import io
        import json
        import sys
        sys.path.insert(0, sys.argv.pop(1))
        from symbiosis_brain.adapters.codex import supervisor

        supervisor._INTERNAL_DEADLINES["SessionEnd"] = 0.15
        supervisor._worker_command = lambda deadline: [
            sys.executable, "-I", "-B", "-c", {helper!r}, {str(SOURCE_ROOT)!r}
        ]
        sys.stdin = io.StringIO(json.dumps({{
            "hook_event_name": "SessionEnd",
            "session_id": "windows-{mode}-timeout",
        }}))
        raise SystemExit(supervisor.run_cli([
            "--vault", sys.argv[1], "--event", "SessionEnd"
        ]))
        """
    )
    completed = subprocess.run(
        [
            sys.executable,
            "-I",
            "-B",
            "-c",
            outer,
            str(SOURCE_ROOT),
            str(tmp_path / "vault"),
        ],
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        timeout=2,
        check=False,
    )
    time.sleep(0.7)

    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout) == {"continue": True}
    assert not late_marker.exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows Job regression")
@pytest.mark.parametrize("mode", ["prewarm", "sync"])
def test_windows_success_preserves_real_supervised_lifecycle_child(
    tmp_path: Path,
    mode: str,
):
    child_marker = tmp_path / f"successful-{mode}-child"
    helper = _windows_lifecycle_helper(mode, child_marker, success=True)
    outer = textwrap.dedent(
        f"""
        import io
        import json
        import sys
        sys.path.insert(0, sys.argv.pop(1))
        from symbiosis_brain.adapters.codex import supervisor

        supervisor._INTERNAL_DEADLINES["SessionEnd"] = 1.0
        supervisor._worker_command = lambda deadline: [
            sys.executable, "-I", "-B", "-c", {helper!r}, {str(SOURCE_ROOT)!r}
        ]
        sys.stdin = io.StringIO(json.dumps({{
            "hook_event_name": "SessionEnd",
            "session_id": "windows-{mode}-success",
        }}))
        raise SystemExit(supervisor.run_cli([
            "--vault", sys.argv[1], "--event", "SessionEnd"
        ]))
        """
    )
    completed = subprocess.run(
        [
            sys.executable,
            "-I",
            "-B",
            "-c",
            outer,
            str(SOURCE_ROOT),
            str(tmp_path / "vault"),
        ],
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        timeout=2,
        check=False,
    )
    deadline = time.monotonic() + 1
    while not child_marker.exists() and time.monotonic() < deadline:
        time.sleep(0.01)

    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout) == {"continue": True}
    assert child_marker.read_text(encoding="utf-8") == "late"


@pytest.mark.skipif(os.name != "nt", reason="Windows Job regression")
def test_windows_external_supervisor_kill_terminates_worker_descendants(
    tmp_path: Path,
):
    worker_ready = tmp_path / "worker-ready"
    late_marker = tmp_path / "late-external-kill-child"
    descendant = textwrap.dedent(
        f"""
        import time
        from pathlib import Path
        time.sleep(0.6)
        Path({str(late_marker)!r}).write_text("late", encoding="utf-8")
        """
    )
    helper = textwrap.dedent(
        f"""
        import subprocess
        import sys
        import time
        from pathlib import Path

        if not sys.stdin.buffer.read():
            raise SystemExit(0)
        subprocess.Popen([
            sys.executable, "-I", "-B", "-c", {descendant!r}
        ])
        Path({str(worker_ready)!r}).write_text("ready", encoding="utf-8")
        time.sleep(5)
        """
    )
    outer = textwrap.dedent(
        f"""
        import sys
        sys.path.insert(0, sys.argv.pop(1))
        from symbiosis_brain.adapters.codex import supervisor

        supervisor._INTERNAL_DEADLINES["SessionEnd"] = 10.0
        supervisor._worker_command = lambda deadline: [
            sys.executable, "-I", "-B", "-c", {helper!r}
        ]
        raise SystemExit(supervisor.run_cli([
            "--vault", sys.argv[1], "--event", "SessionEnd"
        ]))
        """
    )
    process = _python(
        outer,
        str(SOURCE_ROOT),
        str(tmp_path / "vault"),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert process.stdin is not None
    process.stdin.write(
        json.dumps({
            "hook_event_name": "SessionEnd",
            "session_id": "external-supervisor-kill",
        })
    )
    process.stdin.close()
    deadline = time.monotonic() + 3
    while not worker_ready.exists():
        assert process.poll() is None
        assert time.monotonic() < deadline
        time.sleep(0.01)

    process.kill()
    process.wait(timeout=2)
    time.sleep(0.8)

    assert not late_marker.exists()


def test_supervisor_enforces_stdin_limit_in_utf8_bytes(tmp_path: Path):
    launched = tmp_path / "worker-launched"
    payload = json.dumps(
        {
            "hook_event_name": "SessionEnd",
            "session_id": "unicode-" + ("😀" * 40),
        },
        ensure_ascii=False,
    )
    limit = len(payload)
    assert len(payload.encode("utf-8")) > limit
    helper = (
        "from pathlib import Path;"
        f"Path({str(launched)!r}).write_text('launched',encoding='utf-8')"
    )
    outer = textwrap.dedent(
        f"""
        import io
        import sys
        sys.path.insert(0, sys.argv.pop(1))
        from symbiosis_brain.adapters.codex import supervisor

        supervisor._MAX_STDIN_BYTES = {limit}
        supervisor._worker_command = lambda deadline: [
            sys.executable, "-I", "-B", "-c", {helper!r}
        ]
        sys.stdin = io.StringIO({payload!r})
        raise SystemExit(supervisor.run_cli([
            "--vault", sys.argv[1], "--event", "SessionEnd"
        ]))
        """
    )
    completed = subprocess.run(
        [
            sys.executable,
            "-I",
            "-B",
            "-c",
            outer,
            str(SOURCE_ROOT),
            str(tmp_path / "vault"),
        ],
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        timeout=2,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout) == {}
    assert not launched.exists()


def test_save_before_stop_serializes_across_independent_processes(
    tmp_path: Path,
):
    session_id = "independent-save-before-stop"
    save_state(
        SessionState(
            session_key=session_key(session_id),
            turn=2,
            last_save_turn=0,
            save_reminded_turn=0,
        )
    )
    ready = tmp_path / "save-held"
    release = tmp_path / "release-save"
    stop_started = tmp_path / "stop-started"
    stop_result = tmp_path / "stop-result.json"

    saver = textwrap.dedent(
        """
        import sys
        import time
        from dataclasses import replace
        from pathlib import Path
        sys.path.insert(0, sys.argv.pop(1))
        from symbiosis_brain.adapters.codex.session_state import (
            load_state, save_state, session_lock
        )

        session_id, ready, release = sys.argv[1:4]
        with session_lock(session_id, timeout_s=5):
            state = load_state(session_id)
            save_state(replace(state, last_save_turn=state.turn))
            Path(ready).write_text("held", encoding="utf-8")
            deadline = time.monotonic() + 5
            while not Path(release).exists():
                if time.monotonic() >= deadline:
                    raise TimeoutError("release barrier")
                time.sleep(0.01)
        """
    )
    stopper = textwrap.dedent(
        """
        import json
        import sys
        from datetime import datetime, timezone
        from pathlib import Path
        from types import SimpleNamespace
        sys.path.insert(0, sys.argv.pop(1))
        from symbiosis_brain.adapters.codex.adapter import AdapterContext, handle_event
        from symbiosis_brain.adapters.codex.protocol import HookEvent, HookRequest

        session_id, started, result_path, vault = sys.argv[1:5]
        request = HookRequest(
            event=HookEvent.STOP,
            session_id=session_id,
            turn_id="turn-stop",
            cwd=Path.cwd(),
            model=None,
            permission_mode=None,
            payload={},
        )
        context = AdapterContext(
            vault=Path(vault),
            config=SimpleNamespace(save_reminder_turns=2),
            now=datetime.now(timezone.utc),
        )
        Path(started).write_text("started", encoding="utf-8")
        result = handle_event(request, context)
        Path(result_path).write_text(json.dumps(result), encoding="utf-8")
        """
    )

    save_process = _python(
        saver,
        str(SOURCE_ROOT),
        session_id,
        str(ready),
        str(release),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    deadline = time.monotonic() + 3
    while not ready.exists():
        assert save_process.poll() is None
        assert time.monotonic() < deadline
        time.sleep(0.01)

    stop_process = _python(
        stopper,
        str(SOURCE_ROOT),
        session_id,
        str(stop_started),
        str(stop_result),
        str(tmp_path / "vault"),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    while not stop_started.exists():
        assert stop_process.poll() is None
        assert time.monotonic() < deadline
        time.sleep(0.01)

    time.sleep(0.1)
    assert not stop_result.exists()
    release.write_text("release", encoding="utf-8")
    save_stdout, save_stderr = save_process.communicate(timeout=3)
    stop_stdout, stop_stderr = stop_process.communicate(timeout=3)

    assert save_process.returncode == 0, save_stdout + save_stderr
    assert stop_process.returncode == 0, stop_stdout + stop_stderr
    assert json.loads(stop_result.read_text(encoding="utf-8")) == {
        "continue": True
    }
    state = load_state(session_id)
    assert state.last_save_turn == 2
    assert state.save_reminded_turn == 2
