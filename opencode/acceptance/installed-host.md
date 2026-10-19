# Installed-host acceptance — OpenCode 1.19.0

Checked on Windows with a synthetic vault/project and a disposable profile,
using the built 0.7.1 wheel's package resources, not a developer package fallback.
No provider credentials or production configuration were copied.

## Fresh checks

- Public `init-vault` and `setup opencode` completed; package origin was verified
  under extracted wheel `site-packages` resources. A separate fresh-venv wheel
  upgrade/setup/doctor/uninstall test also passed.
- Native `debug paths` reported owned home/data/config/cache/state/temp paths.
- Native `debug config` accepted one flat server tuple bound to the synthetic
  vault. Native `debug skill` resolved `brain-init` and `brain-save` to the
  packaged canonical skill directory. No `--pure` mode was used.
- An owned local `serve` process exposed the normal 14-tool catalog; the Brain
  MCP server was connected. The owned process tree was terminated and waited.
- The actual Orca-hosted TUI loaded the packaged TUI module and rendered
  `Model: ? · Context: ? · MCP: 1/1` before any model turn. Slash autocomplete
  displayed `/brain-init`, `/brain-save` and `/brain-sync` with their descriptions.
  Only the disposable test terminal was closed after inspection.
- Live hook/skill/config canaries recorded no changes during these probes.
- A native `run` turn used a synthetic local OpenAI-compatible endpoint to
  inspect the actual request boundary. A fresh random reference existed only in
  the synthetic vault's critical facts, not in the user prompt: the endpoint
  observed it with the packaged adapter enabled and did not observe a new
  reference with the plugin omitted. Both runs completed. The endpoint returned
  a fixed response; this establishes request-context delivery, not a remote
  model's semantic use of the note.

## Host dependency bootstrap

In a sterile offline profile, both the Brain plugin and an independent minimal
plugin stalled during host configuration loading. A no-plugin control returned.
After preparing the host's pinned `@opencode-ai/plugin@1.19.0` dependency inside
the disposable profile, both minimal-control and packaged-Brain probes returned.
This does not establish that a fresh OpenCode profile is usable offline merely
because the Brain wheel contains its own assets.

## Evidence boundaries

The registration/TUI probes made no model request; the request-boundary probe
used only a local synthetic endpoint, without provider credentials or a remote
model. Earlier bounded 1.19 implementation checks cover hidden-token use,
native/MCP result preservation, child/helper
behavior, responsive footer and Orca/commands coexistence; they remain historical
evidence, not a claim that every scenario was repeated on the release wheel.

Real-profile deployment, provider-specific work, narrow-screen/model-switch
replay and multi-project field usefulness remain separate acceptance activities.
No public publication or automatic live-profile installation follows from these
checks. The synthetic probe is not a claim of operating-system sandboxing.
