/** @jsxImportSource @opentui/solid */
import type { TuiPlugin, TuiPluginModule } from '@opencode-ai/plugin/tui';
import { useTerminalDimensions } from '@opentui/solid';
import { createStatusView } from './src/host-ui.ts';

function StatusLine(props: { api: Parameters<TuiPlugin>[0] }) {
  const dimensions = useTerminalDimensions();
  const diagnostics = (globalThis as typeof globalThis & {
    process?: { env?: Record<string, string | undefined> };
  }).process?.env?.TASK8_TUI_DIAGNOSTICS === '1';
  const view = createStatusView(props.api, () => Math.max(0, dimensions().width - (diagnostics ? 7 : 0)));
  return <text fg={props.api.theme.current.textMuted}>{(() => {
    const size = dimensions();
    const text = view().text;
    return diagnostics ? `D${size.width}x${size.height} ${text}` : text;
  })()}</text>;
}

const tui: TuiPlugin = async (api) => {
  // Slot registration is owned by the host plugin lifecycle; the returned string
  // is an ID, not an unregister callback.
  api.slots.register({
    slots: {
      app_bottom: () => <StatusLine api={api} />,
    },
  });
};

const plugin: TuiPluginModule & { id: string } = {
  id: 'symbiosis-brain-status-tui',
  tui,
};

export default plugin;
