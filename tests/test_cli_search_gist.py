"""CLI subcommand `python -m symbiosis_brain search-gist` for hook usage."""
import io
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest


# --- BUG #1: search-gist UnicodeEncodeError fail-open ------------------------
# Root cause: search-gist reconfigures only *stdout* to utf-8 (errors stays
# 'strict'), never stdin. On Windows the child stdin is cp1251/surrogateescape;
# the hook pipes UTF-8 prompt bytes, so a byte like 0x98 decodes to a lone
# surrogate \udc98. The envelope emit `print(json.dumps(..., ensure_ascii=
# False))` then can't encode the lone surrogate to strict-utf-8 stdout →
# UnicodeEncodeError → exit!=0 → the bash hook fails open to GIST_JSON='[]' →
# drops BOTH memory and route hits.
#
# This is an OS-independent unit test of the EMIT path: it monkeypatches
# sys.stdout to a STRICT utf-8 text wrapper (mirroring the production
# stdout.reconfigure(encoding="utf-8") whose errors default to 'strict'), feeds
# a payload whose one hit's gist contains a lone surrogate plus a SECOND clean
# hit, and asserts the emit does not raise AND the clean hit survives.


def test_emit_json_with_lone_surrogate_does_not_crash_and_keeps_clean_hit(monkeypatch):
    from symbiosis_brain.__main__ import _emit_json

    # Strict utf-8 stdout, exactly like the production reconfigure (errors='strict').
    buf = io.BytesIO()
    fake_stdout = io.TextIOWrapper(buf, encoding="utf-8", errors="strict",
                                   write_through=True)
    monkeypatch.setattr(sys, "stdout", fake_stdout)

    payload = {
        "memory_hits": [
            {"path": "mistakes/bad.md", "title": "Bad", "scope": "global",
             "gist": "bad" + chr(0xDC98) + "gist"},  # lone surrogate \udc98
            {"path": "patterns/clean.md", "title": "Clean", "scope": "global",
             "gist": "clean survivable gist"},
        ],
        "route_hints": [],
    }

    # (i) MUST NOT raise UnicodeEncodeError.
    _emit_json(payload)
    fake_stdout.flush()

    captured = buf.getvalue().decode("utf-8")
    out = json.loads(captured)
    # (ii) parses back to a dict with memory_hits.
    assert isinstance(out, dict)
    assert "memory_hits" in out
    # (iii) the CLEAN hit still survives (not merely no-crash).
    assert any(h.get("gist") == "clean survivable gist" for h in out["memory_hits"])


@pytest.fixture
def sb_home(tmp_path_factory) -> Path:
    """A throwaway Path.home() for subprocess tests.

    `search-gist` resolves its config through Path.home()/".claude"/
    symbiosis-brain-pre-action.json (Path.home() reads USERPROFILE on Windows, HOME on
    POSIX). A child that inherits the real ones reads the developer's own routing knobs
    — the same subprocess passes or fails depending on the machine — and writes its
    seen-store next to the live one. TMPDIR/TEMP are already covered session-wide by
    conftest._isolate_hook_artifacts; this closes the home half.
    """
    home = tmp_path_factory.mktemp("sb-home")
    (home / ".claude").mkdir(exist_ok=True)
    return home


def _hermetic_env(home: Path, **extra) -> dict:
    """os.environ with Path.home() pointed at `home`, plus optional overrides."""
    env = {**os.environ, "HOME": str(home), "USERPROFILE": str(home)}
    env.update(extra)
    return env


def test_cli_search_gist_returns_json(tmp_vault_with_taxonomy: Path, sb_home: Path):
    """Smoke test: invoke `python -m symbiosis_brain search-gist` and parse JSON output."""
    # Pre-populate vault with one note
    note_path = tmp_vault_with_taxonomy / "patterns" / "x.md"
    note_path.parent.mkdir(parents=True, exist_ok=True)
    note_path.write_text(
        "---\ntitle: X\ntype: pattern\nscope: global\ngist: A useful gist\ntags: []\n---\n\n## Body\n\nBody.\n",
        encoding="utf-8",
    )

    result = subprocess.run(
        [sys.executable, "-m", "symbiosis_brain", "search-gist",
         "--vault", str(tmp_vault_with_taxonomy),
         "--query", "X",
         "--limit", "5"],
        capture_output=True, text=True, timeout=30, env=_hermetic_env(sb_home),
    )
    assert result.returncode == 0, f"stderr: {result.stderr}"
    data = json.loads(result.stdout)
    assert isinstance(data, list)
    assert len(data) >= 1
    assert data[0]["gist"] == "A useful gist"
    assert data[0]["path"] == "patterns/x.md"
    assert data[0]["title"] == "X"
    assert data[0]["scope"] == "global"


def test_cli_search_gist_empty_vault_returns_empty_list(tmp_vault_with_taxonomy: Path,
                                                       sb_home: Path):
    result = subprocess.run(
        [sys.executable, "-m", "symbiosis_brain", "search-gist",
         "--vault", str(tmp_vault_with_taxonomy),
         "--query", "nothing", "--limit", "5"],
        capture_output=True, text=True, timeout=30, env=_hermetic_env(sb_home),
    )
    assert result.returncode == 0
    data = json.loads(result.stdout)
    assert data == []


def test_cli_search_gist_handles_cyrillic_and_arrow(tmp_vault_with_taxonomy: Path,
                                                   sb_home: Path):
    """Regression: gist with cyrillic + `→` arrow must not crash with cp1251 UnicodeEncodeError on Windows.

    Why: on Windows default stdout codec is cp1251 unless reconfigured.
    `print(json.dumps(... ensure_ascii=False))` then crashes on `→` (U+2192).
    Hook callers run `python -m symbiosis_brain search-gist` and silently
    discard stderr, so this manifests as empty A1 recall in production.
    """
    note_path = tmp_vault_with_taxonomy / "mistakes" / "cp1251.md"
    note_path.parent.mkdir(parents=True, exist_ok=True)
    note_path.write_text(
        "---\ntitle: Кодировка\ntype: mistake\nscope: global\ngist: cp1251 → utf-8 — кириллица и стрелка ломают stdout\ntags: []\n---\n\n## Body\n\n.\n",
        encoding="utf-8",
    )

    # Force child to default Windows stdout (no PYTHONIOENCODING / PYTHONUTF8).
    env = {k: v for k, v in _hermetic_env(sb_home).items()
           if k not in ("PYTHONIOENCODING", "PYTHONUTF8")}
    result = subprocess.run(
        [sys.executable, "-m", "symbiosis_brain", "search-gist",
         "--vault", str(tmp_vault_with_taxonomy),
         "--query", "кодировка", "--limit", "5"],
        capture_output=True, timeout=30, env=env,
    )
    assert result.returncode == 0, (
        f"stderr: {result.stderr.decode('utf-8', errors='replace')}"
    )
    data = json.loads(result.stdout.decode("utf-8"))
    assert any("→" in n.get("gist", "") for n in data)


# --- Stage-4 backward-compat + routing envelope -----------------------------
# CRITICAL SAFETY PROPERTY: the deployed ~/.claude bash hook still parses the
# OLD bare list (it is not redeployed until Phase B). So `search-gist` MUST
# keep returning a bare list `[{path,title,scope,gist}]` BY DEFAULT (no new
# flag), byte-shape-identical to the legacy contract. The envelope
# `{memory_hits, route_hints}` is returned ONLY under --prompt-from-stdin (or
# an explicit --envelope). The three legacy tests above (which assert
# `isinstance(data, list)`) are part of this guard; the test below makes the
# property explicit and self-documenting.


def test_search_gist_no_flag_returns_bare_list(tmp_vault_with_taxonomy: Path,
                                              sb_home: Path):
    """Backward-compat: the OLD calling convention (--query, NO --prompt-from-stdin
    / --envelope) MUST return the bare list exactly as the deployed hook expects."""
    note_path = tmp_vault_with_taxonomy / "patterns" / "bc.md"
    note_path.parent.mkdir(parents=True, exist_ok=True)
    note_path.write_text(
        "---\ntitle: BC\ntype: pattern\nscope: global\ngist: legacy gist\ntags: []\n---\n\n## Body\n\nBody.\n",
        encoding="utf-8",
    )
    result = subprocess.run(
        [sys.executable, "-m", "symbiosis_brain", "search-gist",
         "--vault", str(tmp_vault_with_taxonomy),
         "--query", "BC", "--limit", "5"],
        capture_output=True, text=True, timeout=30, env=_hermetic_env(sb_home),
    )
    assert result.returncode == 0, f"stderr: {result.stderr}"
    data = json.loads(result.stdout)
    # Bare list, NOT an envelope dict.
    assert isinstance(data, list)
    assert "memory_hits" not in (data if isinstance(data, dict) else {})
    assert len(data) >= 1
    first = data[0]
    assert set(first.keys()) == {"path", "title", "scope", "gist"}
    assert first["gist"] == "legacy gist"
    assert first["path"] == "patterns/bc.md"


def test_search_gist_missing_vault_no_flag_returns_bare_empty_list(tmp_path: Path,
                                                                  sb_home: Path):
    """Legacy missing-vault behavior: bare `[]`, not an envelope."""
    result = subprocess.run(
        [sys.executable, "-m", "symbiosis_brain", "search-gist",
         "--vault", str(tmp_path / "does-not-exist"),
         "--query", "x"],
        capture_output=True, text=True, timeout=30, env=_hermetic_env(sb_home),
    )
    assert result.returncode == 0, f"stderr: {result.stderr}"
    assert json.loads(result.stdout) == []


def test_search_gist_envelope_under_prompt_from_stdin(tmp_vault_with_taxonomy: Path,
                                                     sb_home: Path):
    """Envelope path: --prompt-from-stdin returns {memory_hits, route_hints}."""
    note_path = tmp_vault_with_taxonomy / "patterns" / "env.md"
    note_path.parent.mkdir(parents=True, exist_ok=True)
    note_path.write_text(
        "---\ntitle: Env\ntype: pattern\nscope: global\ngist: envelope gist\ntags: []\n---\n\n## Body\n\nBody.\n",
        encoding="utf-8",
    )
    payload = json.dumps({"prompt": "Env"})
    result = subprocess.run(
        [sys.executable, "-m", "symbiosis_brain", "search-gist",
         "--vault", str(tmp_vault_with_taxonomy),
         "--prompt-from-stdin", "--limit", "5"],
        input=payload, capture_output=True, text=True, timeout=60,
        env=_hermetic_env(sb_home),
    )
    assert result.returncode == 0, f"stderr: {result.stderr}"
    out = json.loads(result.stdout)
    assert isinstance(out, dict)
    assert "memory_hits" in out and "route_hints" in out
    assert isinstance(out["memory_hits"], list)
    assert isinstance(out["route_hints"], list)
    assert any(h.get("gist") == "envelope gist" for h in out["memory_hits"])


def test_search_gist_envelope_under_explicit_envelope_flag(tmp_vault_with_taxonomy: Path,
                                                          sb_home: Path):
    """Explicit --envelope (still using --query) also opts into the dict shape."""
    result = subprocess.run(
        [sys.executable, "-m", "symbiosis_brain", "search-gist",
         "--vault", str(tmp_vault_with_taxonomy),
         "--query", "anything", "--envelope"],
        capture_output=True, text=True, timeout=60, env=_hermetic_env(sb_home),
    )
    assert result.returncode == 0, f"stderr: {result.stderr}"
    out = json.loads(result.stdout)
    assert isinstance(out, dict)
    assert "memory_hits" in out and "route_hints" in out


def test_search_gist_stdin_prompt_not_truncated_with_embedded_quote(tmp_path: Path,
                                                                   sb_home: Path):
    """The prompt is read untruncated from raw stdin JSON (NOT from a truncated
    --query), and an embedded double-quote survives json.loads. We assert the
    Windows route fires off a long prompt whose UNC-path trigger sits PAST the
    point a truncated --query would have cut, proving the full prompt was used."""
    long_prefix = "обсудим " + ("очень длинный контекст " * 30)
    # Embedded double-quote + a UNC path that triggers powershell-on-windows.
    prompt = f'{long_prefix} он сказал "запиши путь" в \\\\server\\share вот так'
    payload = json.dumps({"prompt": prompt})
    result = subprocess.run(
        [sys.executable, "-m", "symbiosis_brain", "search-gist",
         "--vault", str(tmp_path), "--prompt-from-stdin", "--skip-memory"],
        input=payload, capture_output=True, text=True, timeout=30,
        env=_hermetic_env(sb_home, OSTYPE="win32"),
    )
    assert result.returncode == 0, f"stderr: {result.stderr}"
    out = json.loads(result.stdout)
    assert isinstance(out, dict)
    assert out["memory_hits"] == []  # --skip-memory honored
    assert any(h["id"] == "powershell-on-windows" for h in out["route_hints"]), out


# --- FIX 3: live routing config knobs in the envelope CLI fold --------------
# A prompt that matches EXACTLY two no-extra-gate routes on Windows:
#   - version-date-from-registry  (no `when` gate)            priority 80
#   - powershell-on-windows       (when: platform:windows)    priority 60
# so cap=2 yields 2 hints, cap=1 yields 1 (top priority), routing_enabled=false
# yields 0. OSTYPE=win32 is set so the platform:windows gate ("win" in OSTYPE)
# also passes off-Windows — do not "restore" msys here, "win" is not a substring
# of it and the gate would silently stop matching outside Windows.
_TWO_ROUTE_PROMPT = "latest version of ruff, run uv --version"


def _run_envelope_with_home_config(tmp_path, cfg_overrides):
    """Run `search-gist --prompt-from-stdin` in a subprocess whose Path.home()
    is redirected at a temp dir holding ~/.claude/symbiosis-brain-pre-action.json
    with the given overrides. Returns the parsed envelope dict.

    load_config() reads Path.home()/.claude/symbiosis-brain-pre-action.json;
    Path.home() honors USERPROFILE (Windows) / HOME (POSIX), so overriding both
    points the loader at our fixture without touching the real user config."""
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True, exist_ok=True)
    (home / ".claude" / "symbiosis-brain-pre-action.json").write_text(
        json.dumps(cfg_overrides), encoding="utf-8"
    )
    env = {**os.environ, "USERPROFILE": str(home), "HOME": str(home),
           "OSTYPE": "win32", "TMPDIR": str(tmp_path)}
    payload = json.dumps({"prompt": _TWO_ROUTE_PROMPT})
    result = subprocess.run(
        [sys.executable, "-m", "symbiosis_brain", "search-gist",
         "--vault", str(tmp_path), "--prompt-from-stdin", "--skip-memory"],
        input=payload, capture_output=True, text=True, timeout=30, env=env,
    )
    assert result.returncode == 0, f"stderr: {result.stderr}"
    out = json.loads(result.stdout)
    assert isinstance(out, dict)
    return out


def test_routing_config_baseline_two_hints(tmp_path):
    """Sanity baseline: with default cap=2 the prompt yields the two routes,
    proving the knob tests below actually change behavior (not just empty)."""
    out = _run_envelope_with_home_config(tmp_path, {})
    ids = {h["id"] for h in out["route_hints"]}
    assert ids == {"version-date-from-registry", "powershell-on-windows"}, out


def test_routing_disabled_emits_no_hints(tmp_path):
    """FIX 3: routing_enabled=false → empty route_hints (engine skipped)."""
    out = _run_envelope_with_home_config(tmp_path, {"routing_enabled": False})
    assert out["route_hints"] == [], out


def test_routing_cap_one_limits_hints(tmp_path):
    """FIX 3: routing_cap=1 → at most one hint even though 2+ routes match."""
    out = _run_envelope_with_home_config(tmp_path, {"routing_cap": 1})
    assert len(out["route_hints"]) == 1, out
    # cap keeps the top-priority route (version-date-from-registry, p=80).
    assert out["route_hints"][0]["id"] == "version-date-from-registry", out


# --- Task 6: Tier-0 _append_route_events helper tests -----------------------


def test_route_fired_line_shape_and_snippet_cap(tmp_path, monkeypatch):
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    monkeypatch.setenv("SYMBIOSIS_BRAIN_ROUTE_TURN", "5")
    from symbiosis_brain.__main__ import _append_route_events

    long_prompt = "x" * 200
    hints = [{"id": "web-research-dual-engine", "expected_tool": "WebSearch", "observable": False}]
    _append_route_events("sid-A", hints, routing_mode="decompose", rules_emitted=False, prompt=long_prompt)
    evt = tmp_path / "brain-route-events-sid-A.jsonl"
    rec = json.loads(evt.read_text(encoding="utf-8").splitlines()[0])
    assert rec["event"] == "route_fired"
    assert rec["route_id"] == "web-research-dual-engine"
    assert rec["monotonic_turn"] == 5
    assert rec["routing_mode"] == "decompose"
    assert rec["observable"] is False
    assert len(rec["prompt_snippet"]) == 60
    # FIX 4: ts is timezone-aware ISO-8601 (string), SAME format as the engine
    # appender tool_routing.append_route_fired — one log stream, one ts shape.
    import datetime as _dt
    assert isinstance(rec["ts"], str)
    assert _dt.datetime.fromisoformat(rec["ts"]).tzinfo is not None
    # Empty hints list → no file created for sid-B
    _append_route_events("sid-B", [], routing_mode="decompose", rules_emitted=True, prompt="hi")
    assert not (tmp_path / "brain-route-events-sid-B.jsonl").exists()


def _is_json(s):
    try:
        json.loads(s)
        return True
    except Exception:
        return False


def test_event_log_concurrent_appends_N_writers(tmp_path, monkeypatch):
    """FIX 1+2: exercise the REAL production appender
    (`tool_routing.append_route_fired`) from N concurrent THREADS writing to a
    single `brain-route-events-<sid>.jsonl`, and assert non-flakily.

    Determinism (per spec preference): each thread calls the production appender
    under a shared lock, so all N lines are guaranteed (no torn/lost lines) —
    we assert EXACTLY N. Every surviving line must parse as JSON with
    event=="route_fired" (no garbage among survivors). The §6.4 design accepts
    rare torn lines on Windows under lock-free concurrency, but serializing the
    appender here removes that variance so the test cannot flake under parallel
    suite load.
    """
    import re as _re
    import threading

    import symbiosis_brain.tool_routing as tr

    monkeypatch.setenv("TMPDIR", str(tmp_path))
    sid = "concurrent"
    evt = tmp_path / f"brain-route-events-{sid}.jsonl"
    N = 20
    lock = threading.Lock()

    def worker(i):
        route = tr.Route(
            id=f"route-{i}", cls="augment",
            triggers=[_re.compile("x")], hint="h",
            expected_tool="WebSearch", observable=False,
        )
        # Serialize the production appender so all N lines survive (deterministic).
        with lock:
            tr.append_route_fired(
                sid, [route], monotonic_turn=i,
                routing_mode="decompose", rules_emitted=False, prompt="x" * 100,
            )

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(N)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    lines = [ln for ln in evt.read_text(encoding="utf-8").splitlines() if ln.strip()]
    # All survivors well-formed: valid JSON, event=="route_fired", no torn lines.
    parsed = []
    for ln in lines:
        assert _is_json(ln), f"torn/garbage line among survivors: {ln!r}"
        rec = json.loads(ln)
        assert rec["event"] == "route_fired"
        parsed.append(rec)
    # Lock serializes the appender → all N lines guaranteed (deterministic).
    assert len(parsed) == N
    assert {r["monotonic_turn"] for r in parsed} == set(range(N))


def test_event_log_concurrent_appends_multiprocess(tmp_path):
    """AC#8 / §6.4: N TRULY concurrent (multi-process, lock-free) appenders to a
    single event-log produce N valid JSONL lines, tolerating the rare torn line
    the design explicitly accepts on Windows. Complements the thread+lock
    well-formedness test by exercising the unsynchronized open('a')+write path."""
    import sys as _sys
    import subprocess as _sp
    import textwrap as _tw

    evt = tmp_path / "brain-route-events-mp.jsonl"
    prog = _tw.dedent('''
        import sys, json
        p, i = sys.argv[1], sys.argv[2]
        with open(p, "a", encoding="utf-8") as f:
            f.write(json.dumps({"event": "route_fired", "n": int(i)}) + chr(10))
    ''')
    script = tmp_path / "w.py"
    script.write_text(prog, encoding="utf-8")
    N = 20
    procs = [_sp.Popen([_sys.executable, str(script), str(evt), str(i)]) for i in range(N)]
    for p in procs:
        p.wait()
    lines = [ln for ln in evt.read_text(encoding="utf-8").splitlines() if ln.strip()]
    valid = [ln for ln in lines if _is_json(ln)]
    # Lock-free concurrent appends on Windows may drop/tear individual lines
    # (§6.4 explicitly accepts this); a catastrophic regression — e.g. switching
    # to a read-modify-write full-file rewrite instead of an append — would
    # instead lose almost everything. Require a healthy majority so the test
    # distinguishes normal small loss from a non-atomic regression without
    # flaking on the OS's non-deterministic append behaviour.
    assert len(valid) >= N // 2, f"only {len(valid)}/{N} valid lines — possible non-atomic regression"


# --- CP-5 / C2 + C-N1: prompt-path dedup, type filter, over-fetch -----------
#
# The UserPromptSubmit path (`search-gist --envelope`) shaped hits straight out
# of SearchEngine: no SeenStore, no excluded_note_types, no over-fetch. It now
# runs the SAME pipeline PreToolUse runs (pre_action_recall.run_recall) behind
# its OWN seen store, so the two paths cannot starve each other (different
# limits, different purpose).
#
# TMPDIR isolation on EVERY subprocess call is mandatory: the child would
# otherwise write seen-files into the developer's real temp dir and read the
# real ones back — see the note on subprocesses inheriting the system TMPDIR.
# HOME/USERPROFILE are redirected too, so the developer's own
# ~/.claude/symbiosis-brain-pre-action.json can never change the answer.

# Captured at IMPORT (collection) time, before any TMPDIR-redirecting fixture
# runs: fastembed's model cache defaults to <tempdir>/fastembed_cache, so a
# per-test TMPDIR would make every child re-download ~130 MB of ONNX. Pin it to
# whatever the real temp dir was.
_REAL_FASTEMBED_CACHE = os.environ.get("FASTEMBED_CACHE_PATH") or os.path.join(
    tempfile.gettempdir(), "fastembed_cache"
)

# One rare token every fixture note matches, so ranking is stable and the
# candidate pool is always bigger than the limits under test.
_RECALL_TOKEN = "zebracopter"


def _make_recall_vault(tmp_path: Path) -> Path:
    """Vault under tmp_path/vault so the redirected TMPDIR (tmp_path/tmp) never
    lands inside the vault being synced."""
    from symbiosis_brain.sync import VAULT_DIRS

    vault = tmp_path / "vault"
    for d in VAULT_DIRS:
        (vault / d).mkdir(parents=True, exist_ok=True)
    (vault / "reference" / "scope-taxonomy.md").write_text(
        "## Whitelist\n\n| scope | purpose |\n|---|---|\n| `global` | x |\n",
        encoding="utf-8",
    )
    return vault


def _seed_recall_notes(vault: Path, note_count: int = 8,
                       with_user_note: bool = False) -> None:
    for i in range(note_count):
        (vault / "patterns" / f"p{i}.md").write_text(
            "---\n"
            f"title: Pattern {i}\n"
            "type: pattern\n"
            "scope: global\n"
            f"gist: gist number {i} about {_RECALL_TOKEN}\n"
            "tags: []\n"
            "---\n\n## Body\n\n"
            f"This note explains {_RECALL_TOKEN} handling, variant {i}.\n",
            encoding="utf-8",
        )
    if with_user_note:
        # Deliberately the TOP hit: FTS weights the title column x10
        # (bm25(notes_fts, 10, 1, 1)), so before the type filter worked this
        # note was memory hit #1.
        (vault / "user" / "prefs.md").write_text(
            "---\n"
            f"title: {_RECALL_TOKEN} user preference\n"
            "type: user\n"
            "scope: global\n"
            f"gist: user note about {_RECALL_TOKEN}\n"
            "tags: []\n"
            "---\n\n## Body\n\n"
            f"{_RECALL_TOKEN} {_RECALL_TOKEN} {_RECALL_TOKEN} personal preference.\n",
            encoding="utf-8",
        )


def _isolated_env(tmp_path: Path, tmp_dir: Path) -> dict:
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True, exist_ok=True)
    return {
        **os.environ,
        "TMPDIR": str(tmp_dir),
        "TEMP": str(tmp_dir),
        "USERPROFILE": str(home),
        "HOME": str(home),
        "FASTEMBED_CACHE_PATH": _REAL_FASTEMBED_CACHE,
    }


def _run_envelope_recall(vault: Path, tmp_path: Path, tmp_dir: Path, *,
                         session_id: str, limit: int,
                         prompt: str = _RECALL_TOKEN) -> dict:
    result = subprocess.run(
        [sys.executable, "-m", "symbiosis_brain", "search-gist",
         "--vault", str(vault), "--prompt-from-stdin",
         "--limit", str(limit), "--session-id", session_id],
        input=json.dumps({"prompt": prompt}),
        capture_output=True, text=True, timeout=180,
        env=_isolated_env(tmp_path, tmp_dir),
    )
    assert result.returncode == 0, f"stderr: {result.stderr}"
    out = json.loads(result.stdout)
    assert isinstance(out, dict), out
    return out


def _recall_fixture(tmp_path: Path, **seed):
    vault = _make_recall_vault(tmp_path)
    _seed_recall_notes(vault, **seed)
    tmp_dir = tmp_path / "tmp"
    tmp_dir.mkdir()
    return vault, tmp_dir


def test_envelope_dedups_repeated_prompt_hits(tmp_path: Path):
    """Same prompt twice in one session → the second turn must not re-inject
    the notes the first turn already showed."""
    vault, tmp_dir = _recall_fixture(tmp_path)

    first = _run_envelope_recall(vault, tmp_path, tmp_dir,
                                 session_id="dedup-sess", limit=2)
    second = _run_envelope_recall(vault, tmp_path, tmp_dir,
                                  session_id="dedup-sess", limit=2)

    p1 = [h["path"] for h in first["memory_hits"]]
    p2 = [h["path"] for h in second["memory_hits"]]
    assert len(p1) == 2, first
    assert not set(p1) & set(p2), (p1, p2)


def test_envelope_backfills_to_limit_after_dedup(tmp_path: Path):
    """Dedup must BACKFILL, not merely cut: run_recall over-fetches
    (hit_limit*2) and refills the freed slots with fresh hits. A naive
    'filter the already-cut page' implementation would return 0 on the
    second call — this test is what tells the two apart."""
    vault, tmp_dir = _recall_fixture(tmp_path)

    first = _run_envelope_recall(vault, tmp_path, tmp_dir,
                                 session_id="backfill-sess", limit=3)
    second = _run_envelope_recall(vault, tmp_path, tmp_dir,
                                  session_id="backfill-sess", limit=3)

    p1 = [h["path"] for h in first["memory_hits"]]
    p2 = [h["path"] for h in second["memory_hits"]]
    assert len(p1) == 3, first
    assert len(p2) == 3, second          # full page again, not a remainder
    assert len(set(p1) | set(p2)) == 6, (p1, p2)


def test_envelope_excludes_user_note_type(tmp_path: Path):
    """`excluded_note_types` (default ["user"]) applies on the prompt path too."""
    vault, tmp_dir = _recall_fixture(tmp_path, with_user_note=True)

    out = _run_envelope_recall(vault, tmp_path, tmp_dir,
                               session_id="types-sess", limit=1)
    paths = [h["path"] for h in out["memory_hits"]]
    assert paths, out                     # recall still returns something
    assert "user/prefs.md" not in paths, out


def test_prompt_store_isolated_from_pre_action_store(tmp_path: Path):
    """The prompt path gets its OWN seen-file prefix. Sharing
    `brain-recall-seen-` with PreToolUse recall (hit_limit 3, TTL 120 s) would
    make the two paths swallow each other's hits."""
    vault, tmp_dir = _recall_fixture(tmp_path)

    _run_envelope_recall(vault, tmp_path, tmp_dir, session_id="iso-sess", limit=2)

    names = sorted(p.name for p in tmp_dir.glob("*.json"))
    assert any(n.startswith("brain-prompt-recall-seen-") for n in names), names
    assert not any(n.startswith("brain-recall-seen-") for n in names), names


def test_envelope_fail_open_on_corrupt_seen_file(tmp_path: Path):
    """Hottest hook path: a corrupt seen-file degrades to 'no dedup', never to
    'no recall' and never to a non-zero exit."""
    from symbiosis_brain.recall_dedup import _seen_path

    vault, tmp_dir = _recall_fixture(tmp_path)
    corrupt = _seen_path("corrupt-sess", tmp_dir, "brain-prompt-recall-seen-")
    corrupt.write_text("{not json", encoding="utf-8")

    out = _run_envelope_recall(vault, tmp_path, tmp_dir,
                               session_id="corrupt-sess", limit=2)
    assert len(out["memory_hits"]) == 2, out


def test_legacy_bare_list_shape_unchanged(tmp_path: Path):
    """The DEPLOYED hook copy parses the bare list. The legacy path (no
    --envelope / --prompt-from-stdin) must stay byte-shape-identical: same four
    keys, no dedup, no type filter, no seen-file written."""
    vault, tmp_dir = _recall_fixture(tmp_path, with_user_note=True)
    env = _isolated_env(tmp_path, tmp_dir)

    def _legacy():
        r = subprocess.run(
            [sys.executable, "-m", "symbiosis_brain", "search-gist",
             "--vault", str(vault), "--query", _RECALL_TOKEN,
             "--limit", "2", "--session-id", "legacy-sess"],
            capture_output=True, text=True, timeout=180, env=env,
        )
        assert r.returncode == 0, f"stderr: {r.stderr}"
        data = json.loads(r.stdout)
        assert isinstance(data, list), data
        return data

    first = _legacy()
    second = _legacy()

    assert first, first
    assert set(first[0]) == {"path", "title", "scope", "gist"}
    # no dedup on the legacy path — identical answer twice
    assert [h["path"] for h in first] == [h["path"] for h in second]
    # no type filter either — the user note is still there
    assert "user/prefs.md" in [h["path"] for h in first], first
    # and nothing was written into the prompt seen store
    assert not list(tmp_dir.glob("brain-prompt-recall-seen-*.json"))
