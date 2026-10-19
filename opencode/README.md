# Symbiosis Brain for OpenCode — 0.7.1

The OpenCode adapter adds scoped project bootstrap, hybrid memory recall and
tool guidance to normal OpenCode sessions. It uses the same vault, search
algorithm and canonical skills as the other Brain clients. Claude Code does
not need to be installed.

## Requirements

- Windows for automatic setup, repair and removal. The protected configuration
  commit helper fails closed on other platforms; automatic installation there
  is not supported by this version.
- Python 3.12+ with the supported Brain dependencies; `uv` can manage Python.
- Node.js 22.6+ on PATH, or provide `--node PATH` to each management command.
- OpenCode **1.19.0** is the tested installed host. SDK declarations are pinned
  separately; they do not establish compatibility with arbitrary host versions.
- A usable Brain vault/index. The embedding model is downloaded on first use
  unless already cached. OpenCode may separately install its own pinned plugin
  dependencies on first startup; allow that normal host bootstrap or prepare its
  cache. An offline Brain wheel does not make a fresh OpenCode profile offline.
- `grep -E` for compiled shell-action rules. If unavailable, those rules report
  degraded availability rather than executing shell text or replacing ERE with
  a different regex engine.

## Install

Run these commands in PowerShell. Choose your own permanent vault path:

```powershell
uv tool install "symbiosis-brain==0.7.1"
symbiosis-brain init-vault --vault "$HOME/symbiosis-brain-vault"
symbiosis-brain setup opencode --vault "$HOME/symbiosis-brain-vault"
symbiosis-brain doctor opencode
```

For a reviewed development build or release artifact supplied as a wheel,
replace the first line with
`uv tool install "./symbiosis_brain-0.7.1-py3-none-any.whl"`. Successful local
acceptance remains separate from package installation.

**Quit and restart OpenCode.** Plugins and commands are loaded at startup.
Review the host's native permission/trust prompts normally; the adapter does not
grant tool permissions or replace provider credentials. Connect your preferred
provider through OpenCode. There is no fixed model/provider in Brain commands.

For an explicit configuration directory or Node executable:

```powershell
symbiosis-brain setup opencode --vault "$HOME/symbiosis-brain-vault" --config-dir "./test-opencode-config" --node "C:/Program Files/nodejs/node.exe"
symbiosis-brain doctor opencode --config-dir "./test-opencode-config" --node "C:/Program Files/nodejs/node.exe"
```

The installer respects the active OpenCode configuration directory. An explicit
`OPENCODE_CONFIG` must select `opencode.json` or `opencode.jsonc`; unsupported
filenames and ambiguous lower-precedence selections fail with a diagnostic.
The usual default is `$XDG_CONFIG_HOME/opencode`, or `~/.config/opencode`.

Setup registers one server module in `opencode.json[c]`, a separate TUI module
in `tui.json`, packaged skills through `skills.paths`, and three command files.
The Python interpreter, package, vault and MCP command come from the actual
installation. The vault and package are not deleted on removal. Existing MCP
registration must match the intended vault/runtime or be receipt-owned and
eligible for upgrade; a different registration is a conflict.

Unrelated configuration, Orca plugins and `/commands` are preserved. Legacy
recall copies or conflicting skill/command names require explicit resolution;
setup does not silently delete changed user files or disable unrelated skills.
Do not run two active recall implementations in the same project.

## Verify and use

1. `doctor opencode` reports an installed adapter with no conflicts.
2. OpenCode's slash menu contains `/brain-init`, `/brain-save`, `/brain-sync`.
3. `/brain-init` invokes the canonical bootstrap/onboarding skill. For a new
   project, follow its onboarding steps; a v1 scope marker in `CLAUDE.md` keeps
   project identity stable across clients.
4. `/brain-save` invokes the canonical retrospective and deduplicated saving
   workflow. Writes happen through Brain tools, not by copying the transcript.
5. `/brain-sync` explicitly explains that **vault Git sync is deferred**. It does
   not run Git or claim a sync. If you separately request index refresh, the
   agent may invoke MCP `brain_sync`.
6. The footer shows model/effort, last working-request context usage and MCP
   connectivity. Before a working response, unknown fields are `?`; last-used
   values are labeled rather than presented as a current selection.

Automatic recall observes prompts and relevant actions and appends a bounded
reference snapshot to the next applicable model request. It does not throw a
tool error, change tool arguments or ask you to repeat the tool. Recommendations
are filtered by actual available capabilities. Parent/child sessions maintain
separate generations and snapshots; helpers cannot consume the working snapshot.

Scope resolution stays inside the known project/worktree boundary and includes
global notes. Broad ancestor workspace scopes are not inherited as product
scopes. Unknown boundary/scope degrades to global context, not all-project search.
Explicit MCP search remains available for deeper recall.

The adapter reads the existing hybrid index; it does not repair, migrate or
silently replace vector search with keyword-only search. Initialize a fresh
index through the normal MCP server and its explicit `brain_sync` operation.
After editing Markdown outside Brain or pulling vault files, request index
refresh separately. Missing/incompatible index and model drift are diagnostics.

Soft save reminders use meaningful milestones and successful Brain write
results. There is no hard Stop or compaction block. Compaction may receive
reference context but is not asked to save or use tools. A process closed before
the next working request may not show a pending reminder: save useful findings
during work and use `/brain-save` explicitly before leaving.

## Update, repair, disable and remove

```powershell
uv tool upgrade symbiosis-brain
symbiosis-brain setup opencode --vault "$HOME/symbiosis-brain-vault" --repair
symbiosis-brain doctor opencode
symbiosis-brain uninstall opencode
```

Repeat explicit `--config-dir` / `--node` arguments if used at installation.
Quit OpenCode before changing its installation and restart after setup/repair.
Use `uninstall opencode` to disable/remove this adapter; unqualified
`uninstall` retains the existing Claude-client behavior.

The local `.symbiosis-brain-install` directory holds ownership, backups and
recovery metadata. Setup, doctor and removal can recover interrupted operations;
doctor is not a guaranteed read-only operation. User edits produce conflicts
with preserved bytes rather than stale-snapshot overwrite. Keep recovery
metadata when resolving a conflict; do not delete it merely to silence doctor.

## Package and developer build

Generated `opencode/dist` is a versioned release asset. Installing or building a
Python wheel from the release source/sdist does not require Node build tools or
the maintainer's development `node_modules`. Management operations still require
the Node runtime.

To regenerate the asset using the pinned lockfile:

```powershell
npm ci --prefix opencode
npm --prefix opencode run typecheck
npm --prefix opencode run build:installer
node --experimental-strip-types --test opencode/tests/artifact.test.ts
```

The build uses TypeScript `transpileModule`, preserves runtime TS/TSX relative
imports, emits the installer ESM closure, includes the RPC lock helper and vendors
`jsonc-parser` with its license. The manifest records source/artifact SHA-256
hashes without environment paths or timestamps. CI regenerates and checks for
stale tracked output. Wheels map `opencode/dist` to `symbiosis_brain/opencode`;
sdists retain `opencode/dist` so wheel-from-sdist uses the same mapping.

See [installed-host acceptance](acceptance/installed-host.md) for what was
actually checked and what still belongs to deployment/field acceptance.
