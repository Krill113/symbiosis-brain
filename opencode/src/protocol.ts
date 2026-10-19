export type Operation = 'bootstrap' | 'recall' | 'routes' | 'resources' | 'warmup';
export type Origin = 'user' | 'task' | 'action' | 'resume';

export interface ContextRequest {
  version: 1;
  requestId: string;
  sessionId: string;
  generation: number;
  operation: Operation;
  cwd: string;
  boundary?: string;
  scope?: string;
  query?: string;
  origin: Origin;
  capabilityRevision: number;
  action?: { tool: string; shell?: 'bash' | 'powershell'; command?: string };
  capabilities: { tools: string[]; mcp: string[]; skills: string[] };
}

export interface ContextBlock {
  id: string;
  text: string;
  priority: number;
  category: 'bootstrap' | 'memory' | 'route' | 'action_rule';
  capabilityRevision?: number;
}

export interface BridgeResources {
  pythonExe: string;
  packageDir: string;
  skillsDir: string;
  grepExe: string | null;
  actionRulesFile: string;
  mcpCommand: string[];
}

export interface WorkerHello {
  version: 1;
  kind: 'hello';
  ready: true;
  role: 'control' | 'retrieval';
}

export interface ActionDescriptor {
  id: string;
  shell: 'bash' | 'powershell';
  pattern: string;
  hint: string;
  priority: number;
  capabilityRevision: number;
}

export interface ContextResult {
  version: 1;
  kind: 'result';
  requestId: string;
  sessionId: string;
  generation: number;
  capabilityRevision: number;
  status: 'ok' | 'degraded';
  scope: string | null;
  blocks: ContextBlock[];
  diagnostics: string[];
  resources?: BridgeResources;
  actionDescriptors?: ActionDescriptor[];
}

export type WorkerFrame = WorkerHello | ContextResult;

export const MAX_FRAME_BYTES = 64 * 1024;

export class ProtocolError extends Error {
  readonly reason: string;

  constructor(reason: string) {
    super(reason);
    this.name = 'ProtocolError';
    this.reason = reason;
  }
}

function record(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function nonempty(value: unknown, field: string, optional = false): string | undefined {
  if (optional && value === undefined) return undefined;
  if (typeof value !== 'string' || value.trim().length === 0) throw new ProtocolError(`invalid-${field}`);
  return value;
}

function integer(value: unknown, field: string): number {
  if (typeof value !== 'number' || !Number.isSafeInteger(value) || value < 0) throw new ProtocolError(`invalid-${field}`);
  return value;
}

function withinBoundary(cwd: string, boundary: string): boolean {
  // Lexical path checks are intentionally conservative and work for both POSIX
  // and Windows wire paths without resolving through the receiver's filesystem.
  const normalize = (path: string) => {
    const absolute = path.replaceAll('\\', '/');
    const prefix = absolute.startsWith('/') ? '/' : '';
    const parts: string[] = [];
    for (const part of absolute.split('/')) {
      if (!part || part === '.') continue;
      if (part === '..') {
        if (parts.length === 0) return null;
        parts.pop();
      } else parts.push(part);
    }
    return `${prefix}${parts.join('/')}`.replace(/\/$/, '') || '/';
  };
  const child = normalize(cwd);
  const parent = normalize(boundary);
  if (child === null || parent === null) return false;
  const windowsPath = /^[a-z]:\//i.test(child) || child.startsWith('//');
  const c = windowsPath ? child.toLowerCase() : child;
  const p = windowsPath ? parent.toLowerCase() : parent;
  return c === p || c.startsWith(`${p.replace(/\/$/, '')}/`);
}

export function parseRequest(value: unknown, expectedSessionId?: string): ContextRequest {
  if (!record(value)) throw new ProtocolError('invalid-request');
  if (value.version !== 1) throw new ProtocolError('unsupported-version');
  const requestId = nonempty(value.requestId, 'request-id')!;
  const sessionId = nonempty(value.sessionId, 'session-id')!;
  if (expectedSessionId !== undefined && sessionId !== expectedSessionId) throw new ProtocolError('session-mismatch');
  if (typeof value.operation !== 'string' || !['bootstrap', 'recall', 'routes', 'resources', 'warmup'].includes(value.operation)) throw new ProtocolError('invalid-operation');
  if (typeof value.origin !== 'string' || !['user', 'task', 'action', 'resume'].includes(value.origin)) throw new ProtocolError('invalid-origin');
  const cwd = nonempty(value.cwd, 'cwd')!;
  const boundary = nonempty(value.boundary, 'boundary', true);
  if (boundary !== undefined && !withinBoundary(cwd, boundary)) throw new ProtocolError('cwd-outside-boundary');
  const scope = value.scope === undefined ? undefined : nonempty(value.scope, 'scope');
  if (boundary === undefined && scope !== undefined) throw new ProtocolError('scope-without-boundary');
  if (!record(value.capabilities)) throw new ProtocolError('invalid-capabilities');
  for (const key of ['tools', 'mcp', 'skills'] as const) {
    const list = value.capabilities[key];
    if (!Array.isArray(list) || list.some((entry) => typeof entry !== 'string')) throw new ProtocolError(`invalid-capability-${key}`);
  }
  let action: ContextRequest['action'];
  if (value.action !== undefined) {
    if (!record(value.action)) throw new ProtocolError('invalid-action');
    const tool = nonempty(value.action.tool, 'action-tool')!;
    const shell = value.action.shell;
    if (shell !== undefined && shell !== 'bash' && shell !== 'powershell') throw new ProtocolError('invalid-action-shell');
    if (value.action.command !== undefined && typeof value.action.command !== 'string') throw new ProtocolError('invalid-action-command');
    action = { tool, ...(shell === undefined ? {} : { shell }), ...(value.action.command === undefined ? {} : { command: value.action.command as string }) };
  }
  return {
    version: 1, requestId, sessionId,
    generation: integer(value.generation, 'generation'),
    operation: value.operation as Operation, cwd,
    ...(boundary === undefined ? {} : { boundary }),
    ...(scope === undefined ? {} : { scope }),
    ...(value.query === undefined ? {} : { query: nonempty(value.query, 'query')! }),
    origin: value.origin as Origin,
    capabilityRevision: integer(value.capabilityRevision, 'capability-revision'),
    ...(action === undefined ? {} : { action }),
    capabilities: {
      tools: value.capabilities.tools as string[],
      mcp: value.capabilities.mcp as string[],
      skills: value.capabilities.skills as string[],
    },
  };
}

function parseJsonFrame(raw: string | Uint8Array): unknown {
  const bytes = typeof raw === 'string' ? new TextEncoder().encode(raw) : raw;
  if (bytes.byteLength > MAX_FRAME_BYTES) throw new ProtocolError('frame-too-large');
  try {
    return JSON.parse(new TextDecoder('utf-8', { fatal: true }).decode(bytes)) as unknown;
  } catch {
    throw new ProtocolError('malformed-json');
  }
}

export function parseRequestFrame(raw: string | Uint8Array, expectedSessionId?: string): ContextRequest {
  return parseRequest(parseJsonFrame(raw), expectedSessionId);
}

function parseResult(value: Record<string, unknown>, expectedOperation?: Operation): ContextResult {
  if (value.version !== 1 || value.kind !== 'result' || !('scope' in value)) throw new ProtocolError('invalid-result');
  const requestId = nonempty(value.requestId, 'request-id')!;
  const sessionId = nonempty(value.sessionId, 'session-id')!;
  if (value.status !== 'ok' && value.status !== 'degraded') throw new ProtocolError('invalid-result');
  if (value.scope !== null && typeof value.scope !== 'string') throw new ProtocolError('invalid-result');
  if (!Array.isArray(value.blocks) || !Array.isArray(value.diagnostics) || value.diagnostics.some((d) => typeof d !== 'string')) throw new ProtocolError('invalid-result');
  const blocks = value.blocks.map((item): ContextBlock => {
    if (!record(item) || typeof item.category !== 'string' || !['bootstrap', 'memory', 'route', 'action_rule'].includes(item.category) || typeof item.text !== 'string') throw new ProtocolError('invalid-block');
    return {
      id: nonempty(item.id, 'block-id')!, text: item.text,
      priority: integer(item.priority, 'block-priority'),
      category: item.category as ContextBlock['category'],
      ...(item.capabilityRevision === undefined ? {} : { capabilityRevision: integer(item.capabilityRevision, 'capability-revision') }),
    };
  });
  let resources: BridgeResources | undefined;
  if (expectedOperation === 'resources' && value.status === 'ok' && !('resources' in value)) throw new ProtocolError('missing-resources');
  if (value.resources !== undefined) {
    if (!record(value.resources)) throw new ProtocolError('invalid-resources');
    const r = value.resources;
    if (!('grepExe' in r) || !Array.isArray(r.mcpCommand) || r.mcpCommand.some((x) => typeof x !== 'string')) throw new ProtocolError('invalid-resources');
    if (r.grepExe !== null && (typeof r.grepExe !== 'string' || r.grepExe.trim().length === 0)) throw new ProtocolError('invalid-grep-exe');
    resources = {
      pythonExe: nonempty(r.pythonExe, 'python-exe')!, packageDir: nonempty(r.packageDir, 'package-dir')!,
      skillsDir: nonempty(r.skillsDir, 'skills-dir')!, grepExe: r.grepExe,
      actionRulesFile: nonempty(r.actionRulesFile, 'action-rules-file')!, mcpCommand: r.mcpCommand as string[],
    };
  }
  let actionDescriptors: ActionDescriptor[] | undefined;
  if (value.actionDescriptors !== undefined) {
    if (!Array.isArray(value.actionDescriptors)) throw new ProtocolError('invalid-action-descriptors');
    actionDescriptors = value.actionDescriptors.map((item): ActionDescriptor => {
      try {
        if (!record(item) || typeof item.shell !== 'string' || (item.shell !== 'bash' && item.shell !== 'powershell')) throw new ProtocolError('invalid-action-descriptor');
        return {
          id: nonempty(item.id, 'action-descriptor-id')!,
          shell: item.shell,
          pattern: nonempty(item.pattern, 'action-descriptor-pattern')!,
          hint: nonempty(item.hint, 'action-descriptor-hint')!,
          priority: integer(item.priority, 'action-descriptor-priority'),
          capabilityRevision: integer(item.capabilityRevision, 'action-descriptor-capability-revision'),
        };
      } catch {
        throw new ProtocolError('invalid-action-descriptor');
      }
    });
  }
  return {
    version: 1, kind: 'result', requestId, sessionId,
    generation: integer(value.generation, 'generation'),
    capabilityRevision: integer(value.capabilityRevision, 'capability-revision'),
    status: value.status, scope: value.scope as string | null, blocks,
    diagnostics: value.diagnostics as string[],
    ...(resources === undefined ? {} : { resources }),
    ...(actionDescriptors === undefined ? {} : { actionDescriptors }),
  };
}

export function parseWorkerFrame(raw: string | Uint8Array | unknown, expectedOperation?: Operation): WorkerFrame {
  const value = typeof raw === 'string' || raw instanceof Uint8Array ? parseJsonFrame(raw) : raw;
  if (!record(value) || value.version !== 1) throw new ProtocolError('invalid-worker-frame');
  if (value.kind === 'hello') {
    if (value.ready !== true || (value.role !== 'control' && value.role !== 'retrieval')) throw new ProtocolError('invalid-hello');
    return { version: 1, kind: 'hello', ready: true, role: value.role };
  }
  if (value.kind === 'result') return parseResult(value, expectedOperation);
  throw new ProtocolError('invalid-worker-frame');
}
