import type { TuiPluginApi } from '@opencode-ai/plugin/tui';
import type { Accessor } from 'solid-js';
import { projectStatus, readStatusInput } from './status-view.ts';

export function createStatusView(api: TuiPluginApi, width: Accessor<number>) {
  return () => {
    const route = api.route.current;
    const candidate = route.name === 'session' ? route.params?.sessionID : undefined;
    const sessionID = typeof candidate === 'string' ? candidate : undefined;
    return projectStatus(readStatusInput(api, api.state.ready ? sessionID : undefined, width()));
  };
}
