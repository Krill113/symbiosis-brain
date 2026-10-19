from __future__ import annotations

import json
import os
import re
import subprocess
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

import symbiosis_brain.adapters.codex.recall as recall_module
from symbiosis_brain.adapters.codex.recall import (
    ACTION_SEEN_PREFIX,
    PROMPT_SEEN_PREFIX,
    CodexRoutingEnvironment,
    RecallRuntime,
    action_recall,
    codex_discipline_hint,
    match_codex_routes,
    open_recall_runtime,
    prompt_recall,
    resolve_codex_routing_environment,
)
from symbiosis_brain.pre_action_config import PreActionConfig
from symbiosis_brain.adapters.codex.session_state import delete_state
from symbiosis_brain.tool_routing import Route


class _Engine:
    def __init__(self, rows: list[dict] | None = None, *, fail: bool = False):
        self.rows = list(rows or [])
        self.fail = fail
        self.calls: list[dict] = []

    def search(self, **kwargs):
        self.calls.append(kwargs)
        if self.fail:
            raise RuntimeError("search failed")
        rows = [dict(row) for row in self.rows]
        scope = kwargs.get("scope")
        if scope:
            rows = [row for row in rows if row["scope"] in {scope, "global"}]
        return rows[: kwargs["limit"]]


def _hit(path: str = "demo/note.md", *, scope: str = "demo") -> dict:
    return {
        "path": path,
        "title": "Note",
        "scope": scope,
        "gist": "safe gist",
        "content": "safe content",
        "note_type": "decision",
        "frontmatter": {},
    }


@pytest.fixture(autouse=True)
def _isolated_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))
    monkeypatch.setenv("TEMP", str(tmp_path / "temp"))
    monkeypatch.setenv("TMPDIR", str(tmp_path / "temp"))
    (tmp_path / "temp").mkdir()


def _prompt(
    runtime: RecallRuntime,
    *,
    session_id: str = "session-a",
    allow_global: bool = False,
    scope: str | None = "demo",
) -> tuple[list[dict], list[dict]]:
    return prompt_recall(
        "find the relevant decision",
        scope=scope,
        allow_global=allow_global,
        memory_authorized=True,
        config=PreActionConfig(hit_limit=3),
        runtime=runtime,
        session_id=session_id,
        routes=[],
    )


def test_prompt_and_action_use_distinct_namespaces():
    runtime = RecallRuntime(
        storage=SimpleNamespace(count_notes=lambda: 1),
        engine=_Engine([_hit()]),
    )

    prompt_hits, _ = _prompt(runtime)
    action = action_recall(
        "same query",
        scope="demo",
        allow_global=False,
        memory_authorized=True,
        config=PreActionConfig(),
        runtime=runtime,
        session_id="session-a",
    )

    assert PROMPT_SEEN_PREFIX == "brain-codex-prompt-seen-"
    assert ACTION_SEEN_PREFIX == "brain-codex-action-seen-"
    assert [row["path"] for row in prompt_hits] == ["demo/note.md"]
    assert "demo/note.md" in action
    state_dir = Path(os.environ["CODEX_HOME"]) / ".symbiosis-brain" / "state"
    state_names = {path.name for path in state_dir.iterdir()}
    assert any(name.startswith(PROMPT_SEEN_PREFIX) for name in state_names), state_names
    assert any(name.startswith(ACTION_SEEN_PREFIX) for name in state_names), state_names


def test_same_namespace_dedups_but_different_sessions_do_not():
    runtime = RecallRuntime(
        storage=SimpleNamespace(count_notes=lambda: 1),
        engine=_Engine([_hit()]),
    )

    first, _ = _prompt(runtime, session_id="session-a")
    second, _ = _prompt(runtime, session_id="session-a")
    other, _ = _prompt(runtime, session_id="session-b")

    assert len(first) == 1
    assert second == []
    assert len(other) == 1


def test_empty_session_runs_without_persistent_dedup():
    runtime = RecallRuntime(
        storage=SimpleNamespace(count_notes=lambda: 1),
        engine=_Engine([_hit()]),
    )

    first, _ = _prompt(runtime, session_id="")
    second, _ = _prompt(runtime, session_id="")

    assert len(first) == len(second) == 1
    state_dir = Path(os.environ["CODEX_HOME"]) / ".symbiosis-brain" / "state"
    assert not state_dir.exists()


def test_session_lock_wraps_seen_store_read_and_record(monkeypatch: pytest.MonkeyPatch):
    events: list[str] = []

    @contextmanager
    def fake_lock(session_id: str):
        events.append(f"lock:{session_id}")
        yield
        events.append("unlock")

    class Seen:
        def __init__(self, *args, **kwargs):
            assert events == ["lock:session-a"]
            events.append(f"seen:{kwargs['prefix']}")

        def is_seen(self, path: str) -> bool:
            assert events[-1].startswith("seen:")
            events.append("read")
            return False

        def record(self, paths):
            assert events[-1] == "read"
            list(paths)
            events.append("record")

    monkeypatch.setattr(recall_module, "session_lock", fake_lock)
    monkeypatch.setattr(recall_module, "SeenStore", Seen)
    runtime = RecallRuntime(
        storage=SimpleNamespace(count_notes=lambda: 1),
        engine=_Engine([_hit()]),
    )

    hits, _ = _prompt(runtime)

    assert len(hits) == 1
    assert events == [
        "lock:session-a",
        f"seen:{PROMPT_SEEN_PREFIX}",
        "read",
        "record",
        "unlock",
    ]


def test_run_recall_gets_log_ctx_none(monkeypatch: pytest.MonkeyPatch):
    captured: dict = {}

    def fake_run_recall(**kwargs):
        captured.update(kwargs)
        return [_hit()]

    monkeypatch.setattr(recall_module, "run_recall", fake_run_recall)
    runtime = RecallRuntime(storage=SimpleNamespace(count_notes=lambda: 1), engine=_Engine())

    hits, _ = _prompt(runtime, session_id="")

    assert len(hits) == 1
    assert captured["log_ctx"] is None
    assert captured["fts_mode"] == recall_module.FTS_MODE_ALL_THEN_ANY
    assert captured["engine"] is runtime.engine


def test_closed_notes_are_excluded_at_the_search_boundary():
    engine = _Engine([_hit()])
    runtime = RecallRuntime(storage=SimpleNamespace(count_notes=lambda: 1), engine=engine)

    hits, _ = _prompt(runtime, session_id="")

    assert len(hits) == 1
    assert engine.calls[0]["include_closed"] is False


def test_search_failure_returns_empty_output():
    runtime = RecallRuntime(
        storage=SimpleNamespace(count_notes=lambda: 1),
        engine=_Engine(fail=True),
    )

    assert _prompt(runtime)[0] == []
    assert action_recall(
        "query",
        scope="demo",
        allow_global=False,
        memory_authorized=True,
        config=PreActionConfig(),
        runtime=runtime,
        session_id="session-a",
    ) == ""


def test_project_recall_uses_canonical_scope_plus_global_semantics():
    rows = [
        _hit("demo/project.md", scope="demo"),
        _hit("global/shared.md", scope="global"),
        _hit("other/private.md", scope="other"),
    ]
    engine = _Engine(rows)
    runtime = RecallRuntime(
        storage=SimpleNamespace(count_notes=lambda: len(rows)),
        engine=engine,
    )

    hits, _ = _prompt(runtime, allow_global=False)

    assert [row["path"] for row in hits] == [
        "demo/project.md",
        "global/shared.md",
    ]
    assert engine.calls[0]["scope"] == "demo"
    assert engine.calls[0]["include_closed"] is False


def test_unknown_project_scope_still_reaches_global_memory():
    engine = _Engine(
        [
            _hit("global/shared.md", scope="global"),
            _hit("other/private.md", scope="other"),
        ]
    )
    runtime = RecallRuntime(
        storage=SimpleNamespace(count_notes=lambda: 2),
        engine=engine,
    )

    hits, routes = _prompt(
        runtime,
        scope="unknown-project",
        allow_global=False,
        session_id="",
    )

    assert [row["path"] for row in hits] == ["global/shared.md"]
    assert routes == []
    assert engine.calls[0]["scope"] == "unknown-project"


def test_unresolved_auto_recall_uses_global_not_cross_project_search():
    rows = [
        _hit("other/private.md", scope="other"),
        _hit("global/shared.md", scope="global"),
    ]
    engine = _Engine(rows)
    runtime = RecallRuntime(
        storage=SimpleNamespace(count_notes=lambda: 2),
        engine=engine,
    )

    hits, _ = _prompt(runtime, scope=None, allow_global=False)

    assert [row["path"] for row in hits] == ["global/shared.md"]
    assert engine.calls[0]["scope"] == "global"


def test_legacy_memory_authorized_flag_does_not_create_a_codex_policy():
    runtime = RecallRuntime(
        storage=SimpleNamespace(count_notes=lambda: 1),
        engine=_Engine([_hit()]),
    )

    hits, _ = prompt_recall(
        "canonical query",
        scope="demo",
        allow_global=False,
        memory_authorized=False,
        config=PreActionConfig(),
        runtime=runtime,
        session_id="",
        routes=[],
    )

    assert [row["path"] for row in hits] == ["demo/note.md"]
    assert len(runtime.engine.calls) == 1


def test_action_recall_redacts_sentinel_from_search_query_and_recall_header(
    monkeypatch: pytest.MonkeyPatch,
):
    sentinel = "SENTINEL-PRIVATE-42"
    captured: dict[str, object] = {}

    def fake_run_recall(**kwargs):
        captured.update(kwargs)
        return [_hit()]

    monkeypatch.setattr(recall_module, "run_recall", fake_run_recall)
    runtime = RecallRuntime(storage=SimpleNamespace(), engine=_Engine())

    block = action_recall(
        "\n".join(
            [
                f"Set-Item -LiteralPath Env:OPENAI_API_KEY -Value '{sentinel}'",
                f"Set-Item -Path 'Env:OPENAI_API_KEY' -Value '{sentinel}'",
                (
                    r"setx /S remote /U domain\\user /P "
                    f"{sentinel} OPENAI_API_KEY publicvalue"
                ),
                f"PRIVATE_KEY={sentinel}",
                f"AWS_CREDENTIALS={sentinel}",
                f"DATABASE_URL=postgres://u:{sentinel}@example.invalid/db",
                f"curl -HAuthorization:Bearer {sentinel} example.invalid",
            ]
        ),
        scope="demo",
        allow_global=False,
        memory_authorized=True,
        config=PreActionConfig(),
        runtime=runtime,
        session_id="",
    )

    assert captured["query"] == "secret-bearing command [REDACTED]"
    assert sentinel not in str(captured["query"])
    assert "publicvalue" not in str(captured["query"])
    assert sentinel not in block
    assert "publicvalue" not in block
    assert "[REDACTED]" in str(captured["query"])
    assert "[REDACTED]" in block



def test_prompt_recall_uses_canonical_search_engine_hybrid_halves(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = recall_module.Storage(tmp_path / "brain.db")
    engine = recall_module.SearchEngine(storage)
    calls: list[tuple[str, str | None]] = []

    def search_fts(query, scope=None, limit=10, *, mode="all"):
        calls.append(("fts", scope))
        return [_hit("demo/project.md", scope="demo")]

    def search_vector(query, scope=None, limit=10):
        calls.append(("vector", scope))
        return [_hit("global/shared.md", scope="global")]

    monkeypatch.setattr(engine, "search_fts", search_fts)
    monkeypatch.setattr(engine, "search_vector", search_vector)
    runtime = RecallRuntime(storage=storage, engine=engine)

    try:
        hits, _ = _prompt(
            runtime,
            session_id="",
            scope="demo",
            allow_global=False,
        )
    finally:
        storage._conn.close()

    assert calls == [("fts", "demo"), ("vector", "demo")]
    assert [hit["path"] for hit in hits] == [
        "demo/project.md",
        "global/shared.md",
    ]

def test_privacy_sentinel_never_reaches_logs_state_db_or_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    sentinel = "SENTINEL-PRIVATE-42"
    debug_log = tmp_path / "debug.log"
    monkeypatch.setenv("SYMBIOSIS_BRAIN_DEBUG_LOG", str(debug_log))
    storage = recall_module.Storage(tmp_path / "vault" / ".index" / "brain.db")
    engine = _Engine([])
    runtime = RecallRuntime(storage=storage, engine=engine)

    try:
        block = action_recall(
            "\n".join(
                [
                    f"Set-Item -LiteralPath Env:OPENAI_API_KEY -Value '{sentinel}'",
                    f"Set-Item -Path 'Env:OPENAI_API_KEY' -Value '{sentinel}'",
                    (
                        r"setx /S remote /U domain\\user /P "
                        f"{sentinel} OPENAI_API_KEY publicvalue"
                    ),
                    f"PRIVATE_KEY={sentinel}",
                    f"AWS_CREDENTIALS={sentinel}",
                    f"DATABASE_URL=postgres://u:{sentinel}@example.invalid/db",
                    f"curl -HAuthorization:Bearer {sentinel} example.invalid",
                ]
            ),
            scope="demo",
            allow_global=False,
            memory_authorized=True,
            config=PreActionConfig(),
            runtime=runtime,
            session_id="session-secret",
        )

        assert sentinel not in block
        assert storage._conn.execute(
            "SELECT COUNT(*) FROM retrieval_event"
        ).fetchone()[0] == 0
        roots = [Path(os.environ["TEMP"]), Path(os.environ["CODEX_HOME"])]
        for root in roots:
            for path in root.rglob("*"):
                if path.is_file():
                    assert sentinel not in path.read_text(
                        encoding="utf-8", errors="ignore"
                    )
        assert not debug_log.exists() or sentinel not in debug_log.read_text(
            encoding="utf-8"
        )
        assert list(Path(os.environ["TEMP"]).glob("brain-route-events-*")) == []
    finally:
        storage._conn.close()


def test_open_runtime_syncs_and_rekeys_vectors(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    events: list[object] = []

    class Storage:
        def __init__(self, path):
            events.append(("storage", path))
            self._conn = SimpleNamespace(close=lambda: events.append("close"))

    class Sync:
        def __init__(self, vault, storage):
            events.append(("sync-init", vault, storage))

        def sync_all(self):
            events.append("sync")
            return SimpleNamespace(removed=["gone.md"], moved=[("old.md", "new.md")])

    class Search:
        def __init__(self, storage):
            events.append(("search", storage))

        def delete_vec(self, path):
            events.append(("delete", path))

        def move_vec(self, old, new):
            events.append(("move", old, new))

    monkeypatch.setattr(recall_module, "Storage", Storage)
    monkeypatch.setattr(recall_module, "VaultSync", Sync)
    monkeypatch.setattr(recall_module, "SearchEngine", Search)
    vault = tmp_path / "vault"

    with open_recall_runtime(vault) as runtime:
        assert isinstance(runtime.engine, Search)

    assert ("storage", vault / ".index" / "brain.db") in events
    assert ("delete", "gone.md") in events
    assert ("move", "old.md", "new.md") in events
    assert events[-1] == "close"


def _route(route_id: str, when: str, *, cls: str = "augment") -> Route:
    return Route(
        id=route_id,
        cls=cls,
        triggers=[re.compile("work")],
        hint=f"{route_id} hint",
        priority=50,
        when=when,
    )


def test_codex_route_gates_use_effective_environment(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(recall_module.os, "name", "nt")
    env = CodexRoutingEnvironment(
        effective_mcp_names=frozenset({"serena"}),
        effective_skill_names=frozenset({"brain-save"}),
        catalog_available=True,
    )
    routes = [
        _route("windows", "platform:windows"),
        _route("scope", "scope:demo"),
        _route("skill", "skill:brain-save-present"),
        _route("mcp", "serena-present"),
    ]

    matched = match_codex_routes(
        "work", routes, scope="demo", environment=env, cap=10, session_id=""
    )

    assert {route.id for route in matched} == {"windows", "scope", "skill", "mcp"}


def test_ambiguous_or_unknown_route_gates_fail_closed():
    routes = [
        _route("mcp", "serena-present"),
        _route("skill", "skill:brain-save-present"),
        _route("catalog", "catalog-present"),
        _route("unknown", "future-gate"),
    ]
    environment = CodexRoutingEnvironment(frozenset(), frozenset(), None)

    assert match_codex_routes(
        "work", routes, scope="demo", environment=environment, cap=10
    ) == []
    assert match_codex_routes(
        "work", routes, scope="demo", environment=None, cap=10
    ) == []


def test_mcp_present_gate_preserves_existing_substring_semantics():
    environment = CodexRoutingEnvironment(
        effective_mcp_names=frozenset({"serena-tools"}),
        effective_skill_names=frozenset(),
        catalog_available=None,
    )

    matched = match_codex_routes(
        "work",
        [_route("mcp", "serena-present")],
        scope="demo",
        environment=environment,
        cap=2,
    )

    assert [route.id for route in matched] == ["mcp"]


def test_route_dedup_uses_hashed_session_and_writes_no_event_file(
    monkeypatch: pytest.MonkeyPatch,
):
    captured: list[tuple[str, Path | None]] = []

    original_seen = recall_module.SeenStore

    class CapturingSeen(original_seen):
        def __init__(self, session_id, *args, **kwargs):
            captured.append((session_id, kwargs.get("base_dir")))
            super().__init__(session_id, *args, **kwargs)

    monkeypatch.setattr(recall_module, "SeenStore", CapturingSeen)
    environment = CodexRoutingEnvironment(frozenset(), frozenset(), True)

    matched = match_codex_routes(
        "work",
        [_route("scope", "scope:demo")],
        scope="demo",
        environment=environment,
        cap=2,
        session_id="../../raw-private-id",
    )

    assert [route.id for route in matched] == ["scope"]
    assert len(captured) == 1
    assert re.fullmatch(r"[0-9a-f]{64}", captured[0][0])
    assert captured[0][1] == (
        Path(os.environ["CODEX_HOME"]) / ".symbiosis-brain" / "state"
    )
    assert list(Path(os.environ["TEMP"]).glob("brain-route-events-*")) == []


def test_session_cleanup_removes_exact_route_file_created_by_real_producer() -> None:
    session_id = "session-route-cleanup"
    environment = CodexRoutingEnvironment(frozenset(), frozenset(), True)

    matched = match_codex_routes(
        "work",
        [_route("scope", "scope:demo")],
        scope="demo",
        environment=environment,
        cap=2,
        session_id=session_id,
    )
    state_dir = Path(os.environ["CODEX_HOME"]) / ".symbiosis-brain" / "state"
    route_files = list(state_dir.glob("brain-route-seen-*.json"))

    assert [route.id for route in matched] == ["scope"]
    assert len(route_files) == 1

    delete_state(session_id)

    assert not route_files[0].exists()


def test_prompt_recall_returns_route_hints_without_route_event_files():
    runtime = RecallRuntime(storage=SimpleNamespace(count_notes=lambda: 0), engine=_Engine())
    environment = CodexRoutingEnvironment(frozenset(), frozenset(), True)

    hits, hints = prompt_recall(
        "work",
        scope="demo",
        allow_global=False,
        memory_authorized=True,
        config=PreActionConfig(),
        runtime=runtime,
        session_id="session-a",
        routes=[_route("scope", "scope:demo")],
        routing_environment=environment,
    )

    assert hits == []
    assert hints == [{"id": "scope", "class": "augment", "hint": "scope hint"}]
    assert list(Path(os.environ["TEMP"]).glob("brain-route-events-*")) == []


def test_discipline_hint_cadence_and_supersede_behavior():
    text = (
        "Before broad code search, check brain_search first. Use available "
        "subagents for large multi-file exploration; use symbol/dependency "
        "tools before blind edits."
    )

    assert codex_discipline_hint(turn=1, interval=10, routes_fired=False) == text
    assert codex_discipline_hint(turn=9, interval=10, routes_fired=False) == ""
    assert codex_discipline_hint(turn=10, interval=10, routes_fired=False) == text
    assert codex_discipline_hint(turn=20, interval=10, routes_fired=False) == text
    assert codex_discipline_hint(turn=10, interval=10, routes_fired=True) == (
        "Before broad code search, check brain_search first."
    )


def _completed(payload: object, *, returncode: int = 0) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        args=[], returncode=returncode, stdout=json.dumps(payload), stderr=""
    )


def test_routing_environment_uses_active_codex_home_and_enabled_plugin_manifest(
    tmp_path: Path,
):
    codex_home = tmp_path / "alternate-codex"
    plugin = codex_home / "plugins" / "cache" / "source" / "plugin" / "v1"
    (plugin / ".codex-plugin").mkdir(parents=True)
    (plugin / "skills" / "plugin-skill").mkdir(parents=True)
    (plugin / "skills" / "plugin-skill" / "SKILL.md").write_text("# skill", encoding="utf-8")
    (plugin / ".codex-plugin" / "plugin.json").write_text(
        json.dumps({"name": "plugin", "skills": "./skills/"}),
        encoding="utf-8",
    )
    (codex_home / "skills" / "local-skill").mkdir(parents=True)
    (codex_home / "skills" / "local-skill" / "SKILL.md").write_text("# skill", encoding="utf-8")
    calls: list[tuple[list[str], str, str]] = []

    def runner(args, **kwargs):
        calls.append((list(args), kwargs["env"]["CODEX_HOME"], kwargs["cwd"]))
        if args[1:3] == ["mcp", "list"]:
            return _completed([{"name": "serena", "enabled": True}])
        return _completed(
            [
                {
                    "id": "plugin@source",
                    "enabled": True,
                    "install_path": str(plugin),
                }
            ]
        )

    environment = resolve_codex_routing_environment(
        cwd=tmp_path, codex_home=codex_home, runner=runner
    )

    assert environment == CodexRoutingEnvironment(
        effective_mcp_names=frozenset({"serena"}),
        effective_skill_names=frozenset({"local-skill", "plugin-skill"}),
        catalog_available=None,
    )
    assert all(call[1] == str(codex_home.resolve()) for call in calls)
    assert all(Path(call[2]) == tmp_path for call in calls)


def test_disabled_plugins_do_not_contribute_skills(tmp_path: Path):
    codex_home = tmp_path / "codex"
    codex_home.mkdir()

    def runner(args, **kwargs):
        if args[1:3] == ["mcp", "list"]:
            return _completed([])
        return _completed([{"id": "disabled", "enabled": False, "install_path": "outside"}])

    environment = resolve_codex_routing_environment(
        cwd=tmp_path, codex_home=codex_home, runner=runner
    )

    assert environment is not None
    assert environment.effective_skill_names == frozenset()


@pytest.mark.parametrize(
    "bad_result",
    [
        subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="bad"),
        subprocess.CompletedProcess(args=[], returncode=0, stdout="{", stderr=""),
    ],
)
def test_malformed_codex_output_fails_closed(tmp_path: Path, bad_result):
    codex_home = tmp_path / "codex"
    codex_home.mkdir()

    assert resolve_codex_routing_environment(
        cwd=tmp_path,
        codex_home=codex_home,
        runner=lambda *args, **kwargs: bad_result,
    ) is None


def test_project_layered_codex_config_fails_closed(tmp_path: Path):
    codex_home = tmp_path / "codex"
    codex_home.mkdir()
    project = tmp_path / "project"
    (project / ".codex").mkdir(parents=True)
    (project / ".codex" / "config.toml").write_text(
        '[plugins."demo"]\nenabled = false\n',
        encoding="utf-8",
    )

    environment = resolve_codex_routing_environment(
        cwd=project,
        codex_home=codex_home,
        runner=lambda *args, **kwargs: pytest.fail("CLI must not run"),
    )

    assert environment is None
