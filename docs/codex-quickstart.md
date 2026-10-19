# Codex: manual setup for Symbiosis Brain 0.7.1

This guide describes the released 0.7.1 contract. Install 0.7.1 from PyPI;
older packages do not contain these commands. This guide is not permission to
modify someone else's profile.

There is **no automatic Codex installer** in this version. Do not run
`setup codex`, `doctor codex`, `uninstall codex`, or `setup claude-code` as a
substitute. Claude Code does not need to be installed.

## 1. Prerequisites and paths

- Python 3.12 or later with the package's supported binary dependencies. Python
  3.14 is the interpreter used for the current Windows regression tests.
- Codex CLI 0.161.0 is the pinned hook-protocol baseline. Confirm your native
  client exposes `/hooks` and the events below. Other versions need their own
  hook/skill acceptance; a successful package install does not prove it.
- One private, absolute vault path used consistently by MCP and hooks. Reuse an
  existing Brain vault only deliberately. Keep its existing model/index settings;
  do not create a different embedding-model configuration for each client.
- One active Codex home: `CODEX_HOME`, or the client's default `~/.codex`.
  Confirm the actual home used by your launcher before copying anything.
- Git Bash and `git`, `timeout`, `find`, `date` are required only for optional
  packaged Git sync. Python lifecycle hooks do not require Bash.

For examples below substitute:

| Name | Meaning |
|---|---|
| `BRAIN` | absolute installed `symbiosis-brain` executable |
| `VAULT` | absolute private vault directory |
| `CODEX_HOME` | actual home used by this Codex launcher |
| `PROJECT` | a project directory for first onboarding |

Do not copy another user's Codex/Claude profile, API keys, notes or index. For
acceptance testing, redirect HOME and USERPROFILE, client/AppData/XDG/temp/cache
directories before starting any process, and use a synthetic vault. A Git
worktree by itself does not isolate those locations.

## 2. Install the package, not a client profile

```text
uv tool install --python 3.14 "symbiosis-brain==0.7.1"
symbiosis-brain --version
symbiosis-brain --help
symbiosis-brain resources --skills
```

For a reviewed development build or release artifact, install the exact supplied
wheel instead of the PyPI requirement. Do not install an arbitrary branch and
assume it is that artifact. `resources --skills` only prints the packaged
canonical skills directory; it does not copy anything or change profiles.

Resolve `BRAIN` explicitly (`Get-Command symbiosis-brain` in PowerShell or
`command -v symbiosis-brain` on POSIX). The hook command uses the installed
executable, not `uv run`, an unqualified `python`, or a development checkout.
The renderer below is a public package command; no internal uv-tool interpreter
path needs to be guessed.

## 3. Prepare a vault without Claude Code

For a new vault:

```text
symbiosis-brain init-vault --vault "VAULT"
```

Use the real absolute path, not the literal word VAULT. This creates the existing
canonical vault structure, README, minimal scope taxonomy, fallback MEMORY.md and
Git ignore/attributes. It preserves existing notes; it may append missing Git
metadata lines and set private POSIX root permissions. It does not install
client resources, register MCP, select an embedding model or initialize vectors.

Do **not** pre-create `.sb-initialized`: first-use `brain-init` invokes the
canonical welcome/introduction ritual and writes the marker only on success.
`CRITICAL_FACTS.md` comes from that introduction, not another user's fixture.

MCP server startup initializes/synchronizes the index. A first start may download
the configured local embedding model; prepare dependencies/model cache during
installation, not in a latency-sensitive hook. Offline deployments need an
already prepared compatible cache. No model API key is needed for local search.

## 4. Register the existing MCP server explicitly

First inspect existing registrations:

```text
codex mcp list --json
codex mcp get symbiosis-brain --json
```

An absent registration may give a nonzero result; distinguish absence from an
unreadable/corrupt configuration. If a working registration already points to the
same physical vault, preserve it. If it differs, stop and review that difference
and a backup with the owner; do not silently remove or replace it.

For a genuinely absent registration:

```text
codex mcp add --env "SYMBIOSIS_BRAIN_VAULT=VAULT" symbiosis-brain -- "BRAIN" serve --vault "VAULT"
```

Replace BRAIN/VAULT with the same absolute values chosen above. This is an explicit
native-client profile mutation, not an action performed by `init-vault`.

Restart/reconnect as the native client requires. Ask the agent to discover the
server's actual tool names, call logical `brain_status`, and verify the expected
vault and a healthy index. Client namespace spelling can differ; do not hardcode
another host's MCP prefix.

## 5. Make the canonical skills available

```text
symbiosis-brain resources --skills
```

The printed directory contains the same canonical skills used by Claude and
OpenCode:

`brain-init`, `brain-recall`, `brain-save`, `brain-tools`, `brain-welcome`,
`brain-project-init`, `brain-backfill-gists`, `brain-autolearn`.

For the pinned Windows Codex 0.161.0 installation, native `skills/list` was
observed discovering a synthetic **user** skill in `CODEX_HOME/skills`; use that
client-specific directory to avoid a second copy in OpenCode's shared user roots.
Confirm discovery in your client's skill list after restart.

Current official Codex documentation also describes `$HOME/.agents/skills`.
That is a shared location, not a reason to install a second copy. On the tested
Windows native host, changing HOME and USERPROFILE did not redirect every user
skill scan: the process still enumerated the operating system's user skill
directory. Environment-only redirection therefore does not prove native-host
isolation; use an isolated OS account/VM for strict clean-profile acceptance.
Do not infer actual user skill precedence from internal system skills alone.

Inspect each destination before a deliberate copy of its canonical directory:

1. Missing destination: copy that directory and its supporting resources.
2. Identical bytes: leave it as is.
3. Different bytes or the same skill name already discovered elsewhere: preserve
   the user's copy and resolve the conflict explicitly. Do not overwrite or
   silently install a second version.

Do not install a Codex-specific fork into shared `~/.agents/skills`.
OpenCode can reference the same package's canonical directory through its native
skill paths; it does not require a Claude installation or a second edited body.
If OpenCode also discovers the manual copy, resolve same-name duplication before
proceeding: preserve user bytes and select one canonical discovery source rather
than relying on unverified precedence or two versions silently overriding each
other. An identical hash is not by itself proof of the host's precedence rules.

Invoke `brain-init` using the native client skill mechanism, or ask the agent to
read that installed canonical SKILL.md and execute the **whole procedure** if the
client has no skill-invocation tool. Do not substitute a few MCP reads for the
welcome, scope onboarding, recall, save, retrospective or autolearn rituals.

During first `brain-init`, answer the welcome questions and onboard PROJECT. The
canonical project-init creates its project card, taxonomy entry and existing
`CLAUDE.md` scope marker. The marker is ordinary shared scope metadata; no Claude
client is required to read it. `AGENTS.md` is not a replacement scope registry.

Verify:

```text
brain-cli scope-resolve "PROJECT"
```

The returned scope should match the intended project. Unknown project scope still
permits canonical global memory; do not configure a Codex root allowlist or
`allow_global` switch. Those were superseded experimental restrictions.

## 6. Review hook JSON, then merge it manually

Generate JSON to stdout with the installed console executable:

PowerShell:

```powershell
& "BRAIN" codex-hooks --executable "BRAIN" --vault "VAULT"
```

POSIX shell:

```sh
"BRAIN" codex-hooks --executable "BRAIN" --vault "VAULT"
```

The renderer never writes profiles, creates the vault or grants trust. It accepts
absolute paths in a deliberately supported cross-shell domain: letters/digits,
underscores, spaces, dots, colons, path separators and hyphens. Unsupported shell
metacharacters, including `%`, are rejected rather than guessed at. Choose safe
paths or stop; do not hand-edit escaping and assume it is proven.
The Windows override uses PowerShell's call operator and literal path quoting
for the pinned native client, not `cmd.exe` batch escaping.

Back up the existing `CODEX_HOME/hooks.json` yourself. Review and merge only the
generated hook entries into that JSON, preserving unrelated events and entries.
Do not redirect output onto an existing file without reviewing the merge. Do
not install a second copy of the same hook.

The template covers SessionStart, UserPromptSubmit, PreToolUse, PostToolUse,
PreCompact, PostCompact, Stop and SessionEnd. Keep the per-event `--event` argument,
outer timeout and output budget: the event must be known before reading stdin
so the internal deadline includes slow input/config/scope resolution.

Open Codex `/hooks`, inspect the definitions and grant native hook trust only
after review. Restart Codex. Host trust/permission gates remain real user
decisions; removing the adapter root allowlist does not bypass them.

Optional `CODEX_HOME/symbiosis-brain.json`:

```json
{
  "bootstrap_visibility": "one-line",
  "handoff_stale_days": 30,
  "save_reminder_turns": 10,
  "status_line_policy": "recommend-only",
  "sync_mode": "manual",
  "sync_bash_path": null
}
```

Protect this user configuration from other users. Do not include obsolete
`trusted_projects` or `allow_global` settings. Silent startup visibility is
available, but start with visible diagnostics while verifying the integration.

## 7. Verify behavior before using private knowledge

In a synthetic project/vault first:

- Confirm native discovery of all intended skills and the correct MCP vault.
- Invoke the full brain-init ritual; verify global facts, project card and latest
  handoff. Confirm an unknown scope gets global rather than another project.
- Store a synthetic decision through `brain-save`, then recall it through
  `brain-recall`/`brain_search`; verify automatic prompt/action memory and routing.
- Exercise native tools, a successful MCP save, compact/resume and session end.
  Compaction restoration has one owner: SessionStart with source compact.
- Verify no raw secret sentinel enters diagnostics/state/retrieval logs.
- Review the installed native hook behavior, not merely renderer output or tests
  on another client version.

The runtime is advisory for normal tools. Stop can request one continuation at a
configurable turn interval (default ten), suppresses repeated reminders and
observes successful saves. It does not save every conversation automatically,
prevent every loss, or implement Claude's context-percentage policy.

Search uses the same project-plus-global hybrid semantics as the canonical core,
not a Codex-only FTS/privacy fork. Scope is for relevance; it is not a new access
control boundary around a manually callable MCP server.

For status, startup supports silent/one-line/verbose diagnostics. Existing native
Codex status-line settings remain yours; this version does not install a custom
persistent Brain widget, App Server bridge or percentage-based autosave.

## 8. Manual operations and optional Git sync

- Initialization: invoke the canonical `brain-init` skill.
- Recall: `brain-recall`, with the same logical MCP search/read operations.
- Save: `brain-save`, including its original retrospective/autolearn procedure.
- Index synchronization: logical MCP `brain_sync`; this does **not** push Git.
- Git synchronization, only with an intentionally configured vault Git repository:

```text
symbiosis-brain packaged-sync manual --vault "VAULT"
```

Packaged sync uses the shipped script, not `~/.claude/hooks/brain-sync.sh`.
It requires an explicitly verified absolute Bash path in the protected Codex
configuration's `sync_bash_path`, plus git/timeout/find/date. Without that setup it
reports unavailable. For this optional feature replace the JSON null above with
the verified executable path, for example
`"sync_bash_path": "C:/Program Files/Git/usr/bin/bash.exe"` on Windows or
`"sync_bash_path": "/usr/bin/bash"` on POSIX. These are examples, not discovery:
verify the actual executable and utilities in your installation. Use forward
slashes or escaped backslashes in JSON.

Git operations can commit, pull/rebase and push: review the
repository/remotes before invoking them. Auto-sync stays off by default; do not
enable it to compensate for an unverified manual setup.

## 9. Update, disable and remove deliberately

Before an update record the package/client version, executable path, hook entries,
skill hashes and backups. Install the reviewed package version, rerun
`resources --skills` and the stdout hook renderer, compare changes, and deliberately
merge only reviewed entries. Preserve user-edited skills/configuration and
unrelated registrations. Recheck trust/discovery after restart.

To disable: use the native hook UI to disable the Brain hooks, then remove only
the exact entries you added after comparing your backup. Do not restore an entire
old hooks file over newer unrelated edits.

To remove MCP, inspect the current registration first and remove
`symbiosis-brain` only if it is still the registration you added and removal is
intended. Remove only unchanged skill copies you installed and no longer need.
Shared canonical skills may still be used by another client. Preserve the vault
and index unless their deletion is separately requested.

There is no automatic Codex repair/uninstall rollback contract in 0.7.1. Do not
use another client's uninstall command. On a mismatch or uncertain ownership,
stop and retain evidence rather than repair a live profile automatically.
