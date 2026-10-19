"""Canonical skills keep their ritual while dispatching through each native client."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

import pytest
from symbiosis_brain import install_cli


ROOT = Path(__file__).resolve().parents[2]
SKILLS = ROOT / "skills"

CANONICAL_SKILLS = {
    "brain-autolearn",
    "brain-backfill-gists",
    "brain-init",
    "brain-project-init",
    "brain-recall",
    "brain-save",
    "brain-tools",
    "brain-welcome",
}

CLIENT_SCOPED_SKILLS = {
    "brain-autolearn",
    "brain-backfill-gists",
    "brain-init",
    "brain-project-init",
    "brain-save",
    "brain-tools",
}

EXPECTED_FRONTMATTER = {
    "brain-autolearn": """name: brain-autolearn
description: >
  Use when brain-save Step 0 runs at session end (right after the project
  retrospective), when the user asks to look for repeated actions or repeated
  mistakes (/brain-autolearn), or when you catch yourself doing the same thing for
  the third time. Turns repetition into an action rule, a script, a skill, or a
  proposal — with the owner kept in the loop.""",
    "brain-backfill-gists": """name: brain-backfill-gists
description: >
  Backfill missing `gist:` field for existing vault notes. Run once after upgrading
  to gist-layer (or after importing many notes). Spawns parallel Explore subagents
  by scope. Idempotent — re-run safely after partial completion.""",
    "brain-init": """name: brain-init
description: >
  Load Symbiosis Brain context at session start. MUST use at the beginning of
  every new conversation or when switching to a different project. Loads user
  profile, project context, umbrella ecosystem-context, and resolves scope via
  CLAUDE.md marker.""",
    "brain-project-init": """name: brain-project-init
description: >
  Onboard a new project scope into Symbiosis Brain. Creates the project card
  in vault, appends a row to scope-taxonomy.md, and writes a marker into
  <project>/CLAUDE.md. Triggered by brain-init skill — never invoke directly.
  Three modes: full (default), recovery (card lost), migration (marker version >= 2).""",
    "brain-recall": """name: brain-recall
description: >
  Search Symbiosis Brain for relevant context BEFORE acting. Trigger silently on these
  signals: (a) before first Edit/Write in an unfamiliar file or scope; (b) before grep
  across >3 files / a whole subtree; (c) when the user describes a bug or unexpected
  behavior — recall similar fix history; (d) after any subagent error or contradicted
  hypothesis — recall if this mistake is already documented; (e) before using an API
  / library that hasn't appeared in this session yet. Don't ask "should I check?" —
  just check. Report only when something relevant was found.""",
    "brain-save": """name: brain-save
description: >
  Save knowledge to Symbiosis Brain with quality gates and deduplication. Use when you've
  learned something important — decisions, patterns, mistakes, user preferences, research
  findings. Also use when the user asks to remember something. SHOULD trigger proactively
  after significant work, not only when explicitly asked.
  MUST also trigger at session end or after completing large multi-step tasks —
  does a quick retrospective self-scan for dead ends, workflow improvements,
  and missed user signals before saving.""",
    "brain-tools": """name: brain-tools
description: >
  Onboard YOUR tools into Symbiosis Brain tool-routing. Use when the user types
  /brain-tools, or asks to set up / onboard / configure / register their tools,
  MCP servers, or routing — e.g. "настрой мои инструменты", "set up tool routing",
  "добавь мои тулы в роутинг", "configure routing for my MCP servers". Lists the
  tools available in THIS install, auto-routes the ones it already knows, asks one
  short question per unknown tool, and writes a per-install override file so each
  user tailors routing to their own toolbox without editing package code.""",
    "brain-welcome": """name: brain-welcome
description: >
  First-time onboarding for a new Symbiosis Brain installation. Triggered ONLY
  by brain-init Step 0 when <vault>/.sb-initialized is absent. Shows pitch,
  collects user introduction → CRITICAL_FACTS.md, optionally offers tour and
  Obsidian. Writes .sb-initialized marker on success — never re-runs.""",
}

INTENT_ANCHORS = {
    "brain-autolearn": (
        "Rule of three.",
        "## Step 1: Repetition sweep",
        "## Step 2: Do NOT capture",
        "## Step 3: Classify and size",
        "## Step 4: Prefer patching over creating",
        "## Step 5: Make it, register it",
        "## Step 6: Count what is not ripe yet",
        "## Step 7: Report",
    ),
    "brain-backfill-gists": (
        "### Step 1: Find notes that need backfill",
        "### Step 2: Group by scope",
        "### Step 3: Dispatch parallel Explore subagents",
        "brain_patch",
        "### Step 4: Wait for all subagents to complete",
        "### Step 5: Report to user",
        "### Idempotency",
    ),
    "brain-init": (
        "### Step 0: First-time onboarding check",
        "### Step 1: Verify MCP server",
        "### Step 2: Load critical facts (L0)",
        "### Step 3: Detect & validate scope",
        "#### Step 3.1: Vault check when source=hook",
        "### Step 4: Load project context (L1)",
        "### Step 4.5: Umbrella-aware bootstrap",
        "### Step 4.6: Surface latest handoff",
        "### Step 5: Recovery scenarios",
        "### Step 6: Brief summary",
    ),
    "brain-project-init": (
        "### Step 1: Acquire lock",
        "### Step 2: Ask the user",
        "### Step 3: Create vault project card",
        "### Step 4: Append row to scope-taxonomy",
        "### Step 5: Append marker to <project>/CLAUDE.md",
        "### Step 6: Sync",
        "### Step 7: Release lock",
        "### Step 8: Brief confirmation",
        "## Idempotency",
        "## Error recovery",
    ),
    "brain-save": (
        "### Step 0: Retrospective",
        "invoke skill `brain-autolearn`",
        "### Step 1: Quality gate",
        "### Step 2: Deduplication check — BLOCKING",
        "### Step 3: Determine metadata",
        "### Step 4: Wiki-links",
        "### Step 4.5: Write gist field",
        "### Step 5: Apply depth rules",
        "### Step 6: Save",
        "### Step 7: Confirm + closure",
        "### MCP fallback",
        "### Dogfooding",
    ),
    "brain-tools": (
        "## Fail-open contract",
        "### Step 1: List the tools available in THIS install",
        "### Step 2: Split into KNOWN vs UNKNOWN",
        "### Step 3: One question per UNKNOWN tool",
        "### Step 4: Draft a route from the answer",
        "### Step 5: Write / merge into the vault override",
        "### Step 6: Confirm",
        "## Idempotency",
        "## Action rules (`class:\"action\"`)",
    ),
}


def _text(name: str) -> str:
    return (SKILLS / name / "SKILL.md").read_text(encoding="utf-8")


def _frontmatter(text: str) -> str:
    match = re.match(r"\A---\n(.*?)\n---\n", text, re.DOTALL)
    assert match is not None
    return match.group(1)


def _section(text: str, heading: str) -> str:
    marker = f"## {heading}\n"
    start = text.index(marker) + len(marker)
    next_heading = text.find("\n## ", start)
    return text[start:] if next_heading < 0 else text[start:next_heading]


def _client_branch(section: str, client: str) -> str:
    marker = f"### {client}\n"
    start = section.index(marker) + len(marker)
    next_branch = section.find("\n### ", start)
    return section[start:] if next_branch < 0 else section[start:next_branch]


def _squash(text: str) -> str:
    return " ".join(text.split())


def _between(text: str, start: str, end: str) -> str:
    start_at = text.index(start)
    end_at = text.index(end, start_at + len(start))
    return text[start_at:end_at]


def test_canonical_skill_inventory_and_frontmatter_are_preserved() -> None:
    actual = {
        path.parent.name
        for path in SKILLS.glob("*/SKILL.md")
        if path.is_file()
    }
    assert actual == CANONICAL_SKILLS
    for name, expected in EXPECTED_FRONTMATTER.items():
        assert _frontmatter(_text(name)) == expected


@pytest.mark.parametrize("name", sorted(INTENT_ANCHORS))
def test_original_ritual_steps_remain_in_order(name: str) -> None:
    text = _text(name)
    cursor = 0
    for anchor in INTENT_ANCHORS[name]:
        cursor = text.index(anchor, cursor) + len(anchor)


@pytest.mark.parametrize(
    ("name", "digest"),
    (
        ("brain-recall", "52f83f15881866cbd2d05ef62728b2ba5f90c41a482bccdd8f0eaea35da5589e"),
        ("brain-welcome", "fc6e39257fb2a3b2852e99c5bac63e35efaab39a64f9a6c05191806e02427271"),
    ),
)
def test_skills_without_unsupported_calls_stay_byte_for_byte_canonical(
    name: str,
    digest: str,
) -> None:
    # Git may materialize text files with CRLF on Windows. The canonical
    # content invariant is line-ending independent; edits still change digest.
    payload = (SKILLS / name / "SKILL.md").read_bytes().replace(b"\r\n", b"\n")
    assert hashlib.sha256(payload).hexdigest() == digest


@pytest.mark.parametrize("name", sorted(CLIENT_SCOPED_SKILLS))
def test_client_scoped_skills_dispatch_natively_without_renaming_operations(
    name: str,
) -> None:
    compatibility = _section(_text(name), "Client compatibility")
    codex = _client_branch(compatibility, "Codex")
    opencode = _client_branch(compatibility, "OpenCode")
    claude = _client_branch(compatibility, "Claude Code (default)")
    compatibility_flat = _squash(compatibility)
    codex_flat = _squash(codex)
    opencode_flat = _squash(opencode)
    claude_flat = _squash(claude)

    assert "logical Brain operation" in compatibility_flat
    assert "actual exposed tool" in compatibility_flat
    assert "MCP prefix" in compatibility_flat
    assert ".claude" not in codex_flat
    assert "skills.paths" in opencode_flat
    assert "skillsDir" in opencode_flat
    assert "existing Claude behavior below unchanged" in claude_flat


def test_nested_skill_calls_use_native_invocation_or_execute_canonical_skill() -> None:
    expected = {
        "brain-init": ("brain-welcome", "brain-project-init"),
        "brain-save": ("brain-autolearn",),
    }
    for name, nested_skills in expected.items():
        compatibility = _section(_text(name), "Client compatibility")
        codex = _squash(_client_branch(compatibility, "Codex"))
        opencode = _squash(_client_branch(compatibility, "OpenCode"))
        for nested in nested_skills:
            assert nested in codex
            assert nested in opencode
        assert "native skill invocation" in codex
        assert "resources --skills" in codex
        assert "read its canonical `SKILL.md`" in codex
        assert "execute the original ritual" in codex
        assert "invent a `Skill` tool" in codex


def test_brain_init_resolves_and_verifies_native_vault_before_marker_probe() -> None:
    step = _between(
        _text("brain-init"),
        "### Step 0: First-time onboarding check",
        "### Step 1: Verify MCP server",
    )
    flat = _squash(step)
    status_step = _between(
        _text("brain-init"),
        "### Step 1: Verify MCP server",
        "### Step 2: Load critical facts (L0)",
    )
    status_flat = _squash(status_step)
    marker_probe = 'test -f "$SYMBIOSIS_BRAIN_VAULT/.sb-initialized"'

    assert "codex mcp get symbiosis-brain --json" in flat
    assert "SYMBIOSIS_BRAIN_VAULT" in flat
    assert "registered `--vault`" in flat
    assert "registered server environment" in flat
    assert "not inherited parent environment" in flat
    assert "SYMBIOSIS_BRAIN_OPENCODE_VAULT" in flat
    assert "brain_status" in flat
    assert "same physical vault" in flat
    assert "status is unavailable" in flat
    assert "not a vault mismatch" in flat
    assert "known resolved vault" in flat
    assert "<resolved-vault>/MEMORY.md" in flat
    assert "do not run welcome or any write" in flat
    assert "do not execute the marker probe below" in flat
    assert "skip Steps 3-5" in flat
    assert "continue the session" in flat
    assert "physical vault differs" in flat
    assert "stop Brain initialization" in flat
    assert "resolved path is unknown" in flat
    assert "continue the session without Brain memory" in flat
    assert "do not guess" in flat.lower()
    assert "<resolved-vault>/MEMORY.md" in status_flat
    assert "no resolved path" in status_flat
    assert step.index("codex mcp get symbiosis-brain --json") < step.index(marker_probe)


def test_scope_cli_dispatch_is_at_each_affected_use_site() -> None:
    init_step = _between(
        _text("brain-init"),
        "### Step 3: Detect & validate scope",
        "#### Step 3.1: Vault check when source=hook",
    )
    init_flat = _squash(init_step)
    assert "**Native clients — scope resolve:**" in init_step
    assert 'brain-cli scope-resolve "$PWD"' in init_flat
    assert (
        '"<absolute current interpreter>" -m symbiosis_brain.scope_cli '
        'scope-resolve "$PWD"'
    ) in init_flat
    assert (
        '"$SYMBIOSIS_BRAIN_OPENCODE_PYTHON" -m '
        'symbiosis_brain.scope_cli scope-resolve "$PWD"'
    ) in init_flat
    assert "**Claude Code default — Windows**" in init_step
    assert "**Claude Code default — macOS/Linux**" in init_step

    project = _text("brain-project-init")
    acquire = _between(project, "### Step 1: Acquire lock", "### Step 2: Ask the user")
    release = _between(project, "### Step 7: Release lock", "### Step 8: Brief confirmation")
    for block, operation in (
        (acquire, 'acquire-onboard-lock "$SCOPE" --timeout-s 120'),
        (release, 'release-onboard-lock "$SCOPE"'),
    ):
        flat = _squash(block)
        assert "**Native clients:**" in block
        assert f"brain-cli {operation}" in flat
        assert (
            f'"<absolute current interpreter>" -m symbiosis_brain.scope_cli '
            f"{operation}"
        ) in flat
        assert (
            f'"$SYMBIOSIS_BRAIN_OPENCODE_PYTHON" -m '
            f"symbiosis_brain.scope_cli {operation}"
        ) in flat
        assert "**Claude Code default:**" in block


def test_native_file_edits_and_parallel_work_are_dispatched_at_use_sites() -> None:
    project = _text("brain-project-init")
    taxonomy = _between(
        project,
        "### Step 4: Append row to scope-taxonomy",
        "### Step 5: Append marker to <project>/CLAUDE.md",
    )
    marker = _between(
        project,
        "### Step 5: Append marker to <project>/CLAUDE.md",
        "### Step 6: Sync",
    )
    recovery = _between(
        _text("brain-init"),
        "### Step 5: Recovery scenarios",
        "### Step 6: Brief summary",
    )
    for block in (taxonomy, marker, recovery):
        flat = _squash(block)
        assert "actual native file-edit capability" in flat
        assert "fake `Edit` tool" in flat

    backfill = _between(
        _text("brain-backfill-gists"),
        "### Step 3: Dispatch parallel Explore subagents",
        "### Step 4: Wait for all subagents to complete",
    )
    backfill_flat = _squash(backfill)
    assert "explicit model and reasoning effort" in backfill_flat
    assert "native parallel-agent interface" in backfill_flat
    assert "same bounded buckets sequentially" in backfill_flat
    assert "fake `Agent`" in backfill_flat
    assert "**Dispatch — Claude Code default:**" in backfill
    assert "**All clients — per-bucket procedure:**" in backfill
    assert backfill.index("**All clients — per-bucket procedure:**") < backfill.index(
        "1. Receives the list of paths in its scope."
    )
    assert "logical `brain_patch` operation" in backfill_flat
    assert "Claude Code spelling example" in backfill_flat
    assert "logical `brain_sync` operation" in backfill_flat


def test_codex_capabilities_are_not_inferred_from_codex_home() -> None:
    backfill_compat = _squash(
        _section(_text("brain-backfill-gists"), "Client compatibility")
    )
    autolearn_compat = _squash(
        _section(_text("brain-autolearn"), "Client compatibility")
    )
    combined = "\n".join(
        _text(name) for name in ("brain-init", "brain-project-init")
    )

    assert "parallel-agent capability exposed by the current session" in backfill_compat
    assert "parallel-agent capability" in backfill_compat
    assert "available under `$CODEX_HOME`" not in backfill_compat
    assert "actual tool inventory exposed by the current session" in autolearn_compat
    assert "`$CODEX_HOME` only for skill files and client configuration" in autolearn_compat
    assert "native skill and tool inventories under `$CODEX_HOME`" not in autolearn_compat
    assert "sys.executable" not in combined
    assert '"<absolute current interpreter>" -m symbiosis_brain.scope_cli' in combined


def test_brain_tools_uses_actual_inventory_and_actual_expected_tool_identifier() -> None:
    text = _text("brain-tools")
    compatibility = _squash(_section(text, "Client compatibility"))
    fail_open = _between(text, "## Fail-open contract", "## Procedure")
    step1 = _between(
        text,
        "### Step 1: List the tools available in THIS install",
        "### Step 2: Split into KNOWN vs UNKNOWN",
    )
    step4 = _between(
        text,
        "### Step 4: Draft a route from the answer",
        "### Step 5: Write / merge into the vault override",
    )
    fail_open_flat = _squash(fail_open)
    step1_flat = _squash(step1)
    step4_flat = _squash(step4)

    assert "under `$CODEX_HOME`" not in compatibility
    assert "Native clients" in fail_open_flat
    assert "actual tool inventory" in fail_open_flat
    assert "Claude Code default" in fail_open_flat
    assert "actual tool inventory exposed to this session" in step1_flat
    assert "codex mcp list --json" in step1_flat
    assert "native skill list" in step1_flat
    assert "OpenCode" in step1_flat
    assert "skills.paths" in step1_flat
    assert "**Claude Code default sources:**" in step1
    assert "actual exposed tool identifier from Step 1" in step4_flat
    assert "`mcp__<server>__<tool>` is only the Claude Code example" in step4_flat
    assert "actual server name from Step 1" in step4_flat
    assert "actual observable tool identifier" in step4_flat
    assert "Claude Code default set" in step4_flat


def test_brain_tools_resolves_native_vault_and_packaged_catalog_at_use_sites() -> None:
    text = _text("brain-tools")
    fail_open = _squash(_between(text, "## Fail-open contract", "## Procedure"))
    step2 = _between(
        text,
        "### Step 2: Split into KNOWN vs UNKNOWN",
        "### Step 3: One question per UNKNOWN tool",
    )
    step5 = _between(
        text,
        "### Step 5: Write / merge into the vault override",
        "### Step 6: Confirm",
    )
    step2_flat = _squash(step2)
    step5_flat = _squash(step5)

    assert "resolved native vault" in fail_open
    assert "Claude Code default" in fail_open
    assert "explicit `SYMBIOSIS_BRAIN_VAULT`" in step2_flat
    assert "codex mcp get symbiosis-brain --json" in step2_flat
    assert "registered `--vault`" in step2_flat
    assert "registered server environment" in step2_flat
    assert "not inherited parent environment" in step2_flat
    assert "SYMBIOSIS_BRAIN_OPENCODE_VAULT" in step2_flat
    assert "brain_status" in step2_flat
    assert "symbiosis-brain resources --routing" in step2_flat
    assert "importlib.resources" not in step2_flat
    assert "absolute interpreter" not in step2_flat
    assert "data/tool-routing.json" in step2_flat
    assert "**Claude Code default catalog path:**" in step2
    assert "<resolved-vault>/tool-routing.local.json" in step5_flat
    assert "known and writable" in step5_flat
    assert "**Claude Code default path:**" in step5


def test_public_resources_routing_contract_is_read_only_and_requires_one_flag(
    capsys: pytest.CaptureFixture[str],
) -> None:
    expected = (
        ROOT / "src" / "symbiosis_brain" / "data" / "tool-routing.json"
    ).resolve(strict=True)
    before = expected.read_bytes()

    parser = install_cli.build_parser()
    args = parser.parse_args(["resources", "--routing"])
    assert args.func(args) == 0

    routing = Path(capsys.readouterr().out.strip()).resolve(strict=True)
    assert routing == expected
    assert routing.is_file()
    assert routing.read_bytes() == before

    with pytest.raises(SystemExit):
        parser.parse_args(["resources"])
    with pytest.raises(SystemExit):
        parser.parse_args(["resources", "--skills", "--routing"])


def test_autolearn_skill_install_targets_and_action_rule_commands_are_concrete() -> None:
    automation = (
        SKILLS / "brain-autolearn" / "references" / "automation-recipe.md"
    ).read_text(encoding="utf-8")
    automation_flat = _squash(automation)
    assert "package `skillsDir` is read-only" in automation_flat
    assert "writable development source" in automation_flat
    assert "explicit `skills.paths`" in automation_flat
    assert "native writable user skill path" in automation_flat

    recipe = (
        SKILLS
        / "brain-autolearn"
        / "references"
        / "action-rule-recipe.md"
    ).read_text(encoding="utf-8")
    recipe_flat = _squash(recipe)
    assert 'symbiosis-brain compile-action-rules --vault "<resolved-vault>"' in recipe_flat
    assert (
        '"<absolute interpreter for the installed package>" -m symbiosis_brain '
        'compile-action-rules --vault "<resolved-vault>"'
    ) in recipe_flat
    assert (
        '"$SYMBIOSIS_BRAIN_OPENCODE_PYTHON" -m symbiosis_brain '
        'compile-action-rules --vault "<resolved-vault>"'
    ) in recipe_flat
    assert "SYMBIOSIS_BRAIN_OPENCODE_PYTHON" in recipe_flat
    assert "synthetic pre-action smoke entrypoint" not in recipe_flat
    assert "positive/negative compiler vectors passed; no live hook proof" in recipe_flat


def test_native_save_fallback_never_guesses_a_memory_path() -> None:
    fallback = _between(
        _text("brain-save"),
        "### MCP fallback",
        "### Dogfooding",
    )
    flat = _squash(fallback)
    assert "<resolved-vault>/MEMORY.md" in flat
    assert "known and writable" in flat
    assert "report the knowledge as unsaved" in flat
    assert "do not guess" in flat.lower()
    assert "Claude Code default" in flat
