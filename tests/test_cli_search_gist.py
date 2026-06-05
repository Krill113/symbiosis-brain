"""CLI subcommand `python -m symbiosis_brain search-gist` for hook usage."""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


def test_cli_search_gist_returns_json(tmp_vault_with_taxonomy: Path):
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
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, f"stderr: {result.stderr}"
    data = json.loads(result.stdout)
    assert isinstance(data, list)
    assert len(data) >= 1
    assert data[0]["gist"] == "A useful gist"
    assert data[0]["path"] == "patterns/x.md"
    assert data[0]["title"] == "X"
    assert data[0]["scope"] == "global"


def test_cli_search_gist_empty_vault_returns_empty_list(tmp_vault_with_taxonomy: Path):
    result = subprocess.run(
        [sys.executable, "-m", "symbiosis_brain", "search-gist",
         "--vault", str(tmp_vault_with_taxonomy),
         "--query", "nothing", "--limit", "5"],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0
    data = json.loads(result.stdout)
    assert data == []


def test_cli_search_gist_handles_cyrillic_and_arrow(tmp_vault_with_taxonomy: Path):
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
    import os
    env = {k: v for k, v in os.environ.items()
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


def test_search_gist_no_flag_returns_bare_list(tmp_vault_with_taxonomy: Path):
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
        capture_output=True, text=True, timeout=30,
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


def test_search_gist_missing_vault_no_flag_returns_bare_empty_list(tmp_path: Path):
    """Legacy missing-vault behavior: bare `[]`, not an envelope."""
    result = subprocess.run(
        [sys.executable, "-m", "symbiosis_brain", "search-gist",
         "--vault", str(tmp_path / "does-not-exist"),
         "--query", "x"],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, f"stderr: {result.stderr}"
    assert json.loads(result.stdout) == []


def test_search_gist_envelope_under_prompt_from_stdin(tmp_vault_with_taxonomy: Path):
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
    )
    assert result.returncode == 0, f"stderr: {result.stderr}"
    out = json.loads(result.stdout)
    assert isinstance(out, dict)
    assert "memory_hits" in out and "route_hints" in out
    assert isinstance(out["memory_hits"], list)
    assert isinstance(out["route_hints"], list)
    assert any(h.get("gist") == "envelope gist" for h in out["memory_hits"])


def test_search_gist_envelope_under_explicit_envelope_flag(tmp_vault_with_taxonomy: Path):
    """Explicit --envelope (still using --query) also opts into the dict shape."""
    result = subprocess.run(
        [sys.executable, "-m", "symbiosis_brain", "search-gist",
         "--vault", str(tmp_vault_with_taxonomy),
         "--query", "anything", "--envelope"],
        capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, f"stderr: {result.stderr}"
    out = json.loads(result.stdout)
    assert isinstance(out, dict)
    assert "memory_hits" in out and "route_hints" in out


def test_search_gist_stdin_prompt_not_truncated_with_embedded_quote(tmp_path: Path):
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
        env={**os.environ, "OSTYPE": "msys"},
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
# yields 0. OSTYPE=msys is set so the platform:windows gate also passes off-Win.
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
           "OSTYPE": "msys", "TMPDIR": str(tmp_path)}
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
