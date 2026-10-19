"""Line-oriented stdio worker. Importing this module stays model-free."""

from __future__ import annotations

import contextlib
import sqlite3
import sys
import time
from collections import deque
from collections.abc import Callable
from typing import Any

from .protocol import ContextRequest, ContextResult, ProtocolError, WorkerHello, encode_frame, parse_request_frame


def _write_frame(stream: Any, value: Any) -> None:
    frame = encode_frame(value)
    try:
        stream.write(frame)
    except TypeError:
        stream.write(frame.decode("utf-8"))
    stream.flush()


def _degraded(request: ContextRequest, reason: str) -> ContextResult:
    return ContextResult(
        request_id=request.request_id, session_id=request.session_id,
        generation=request.generation, capability_revision=request.capability_revision,
        status="degraded", scope=request.scope, diagnostics=[reason],
    )


def serve(
    input_stream: Any,
    output_stream: Any,
    service_factory: Callable[[str], Callable[[ContextRequest], ContextResult]],
    *,
    role: str,
) -> int:
    """Serve JSONL requests until EOF; each process executes serially."""
    if role not in {"control", "retrieval"}:
        raise ValueError("role must be control or retrieval")
    handler = service_factory(role)
    if not callable(handler):
        raise TypeError("service_factory must return a request handler")
    _write_frame(output_stream, WorkerHello(role))
    seen_ids: set[str] = set()
    seen_order: deque[str] = deque()
    while True:
        raw = input_stream.readline()
        if raw in (b"", ""):
            return 0
        try:
            request = parse_request_frame(raw)
        except ProtocolError as exc:
            # A malformed frame cannot safely be correlated to a request.
            print(f"worker-protocol:{exc.reason}", file=sys.stderr, flush=True)
            continue
        if request.request_id in seen_ids:
            result = _degraded(request, "duplicate-request-id")
        else:
            if len(seen_order) >= 4096:
                seen_ids.discard(seen_order.popleft())
            seen_order.append(request.request_id)
            seen_ids.add(request.request_id)
            try:
                # Import progress/noisy dependencies must never corrupt stdout JSONL.
                with contextlib.redirect_stdout(sys.stderr):
                    result = handler(request)
                if not isinstance(result, ContextResult):
                    result = _degraded(request, "invalid-service-result")
                elif (
                    result.request_id != request.request_id
                    or result.session_id != request.session_id
                    or result.generation != request.generation
                    or result.capability_revision != request.capability_revision
                ):
                    result = _degraded(request, "service-result-context-mismatch")
            except Exception:
                result = _degraded(request, "worker-operation-failed")
        _write_frame(output_stream, result)


def _service_factory(vault_path: str | None) -> Callable[[str], Callable[[ContextRequest], ContextResult]]:
    def factory(role: str) -> Callable[[ContextRequest], ContextResult]:
        from pathlib import Path

        from .config import AdapterConfig

        config = AdapterConfig.from_env()
        vault = Path(vault_path).expanduser() if vault_path else config.vault

        scope_unset = object()

        def result(request: ContextRequest, *, blocks: list | None = None, diagnostics: list[str] | None = None,
                   resources=None, action_descriptors: list | None = None, scope=scope_unset) -> ContextResult:
            return ContextResult(
                request_id=request.request_id, session_id=request.session_id,
                generation=request.generation, capability_revision=request.capability_revision,
                status="degraded" if diagnostics else "ok", scope=request.scope if scope is scope_unset else scope,
                blocks=blocks or [], diagnostics=diagnostics or [], resources=resources,
                action_descriptors=action_descriptors,
            )

        def handle(request: ContextRequest) -> ContextResult:
            if vault is None:
                return result(request, diagnostics=["vault-unavailable"])
            if request.operation == "resources":
                import ast
                import importlib.util
                import shutil
                import sys as worker_sys
                from importlib.resources import files

                from .bootstrap import resolve_project

                project_context = resolve_project(
                    Path(request.cwd), vault, Path(request.boundary) if request.boundary else None,
                )
                package = Path(str(files("symbiosis_brain")))
                packaged_skills = package / "skills"
                # Development checkouts keep canonical skill sources at repo/skills;
                # installed builds should package them under symbiosis_brain/skills.
                source_skills = package.parent.parent / "skills"
                skills = packaged_skills if packaged_skills.is_dir() else source_skills
                entry_spec = importlib.util.find_spec("symbiosis_brain.install_cli")
                if entry_spec is None or not entry_spec.origin:
                    return result(request, diagnostics=["public-install-entrypoint-unavailable"], scope=project_context.scope)
                try:
                    entry_tree = ast.parse(Path(entry_spec.origin).read_text(encoding="utf-8"))
                except (OSError, SyntaxError):
                    return result(request, diagnostics=["public-install-entrypoint-invalid"], scope=project_context.scope)
                if not any(isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "main" for node in entry_tree.body):
                    return result(request, diagnostics=["public-install-entrypoint-invalid"], scope=project_context.scope)
                if not skills.is_dir() or any(not (skills / name / "SKILL.md").is_file() for name in ("brain-init", "brain-save")):
                    return result(request, diagnostics=["canonical-skills-unavailable"], scope=project_context.scope)
                action_file = vault / ".index" / "action-rules.tsv"
                from .protocol import BridgeResources

                resources = BridgeResources(
                    python_exe=worker_sys.executable, package_dir=str(package),
                    skills_dir=str(skills), grep_exe=shutil.which("grep"),
                    action_rules_file=str(action_file),
                    mcp_command=(worker_sys.executable, "-c", "from symbiosis_brain.install_cli import main; main()", "serve", "--vault", str(vault)),
                )
                return result(request, resources=resources, scope=project_context.scope)
            if request.operation == "bootstrap":
                from .bootstrap import build_bootstrap, resolve_project

                context = resolve_project(Path(request.cwd), vault, Path(request.boundary) if request.boundary else None)
                blocks = build_bootstrap(vault, context, char_limit=AdapterConfig.from_env().char_limit, now=__import__("datetime").datetime.now(__import__("datetime").timezone.utc))
                diagnostics = list(context.diagnostics)
                if context.global_only and "project-boundary-unknown" not in diagnostics:
                    diagnostics.append("project-scope-unavailable")
                return result(request, blocks=blocks, diagnostics=diagnostics, scope=context.scope)
            if request.operation == "routes":
                if role != "control":
                    return result(request, diagnostics=["operation-role-mismatch"])
                from .routing import load_action_descriptors, match_opencode_routes

                diagnostics: list[str] = []
                capabilities = {
                    "tools": list(request.tools), "mcp": list(request.mcp),
                    "skills": list(request.skills), "revision": request.capability_revision,
                    "cwd": request.cwd,
                }
                blocks = match_opencode_routes(request.query or "", capabilities, request.scope or "", Path(request.cwd), vault, diagnostics=diagnostics)
                descriptors = load_action_descriptors(vault, capabilities, request.scope or "", diagnostics=diagnostics)
                return result(request, blocks=blocks, diagnostics=diagnostics, action_descriptors=descriptors)
            if request.operation in {"recall", "warmup"}:
                if role != "retrieval":
                    return result(request, diagnostics=["operation-role-mismatch"])
                from .compat_index import ExistingIndexReader, IndexNotReady, ReadOnlyIndexView, validate_index

                db_path = vault / ".index" / "brain.db"
                if request.operation == "recall" and (not request.query or not request.scope):
                    return result(request, diagnostics=["explicit-recall-scope-required"])
                for attempt, delay in enumerate((0.0, 0.05, 0.15)):
                    if delay:
                        time.sleep(delay)
                    view = None
                    try:
                        view = ReadOnlyIndexView(db_path)
                        validate_index(view)
                        from symbiosis_brain.search import SearchEngine

                        engine = SearchEngine(view)
                        reader = ExistingIndexReader(engine)
                        if request.operation == "warmup":
                            # Exercise the full hybrid path; hello alone is only transport readiness.
                            reader.search("symbiosis retrieval warmup", scope="global", limit=1, mode="gist")
                            return result(request)
                        from .recall import recall_hits

                        hits = recall_hits(request.query, request.scope, reader, limit=5)
                        from .protocol import ContextBlock

                        blocks = [ContextBlock(
                            id=f"memory:{hit.get('path', 'unknown')}",
                            text=str(hit.get("gist") or ""), priority=50, category="memory",
                        ) for hit in hits if hit.get("gist")]
                        return result(request, blocks=blocks)
                    except (IndexNotReady, OSError, sqlite3.Error, ImportError):
                        if attempt == 2:
                            return result(request, diagnostics=["index-not-ready-retry-exhausted"])
                    finally:
                        if view is not None:
                            view.close()
                return result(request, diagnostics=["index-not-ready-retry-exhausted"])
            return result(request, diagnostics=["unsupported-operation"])

        return handle
    return factory


def main(argv: list[str] | None = None) -> int:
    import argparse
    import os

    parser = argparse.ArgumentParser()
    parser.add_argument("--worker", action="store_true", required=True)
    parser.add_argument("--role", choices=("control", "retrieval"), required=True)
    parser.add_argument("--vault", required=True)
    args = parser.parse_args(argv)
    os.environ["SYMBIOSIS_BRAIN_VAULT"] = args.vault
    return serve(sys.stdin.buffer, sys.stdout.buffer, _service_factory(args.vault), role=args.role)
