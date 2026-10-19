# OpenCode 1.18.35 host contracts

Checked against the pinned `v1.18.35` source and documentation on 2026-10-10.
The adapter may rely only on the fields listed here. The Node tests are contract
sanity checks; they do not substitute for the isolated runtime probes below.

## Server hooks and results

| Hook | Proven input/output | Evidence |
| --- | --- | --- |
| `chat.message` | `input.sessionID`; optional `agent`, `model`, `messageID`, `variant`; output `{ message, parts }` | `packages/plugin/src/index.ts` `Hooks` |
| `shell.env` | input `{ cwd, sessionID?: string, callID?: string }`; mutable output `{ env: Record<string,string> }` | `packages/plugin/src/index.ts` `Hooks` |
| `tool.execute.before` | input `{ tool, sessionID, callID }`; mutable output `{ args }` | `packages/plugin/src/index.ts` `Hooks` and `session/tools.ts` |
| `tool.execute.after`, native | input `{ tool, sessionID, callID, args }`; mutable result `{ title, output, metadata, attachments? }` | `packages/plugin/src/index.ts`; native execute path in `session/tools.ts` |
| `tool.execute.after`, MCP | Same hook input; callback receives the raw MCP `CallToolResult`: `content` plus optional fields such as `isError`, `structuredContent`, and `_meta` | `session/tools.ts` passes the MCP execute result directly; `mcp/catalog.ts` creates/returns it |
| `experimental.chat.system.transform` | input `{ sessionID?: string, model }`; mutable output `{ system: string[] }` | `packages/plugin/src/index.ts`; called from `session/llm/request.ts` before messages are prepared |
| `experimental.chat.messages.transform` | input `{}`; mutable output `{ messages: { info, parts }[] }`; invoked during compaction over cloned selected history | `packages/plugin/src/index.ts`; `session/compaction.ts` |
| `experimental.session.compacting` | input `{ sessionID }`; output `{ context: string[], prompt?: string }` | `packages/plugin/src/index.ts`; `session/compaction.ts` |

There is no public request-purpose field on the system transform input. That
callback is used by requests prepared through the normal LLM request path; it
must not assume that it identifies only a user-facing work request. In
particular, do not infer a purpose from undocumented metadata.

Native after-results preserve the native result object. MCP after-results are
triggered with the raw `CallToolResult` **before** OpenCode formats it for the
model; they do not yet have OpenCode's normalized `title`, `output`,
`metadata`, or `attachments` fields. The public `Hooks` declaration types the
output using the native shape and does not fully describe the MCP runtime
object. The later MCP normalizer reads `content`, makes text/model output,
creates attachments for supported image content, and preserves structured
metadata separately. MCP `isError` results throw before `tool.execute.after`.
Mutators must edit the raw text content and retain non-text and unknown fields.

## TUI

- TUI configuration is `tui.json`; plugin entries are a string spec or
  `[spec, options]`.
- `@opencode-ai/plugin/tui` exports `TuiPlugin` and `TuiPluginModule` types.
- A TUI module is a default-exported `{ id?, tui }`; it must not also export
  `server`. A file/path module must provide a non-empty `id`; npm modules may
  fall back to the package name. Plugin packages supporting both targets need
  separate entry files.
- `tui` is called as `(api, options, meta) => Promise<void>`.
- Status line placement uses the documented `app_bottom` slot and
  `api.slots.register({ slots: { app_bottom: renderer } })`. The plugin slot
  descriptor has no caller-provided `id`; `register` returns a host-assigned
  string ID, not a disposer. Slot context exposes only `theme`.
- `api.state` exposes `ready`, `config`, provider list, `path`, optional VCS,
  session readers (`count`, `get`, `messages`, `status`, `todo`, `diff`,
  `permission`, `question`), `part(messageID)`, `lsp()`, and `mcp()`. Provider
  and MCP availability come from `state.provider` and `state.mcp()`; per-session
  messages come from `state.session.messages(sessionID)`. `api.client` is the
  current runtime client and `api.event.on(type, handler)` reads the event
  stream.
- `app_bottom` has no session-id prop. Obtain a session ID only when
  `api.route.current.name === "session"`, from `params.sessionID`, then use
  `api.state.session.get(id)` / `messages(id)` with missing-state checks. On
  other routes, or before state is ready, show unavailable/unknown rather than
  guessing an active session.
- Current-selection model/variant is not established as a supported status API
  by this contract. Use an explicitly labeled last-used value only if
  independently verified; otherwise show unknown.
- TSX uses `/** @jsxImportSource @opentui/solid */` and the host's TUI plugin
  loader. Do not combine server and TUI exports in one module.

## Pinned local toolchain

- OpenCode CLI: `1.18.35`
- `@opencode-ai/plugin`, `@opencode-ai/sdk`: `1.18.35`
- `@opentui/core`, `@opentui/solid`, `@opentui/keymap`: `0.5.17`; peer `solid-js`: `1.9.12`
- Node test runner: `node --experimental-strip-types --test
  opencode/tests/host-contracts.test.ts`
- The test runner contract test does not establish callback delivery, helper
  isolation, native/MCP result mutation, or TUI rendering at runtime.

## Isolated live probes

Record each probe's command, exit result and observed behavior here. Use a
disposable config directory and clear inherited `OPENCODE_CONFIG`,
`OPENCODE_CONFIG_CONTENT`, provider/launcher credentials and vault settings in
the child process. Do not use `--pure` because external plugins must load.

| Probe | Result |
| --- | --- |
| System context reaches work and remains available around helpers/compaction | Pass with exact-match oracle. With snapshot injection omitted, a neutral question did not match the hidden random oracle (`oracleMatch=false`, hook logged `snapshotInjected=false`). With injection enabled, the same neutral question exactly matched the separate oracle (`oracleMatch=true`, `snapshotInjected=true`); neither question contained the value. After visible `/compact` completed, a new unpredictable token was generated; the old summary/user history could not contain it. A neutral post-compaction question matched the new oracle, with `freshSystemHook=true`, `compactObserved=true`, and session state available. No value is stored in reports or result logs. |
| Native and MCP `tool.execute.after` result mutations | Pass with exact-match and omitted-mutation controls. For each tool, negative control called exactly one tool with after-hook injection disabled and did not match its hidden oracle. Positive control called exactly one tool; only after-hook mutation appended its independently generated audit signature, the next model response exactly matched the oracle, and callback/result-preservation checks passed. MCP callback remained raw (`_meta`, `content`, `extensionField`, `isError`, `structuredContent`, without normalized `output`); image/structured/meta/unknown fields survived. |
| TUI plugin state access and `app_bottom` rendering | Pass. Pinned TUI module loaded in visible Orca terminal and rendered `SB_TUI_PROBE` with `state=ready`, provider/MCP counts, then `session=available` after a work request. R2 used the same visible host path to confirm exact fresh-token use after compaction. |

Probe runtime dependency versions, read from the disposable config's installed
packages: `@opencode-ai/plugin`/`@opencode-ai/sdk` 1.18.35,
`@opentui/{core,solid,keymap}` 0.5.17, `solid-js` 1.9.12. These match the
adapter pins and v1.18.35 host evidence; no unversioned `latest` dependency was
used for the compatibility run. `@opentui/core` declares Node >=26.4/Bun >=1.3;
the Node 24.14 runner was used only for strip-types tests/typecheck, while the
actual visible TUI probe passed through the installed OpenCode host.
