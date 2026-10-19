export type Usage = {
  input: number;
  output: number;
  reasoning: number;
  cacheRead: number;
  cacheWrite: number;
};

export type ModelSelection = { model: string; variant: string | null };

export type StatusInput = {
  currentSelection: ModelSelection | null;
  lastUsed: ModelSelection | null;
  usage: Usage | null;
  limit: number | null;
  compactionInvalidated: boolean;
  mcp: { connected: number; enabled: number } | null;
  width: number;
};

export type StatusView = {
  text: string;
  modelLabel: string;
  contextLabel: string;
  mcpLabel: string;
  contextUsed: number | null;
  contextPercent: number | null;
  unknown: { model: boolean; context: boolean; mcp: boolean };
};

type Message = {
  id: string;
  role?: string;
  providerID?: string;
  modelID?: string;
  variant?: string;
  summary?: boolean | object;
  tokens?: {
    input?: number;
    output?: number;
    reasoning?: number;
    cache?: { read?: number; write?: number };
  };
};

type StatusApi = {
  state: {
    ready: boolean;
    session: { messages(sessionID: string): ReadonlyArray<Message> };
    part(messageID: string): ReadonlyArray<{ type?: string }>;
    provider: ReadonlyArray<{
      id?: string;
      models?: Record<string, { limit?: { context?: number } }>;
    }>;
    mcp(): ReadonlyArray<{ status?: string }>;
  };
  renderer: { width: number };
};

const finiteNonnegative = (value: unknown): number | null =>
  typeof value === 'number' && Number.isFinite(value) && value >= 0 ? value : null;

/** Read only documented live TUI state. Current model/variant selection is not public in v1.19.0. */
export function readStatusInput(api: StatusApi, sessionId?: string, widthHint?: number): StatusInput {
  const width = finiteNonnegative(widthHint) ?? finiteNonnegative(api.renderer?.width) ?? 80;
  const unknown: StatusInput = {
    currentSelection: null,
    lastUsed: null,
    usage: null,
    limit: null,
    compactionInvalidated: false,
    mcp: null,
    width,
  };

  if (api.state.ready) {
    try {
      const mcp = api.state.mcp();
      const activeMcp = mcp.filter((item) => item.status !== 'disabled');
      unknown.mcp = {
        connected: activeMcp.filter((item) => item.status === 'connected').length,
        enabled: activeMcp.length,
      };
    } catch {
      // MCP state is global and independent of the active route/session.
    }
  }

  if (!api.state.ready || !sessionId) return unknown;

  let messages: ReadonlyArray<Message>;
  try {
    messages = api.state.session.messages(sessionId);
  } catch {
    return unknown;
  }

  let latestCompaction = -1;
  let latestWorkingIndex = -1;
  let latestWorking: Message | undefined;
  for (let index = messages.length - 1; index >= 0; index--) {
    const message = messages[index];
    if (latestWorkingIndex < 0 &&
      message?.role === 'assistant' &&
      message.summary !== true &&
      finiteNonnegative(message.tokens?.output) !== null &&
      (message.tokens?.output ?? 0) > 0
    ) {
      latestWorking = message;
      latestWorkingIndex = index;
    }
    if (latestCompaction < 0 && message?.role === 'user') {
      try {
        if (api.state.part(message.id).some((part) => part.type === 'compaction')) latestCompaction = index;
      } catch {
        // If parts are unavailable we do not infer invalidation from unrelated user content.
      }
    }
    if (latestWorkingIndex >= 0 && latestCompaction >= 0) break;
  }

  if (!latestWorking) return unknown;

  const providerID = latestWorking.providerID;
  const modelID = latestWorking.modelID;
  if (providerID && modelID) {
    unknown.lastUsed = {
      model: `${providerID}/${modelID}`,
      variant: typeof latestWorking.variant === 'string' ? latestWorking.variant : null,
    };
  }

  // A compaction response is a helper summary, not the usage/limit of user work.
  if (latestWorkingIndex <= latestCompaction) {
    unknown.compactionInvalidated = true;
    return unknown;
  }

  const tokens = latestWorking.tokens;
  const values = [
    finiteNonnegative(tokens?.input),
    finiteNonnegative(tokens?.output),
    finiteNonnegative(tokens?.reasoning),
    finiteNonnegative(tokens?.cache?.read),
    finiteNonnegative(tokens?.cache?.write),
  ];
  if (values.every((value): value is number => value !== null)) {
    const [input, output, reasoning, cacheRead, cacheWrite] = values as [number, number, number, number, number];
    unknown.usage = { input, output, reasoning, cacheRead, cacheWrite };
  }

  const provider = api.state.provider.find((item) => item.id === providerID);
  unknown.limit = finiteNonnegative(provider?.models?.[modelID ?? '']?.limit?.context);
  return unknown;
}

/** Project host-independent labels and honest unknown/stale state into one compact line. */
export function projectStatus(input: StatusInput): StatusView {
  const selection = input.currentSelection;
  const used = input.lastUsed;
  const modelLabel = selection
    ? `${selection.model}${selection.variant ? ` / ${selection.variant}` : ''}`
    : used
      ? `Used: ${used.model}${used.variant ? ` / ${used.variant}` : ''}`
      : 'Model: ?';

  const contextUsed = input.usage
    ? input.usage.input + input.usage.output + input.usage.reasoning + input.usage.cacheRead + input.usage.cacheWrite
    : null;
  const contextPercent = contextUsed !== null && input.limit !== null && input.limit > 0
    ? Math.round((contextUsed / input.limit) * 100)
    : null;
  const contextLabel = input.compactionInvalidated
    ? 'Context: stale'
    : contextUsed === null
      ? 'Context: ?'
      : `Context: ${formatCount(contextUsed)} / ${input.limit === null ? '?' : formatCount(input.limit)}${contextPercent === null ? '' : ` · ${contextPercent}%`}`;
  const mcpConnected = input.mcp?.connected;
  const mcpEnabled = input.mcp?.enabled;
  const width = Math.max(0, Math.floor(input.width));
  const text = formatLine(modelLabel, contextLabel, contextUsed, input.limit, contextPercent, mcpConnected, mcpEnabled, width);
  const mcpLabel = mcpConnected === undefined || mcpEnabled === undefined
    ? 'MCP: ?/?'
    : `MCP: ${mcpConnected}/${mcpEnabled}`;

  return {
    text,
    modelLabel,
    contextLabel,
    mcpLabel,
    contextUsed,
    contextPercent,
    unknown: {
      model: selection === null && used === null,
      context: contextUsed === null || input.limit === null,
      mcp: mcpConnected === undefined || mcpEnabled === undefined || !Number.isFinite(mcpConnected) || !Number.isFinite(mcpEnabled),
    },
  };
}

function formatCount(value: number): string {
  return Math.round(value).toLocaleString('en-US');
}

function compactCount(value: number): string {
  if (value < 1000) return String(Math.round(value));
  if (value < 1_000_000) return `${trimDecimal(value / 1000)}k`;
  return `${trimDecimal(value / 1_000_000)}m`;
}

function trimDecimal(value: number): string {
  return value.toFixed(value < 1000 ? 1 : 0).replace(/\.0$/, '');
}

function cellWidth(value: string): number {
  let width = 0;
  for (const char of value) {
    const cp = char.codePointAt(0) ?? 0;
    if (cp === 0 || cp < 32 || (cp >= 0x300 && cp <= 0x36f)) continue;
    width += cp >= 0x1100 && (
      cp <= 0x115f || cp === 0x2329 || cp === 0x232a ||
      (cp >= 0x2e80 && cp <= 0xa4cf) || (cp >= 0xac00 && cp <= 0xd7a3) ||
      (cp >= 0xf900 && cp <= 0xfaff) || (cp >= 0xfe10 && cp <= 0xfe19) ||
      (cp >= 0xfe30 && cp <= 0xfe6f) || (cp >= 0xff00 && cp <= 0xff60) ||
      (cp >= 0xffe0 && cp <= 0xffe6) || (cp >= 0x1f300 && cp <= 0x1faff) ||
      (cp >= 0x20000 && cp <= 0x3fffd)
    ) ? 2 : 1;
  }
  return width;
}

function fit(value: string, width: number): string {
  if (cellWidth(value) <= width) return value;
  if (width <= 0) return '';
  if (width === 1) return '…';
  let result = '';
  for (const char of value) {
    if (cellWidth(`${result}${char}…`) > width) break;
    result += char;
  }
  return width === 1 ? result : `${result}…`;
}

function formatLine(
  model: string,
  context: string,
  used: number | null,
  limit: number | null,
  percent: number | null,
  mcpConnected: number | undefined,
  mcpEnabled: number | undefined,
  width: number,
): string {
  const separator = ' · ';
  const mcpFull = mcpConnected === undefined || mcpEnabled === undefined
    ? 'MCP: ?/?'
    : `MCP: ${mcpConnected}/${mcpEnabled}`;
  const mcpCompact = mcpConnected === undefined || mcpEnabled === undefined
    ? 'MCP ?/?'
    : `MCP ${mcpConnected}/${mcpEnabled}`;
  const contextCompact = used === null
    ? context
    : `Context: ${compactCount(used)} / ${limit === null ? '?' : compactCount(limit)}${percent === null ? '' : ` · ${percent}%`}`;
  const contextShort = used === null
    ? context
    : `${compactCount(used)}/${limit === null ? '?' : compactCount(limit)} ${percent === null ? '' : `${percent}%`}`.trim();
  const modelVariants = [model, compactModelLabel(model)];
  const contextVariants = [context, contextCompact, contextShort];
  const mcpVariants = [mcpFull, mcpCompact, mcpConnected === undefined || mcpEnabled === undefined ? '?/?' : `${mcpConnected}/${mcpEnabled}`];

  for (const contextText of [...new Set(contextVariants)]) {
    for (const mcpText of [...new Set(mcpVariants)]) {
      const suffix = `${separator}${contextText}${separator}${mcpText}`;
      const room = width - cellWidth(suffix);
      if (cellWidth(model) <= room) return `${model}${suffix}`;
      if (cellWidth(modelVariants[1] ?? model) <= room) return `${modelVariants[1]}${suffix}`;
    }
  }

  // Keep all fields recognizable at extreme widths; model abbreviation is explicit.
  const minSuffix = `${separator}${contextShort}${separator}${mcpVariants[2]}`;
  const modelRoom = Math.max(0, width - cellWidth(minSuffix));
  return `${fitModelLabel(modelVariants[1] ?? model, modelRoom)}${fit(minSuffix, width)}`;
}

function compactModelLabel(model: string): string {
  if (!model.startsWith('Used: ')) return model;
  const identifier = model.slice('Used: '.length);
  const slash = identifier.indexOf('/');
  if (slash < 0) return `Used: ${identifier}`;
  const variantAt = identifier.lastIndexOf(' / ');
  const modelEnd = variantAt > slash ? variantAt : identifier.length;
  const provider = identifier.slice(0, slash);
  const modelName = identifier.slice(slash + 1, modelEnd);
  const variant = variantAt > slash ? identifier.slice(variantAt) : '';
  void provider;
  return `Used: ${modelName}${variant}`;
}

function fitModelLabel(model: string, width: number): string {
  if (cellWidth(model) <= width) return model;
  if (!model.startsWith('Used: ')) return fit(model, width);
  const identifier = model.slice('Used: '.length);
  const variantAt = identifier.lastIndexOf(' / ');
  if (variantAt < 0) return fit(model, width);
  const variant = identifier.slice(variantAt + 3);
  const fixed = `Used: … / ${variant}`;
  const modelRoom = width - cellWidth(fixed) + 1;
  if (modelRoom <= 0) return fit(model, width);
  const name = identifier.slice(0, variantAt);
  return `Used: ${fit(name, modelRoom)} / ${variant}`;
}
