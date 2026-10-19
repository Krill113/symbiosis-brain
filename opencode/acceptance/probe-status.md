# Status TUI acceptance

## Verified source contract

Checked against OpenCode v1.19.0 `packages/tui/src/feature-plugins/sidebar/context.tsx`:

- Native context usage selects the last assistant message with `tokens.output > 0`.
- Its normalized context count is exactly `input + output + reasoning + cache.read + cache.write`.
- The percentage is `Math.round(tokens / model.limit.context * 100)` using the provider/model on that assistant message.
- The assistant DTO exposes `providerID`, `modelID`, `variant`, `summary`, and token counters; the provider model DTO exposes `limit.context`.
- The supported TUI state API exposes provider, MCP, route, session, and message readers. It does not expose current model/variant selection, so this module reports a labeled `Used:` fallback.

This implementation follows the host's usage formula, filters summary/helper responses, treats missing usage as unknown, and invalidates the previous working usage at compaction until a subsequent non-summary assistant response. Disabled MCP entries are not counted in the enabled denominator.

## Isolated UI probe

| Probe | Result |
| --- | --- |
| Focused projection tests | Pass: 12/12 |
| OpenCode Node suite | Pass: 104/104 |
| TypeScript typecheck | Pass |
| Actual OpenCode 1.19.0 TUI in owned Orca terminal with isolated HOME/USERPROFILE, temp, XDG/client config/data/cache/state and synthetic project/vault; secure launcher path resolved before child profile redirection | Host launched; isolated-profile preflight passed |
| Native TUI usage and MCP baseline after one synthetic working response | Observed native line (provider label generalized) `Build · gpt-6-luna Custom GPT Gateway · medium`; native footer `1.9K (0%)`, `1 MCP`; assistant replied `STATUS_LIVE_OK` |
| Configured `app_bottom` module, 80-column Orca terminal | Pass after correcting the isolated probe path to `opencode/tui.tsx`: before work `Model: ? · Context: ? · MCP: 1/1`; after response (provider ID generalized) `Used: custom/gpt-6-luna / medium · Context: 1,925 / 922,000 · 0% · MCP: 1/1` |
| Configured `app_bottom` module, 48-column Orca terminal | Pass: after the real synthetic bootstrap response, `Used: gpt-6-luna / medium · 2.1k/922k 0% · 1/1` was visible |
| 40-column × 45-row Orca PTY | Host accepted the requested PTY size and completed a synthetic response, but the custom `app_bottom` line was absent from the captured 40-column screen. |
| Model/variant switch-before-submit and subsequent work | Native `/models` selection changed to `gpt-5.6-luna` with `low`; `Used:` continued to report the prior completed `gpt-6-luna / medium` response until the next work request, then showed `Used: gpt-5.6-luna / low` |
| Live MCP enabled/disabled state change | Toggling the synthetic fixture MCP from disabled to enabled changed the status from `MCP: 1/1` to `MCP: 2/2`; toggling it back returned `MCP: 1/1`, without another model response |
| Server recall with status TUI disabled | Pass: the status plugin was disabled in the disposable `tui.json`; the separate server adapter remained configured and returned the hidden synthetic project-card phrase exactly on a neutral query |
| Server recall with status TUI enabled | Pass: at 48 columns, the adapter returned the exact random synthetic project-card oracle; the status line displayed the actual model, context usage/limit and MCP count |
| Orca / exact `/commands` alias coexistence | Pass in a fresh owned isolated Orca terminal: typed `/`, inspected autocomplete containing `/commands  Show all commands`, then typed `commands` and inspected the filtered registered suggestion before selecting it with Enter. The native Commands palette opened and listed `Show all commands`; Escape dismissed it, returning to the composer with `Model: ? · Context: ? · MCP: 1/1` still visible. The disposable TUI config referenced this module and both validated installed TUI modules (`orca-opencode-status-tui` and `commands-palette`). |

The live runs used the existing secure launcher inside separately isolated Orca terminal children. The launcher generated provider configuration only under fake local application-data roots and used the existing current-SID credential mechanism; no config/profile/key was copied into fixtures. The TUI configuration path now asserts the exact module file exists and uses `opencode/tui.tsx`; this corrected the previous fixture-root resolution error. Probe source is ignored under `opencode/acceptance/probes/`.

The first failure was a harness path error, not an OpenCode API limitation: resolving three parent directories from `opencode/acceptance/probes` produced the repository root, then the harness appended `tui.tsx` instead of `opencode/tui.tsx`. The corrected harness asserts the file before launch. Normal and 48-column status output is confirmed, including model/variant selection and MCP state changes. Server recall succeeded with the TUI both enabled and disabled. The earlier 40×45 capture omitted the status row; the R3 live-gate follow-up below supersedes that observation with a renderer-sized capture where the row is visible.

Current-selection switching cannot be surfaced through the documented v1.19.0 TUI API; the view reports the last successful working request as `Used:`. The module rendered and updated in the actual 80- and 48-column host sessions; the 40-column follow-up is recorded below.

## R3 live-gate follow-up

An owned Orca terminal was launched with the saved secure launcher and fresh synthetic child profiles. A diagnostic-only label (`TASK8_TUI_DIAGNOSTICS=1`) exposed the supported OpenTUI renderer width/height alongside the actual status projection; the label is absent by default.

| Probe | Actual observation |
| --- | --- |
| 40×45 narrow screen | Screen capture showed the module line `D40x45 Model: ? · Context: ? · MCP: 1/1` in the final row. After a synthetic work reply, the line remained present with a `Used:` model and context/MCP values (some surrounding host rows visually crowd the 40-column footer). This confirms the module was rendered at width 40. |
| Resize-only reflow | In one unchanged session after a synthetic work response, the renderer reported `D78x47`. Splitting the owned TUI terminal reduced its actual renderer viewport and the captured status recomputed at `D36x23`; closing only the created split restored `D78x47`. No message, MCP or route change occurred during the resize sequence. The row was recalculated on both transitions; narrow split rendering is crowded by the adjacent pane. |
| Orca / command-palette TUI coexistence | Read-only inspection of the installed `tui.json` found the existing Orca status and `commands-palette` module paths. Both were referenced by URI in the disposable TUI config beside this module; both paths were validated as existing files. In the owned child, Ctrl+P opened the actual native `Commands` palette containing `Show all commands`; selecting/filtering that entry displayed the full palette. Esc closed it and the `D40x45 … MCP: 1/1` status line remained visible. |
| `/commands` alias spelling | The R4 follow-up below directly invoked the registered slash autocomplete and confirmed it opens the native Commands palette. |

The renderer-sized evidence identifies the prior 40-column omission as a probe/observation gap: on this owned fresh TUI run, `app_bottom` is present at actual 40×45 dimensions, and resize updates trigger a fresh projection. No production minimum-width workaround was added. All temporary owned Orca terminals were closed; shared config, keys and profiles were not copied or changed.

## R4 exact slash-alias follow-up

The existing live harness passed its isolated child-profile checks and generated a synthetic `tui.json` containing the status TUI plus read-only references to the installed Orca status and commands-palette modules. In the actual OpenCode prompt, `/` showed the registered `/commands` suggestion. After typing `commands` incrementally, the filtered `/commands  Show all commands` row was inspected before Enter. Enter opened the native Commands palette and displayed `Show all commands`; Escape closed it and the status row remained visible. This directly verifies the slash invocation and coexistence. No source change was needed. The status-plugin-disabled comparison was therefore not run.
