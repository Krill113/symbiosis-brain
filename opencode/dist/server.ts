import type { Plugin, PluginInput, PluginOptions } from '@opencode-ai/plugin'
import { randomUUID } from 'node:crypto'
import { isAbsolute, relative, resolve } from 'node:path'
import { createActionMatcher, type ActionMatcher, type ActionMatchResult } from './src/action-matcher.ts'
import { createCoordinator, type CoordinatorClock, type CoordinatorSettings } from './src/coordinator.ts'
import { createServerHooks, type ResolvedSessionLocation, type SessionSelection } from './src/server-hooks.ts'
import { createSaveCheckpoints } from './src/save-checkpoint.ts'
import { createWorkerPool, type WorkerPool } from './src/worker-pool.ts'
import type { BridgeResources, ContextRequest, ContextResult } from './src/protocol.ts'
import type { Capabilities } from './src/capabilities.ts'
import { detectActiveLegacyAdapter } from './install/legacy-discovery.ts'

const MODULE_ID = 'symbiosis-brain-opencode-server'
const MAX_SESSION_CONFIGS = 256

type RuntimeOptions = PluginOptions & { python?: string; pythonExe?: string; vault?: string; tools?: string; packageDir?: string; mcpServer?: string; shell?: string }
type ResolvedRuntime = { python?: string; vault?: string; tools?: string; shell: 'bash' | 'powershell' | null; mcpEnvironment: Record<string, string> }

function retainSessionValue<T>(map: Map<string, T>, sessionId: string, value: T): void {
  map.delete(sessionId)
  map.set(sessionId, value)
  while (map.size > MAX_SESSION_CONFIGS) map.delete(map.keys().next().value!)
}

function asRecord(value: unknown): Record<string, unknown> | undefined {
  return typeof value === 'object' && value !== null && !Array.isArray(value) ? value as Record<string, unknown> : undefined
}

function responseData(value: unknown): unknown {
  const response = asRecord(value)
  return response && 'data' in response ? response.data : value
}

function interpolateEnvironment(value: unknown): string | undefined {
  if (typeof value !== 'string' || !value.trim()) return undefined
  return value.replace(/\$\{([A-Za-z_][A-Za-z0-9_]*)\}/g, (_match, name: string) => process.env[name] ?? '')
}

function knownShell(value: unknown): 'bash' | 'powershell' | null {
  if (typeof value !== 'string' || !value.trim()) return null
  const name = value.trim().replaceAll('\\', '/').split('/').at(-1)!.toLocaleLowerCase()
  if (name === 'bash' || name === 'bash.exe') return 'bash'
  if (['pwsh', 'pwsh.exe', 'powershell', 'powershell.exe', 'pwsh-utf8', 'pwsh-utf8.exe'].includes(name)) return 'powershell'
  return null
}

function globMatch(pattern: string, value: string): boolean {
  const escaped = pattern.replace(/[.+?^${}()|[\]\\]/g, '\\$&').replaceAll('*', '.*')
  return new RegExp(`^${escaped}$`, 'i').test(value)
}

function enabledAgentTools(agent: Record<string, unknown> | undefined, allTools: string[]): string[] {
  if (!agent) return []
  const configuredTools = agent.tools
  const enabledMap = asRecord(configuredTools)
  const enabledList = Array.isArray(configuredTools) ? new Set(configuredTools.filter((item): item is string => typeof item === 'string')) : undefined
  const permissionValue = agent.permission
  const rules = Array.isArray(permissionValue) ? permissionValue.flatMap((item) => {
    const rule = asRecord(item)
    return rule && typeof rule.permission === 'string' && typeof rule.action === 'string' ? [rule] : []
  }) : []
  const permissionMap = asRecord(permissionValue)
  return allTools.filter((tool) => {
    const configured = enabledList ? enabledList.has(tool) : typeof enabledMap?.[tool] === 'boolean' ? enabledMap[tool] as boolean : undefined
    if (configured === false) return false
    const matching = rules.filter((rule) => {
      const permission = rule.permission as string
      return permission === '*' || permission === tool || globMatch(permission, tool)
    }).filter((rule) => rule.pattern === '*' || rule.pattern === tool)
    const rule = matching.at(-1)
    if (rule) return rule.action === 'allow'
    if (permissionMap) {
      const direct = permissionMap[tool]
      if (direct === 'deny' || direct === 'ask') return false
      if (direct === 'allow') return true
      if (tool === 'bash') {
        const bash = asRecord(permissionMap.bash)
        if (bash?.['*'] === 'deny' || bash?.['*'] === 'ask') return false
        if (bash?.['*'] === 'allow') return true
      }
    }
    return configured === true || enabledList?.has(tool) === true
  })
}

function toolBelongsToMcp(tool: string, server: string): boolean {
  const normalizedTool = tool.toLocaleLowerCase()
  const normalizedServer = server.toLocaleLowerCase()
  return normalizedTool.startsWith(`${normalizedServer}_`) || normalizedTool.startsWith(`mcp__${normalizedServer}__`)
}

function configuredBrainMcp(config: Record<string, unknown>, requestedName?: string): { environment: Record<string, string>; command: string[] } {
  const servers = asRecord(config.mcp)
  if (!servers) return { environment: {}, command: [] }
  const matches = Object.entries(servers).filter(([name, value]) => {
    if (requestedName) return name === requestedName
    const server = asRecord(value)
    const command = Array.isArray(server?.command) ? server.command : []
    return /symbiosis[-_]brain/i.test(name) || command.some((part) => typeof part === 'string' && /symbiosis_brain\.server/i.test(part))
  })
  if (matches.length !== 1) return { environment: {}, command: [] }
  const server = asRecord(matches[0]![1])
  if (server?.enabled === false) return { environment: {}, command: [] }
  const environment = asRecord(server?.environment)
  return {
    environment: Object.fromEntries(Object.entries(environment ?? {}).flatMap(([key, value]) => {
      const resolved = interpolateEnvironment(value)
      return resolved === undefined ? [] : [[key, resolved]]
    })),
    command: Array.isArray(server?.command) ? server.command.filter((part): part is string => typeof part === 'string') : [],
  }
}

async function resolveRuntime(input: PluginInput, options: RuntimeOptions, cwd: string, diagnose: (code: string) => void): Promise<ResolvedRuntime> {
  let config: Record<string, unknown> = {}
  const explicitRuntime = !!(options.python || options.pythonExe) && !!options.vault
    && !!(options.tools || options.packageDir) && !!options.shell
  if (!explicitRuntime) {
    let timer: ReturnType<typeof setTimeout> | undefined
    try {
      const response = await Promise.race([
        input.client.config.get({ directory: cwd, query: { directory: cwd } } as never),
        new Promise<undefined>((resolve) => { timer = setTimeout(() => resolve(undefined), 1_000); timer.unref?.() }),
      ])
      if (response === undefined) diagnose('runtime_config_unavailable')
      else config = asRecord(responseData(response)) ?? {}
    } catch { diagnose('runtime_config_unavailable') }
    finally { if (timer) clearTimeout(timer) }
  }
  const mcp = configuredBrainMcp(config, typeof options.mcpServer === 'string' ? options.mcpServer : undefined)
  const configuredPython = mcp.command[0] && /(?:^|[\\/])python(?:\d+(?:\.\d+)?)?(?:\.exe)?$/i.test(mcp.command[0]) ? mcp.command[0] : undefined
  const python = interpolateEnvironment(options.python ?? options.pythonExe)
    ?? process.env.SYMBIOSIS_BRAIN_OPENCODE_PYTHON ?? mcp.environment.SYMBIOSIS_BRAIN_OPENCODE_PYTHON ?? configuredPython
  const vault = interpolateEnvironment(options.vault) ?? process.env.SYMBIOSIS_BRAIN_VAULT ?? mcp.environment.SYMBIOSIS_BRAIN_VAULT
  const tools = interpolateEnvironment(options.tools ?? options.packageDir)
    ?? process.env.SYMBIOSIS_BRAIN_TOOLS ?? mcp.environment.SYMBIOSIS_BRAIN_TOOLS
  return { python, vault, tools, shell: knownShell(options.shell ?? config.shell), mcpEnvironment: mcp.environment }
}

const clock: CoordinatorClock = {
  now: () => Date.now(),
  waitUntil: (deadline) => new Promise<void>((resolveWait) => {
    const timer = setTimeout(resolveWait, Math.max(0, deadline - Date.now()))
    timer.unref?.()
  }),
}

function unavailablePool(): WorkerPool {
  return {
    async request(request: ContextRequest): Promise<ContextResult> {
      return { version: 1, kind: 'result', requestId: request.requestId, sessionId: request.sessionId,
        generation: request.generation, capabilityRevision: request.capabilityRevision, status: 'degraded',
        scope: request.scope ?? null, blocks: [], diagnostics: ['adapter-runtime-unavailable'] }
    },
    async dispose() {},
  }
}

function unavailableMatcher(): ActionMatcher {
  return {
    async match() { return [] },
    async matchDetailed(): Promise<ActionMatchResult> { return { status: 'degraded', blocks: [] } },
    async dispose() {},
    diagnostics: ['adapter-runtime-unavailable'],
  }
}

export function createServerPlugin(factories: {
  workerPool?: typeof createWorkerPool
  actionMatcher?: typeof createActionMatcher
} = {}): Plugin {
  return async (input: PluginInput, rawOptions?: PluginOptions) => {
  const options = (rawOptions ?? {}) as RuntimeOptions
  const cwd = resolve(input.directory)
  const boundary = resolve(input.worktree || input.directory)
  const diagnostics = new Set<string>()
  const diagnose = (code: string) => {
    if (diagnostics.has(code)) return
    diagnostics.add(code)
    try { console.warn(`[symbiosis-brain] ${code}`) } catch { /* diagnostics never affect adapter behavior */ }
  }
  const legacy = await detectActiveLegacyAdapter(cwd)
  if (legacy.active) diagnose(legacy.diagnostics[0] ?? 'legacy-adapter-active-in-scope')
  const runtime = await resolveRuntime(input, options, cwd, diagnose)
  const { python, vault, tools } = runtime
  const runtimeReady = !!(python && vault) && !legacy.active
  if (!runtimeReady) diagnose('adapter_runtime_unavailable')
  const createPool = factories.workerPool ?? createWorkerPool
  const createMatcher = factories.actionMatcher ?? createActionMatcher
  const pool = runtimeReady ? createPool({ pythonExe: python!, packageDir: tools ?? cwd, vault: vault!, cwd,
    env: runtime.mcpEnvironment }) : unavailablePool()
  let actionMatcher: ActionMatcher = unavailableMatcher()
  let actionMatcherPromise: Promise<ActionMatcher> | undefined
  let bridgeResourcesPromise: Promise<{ resources: BridgeResources; scope?: string } | undefined> | undefined
  if (runtimeReady) {
    const initialRequest: ContextRequest = {
      version: 1, requestId: `resources-${randomUUID()}`, sessionId: 'adapter-resources', generation: 0,
      operation: 'resources', cwd, boundary, origin: 'resume', capabilityRevision: 0,
      capabilities: { tools: [], mcp: [], skills: [] },
    }
    bridgeResourcesPromise = pool.request(initialRequest).then((result) => {
      if (result.status !== 'ok' || !result.resources) { diagnose('adapter_resources_unavailable'); return undefined }
      return { resources: result.resources, ...(result.scope ? { scope: result.scope } : {}) }
    }).catch(() => { diagnose('adapter_resources_unavailable'); return undefined })
    actionMatcherPromise = bridgeResourcesPromise.then((resolved) => resolved ? createMatcher(resolved.resources) : unavailableMatcher())
    // The coordinator receives a stable proxy, so early action calls wait for
    // resource discovery instead of creating a second matcher or guessing paths.
    actionMatcher = {
      async match(command, shell, descriptors, revision) {
        return (await actionMatcherPromise!).match(command, shell, descriptors, revision)
      },
      async matchDetailed(command, shell, descriptors, revision) {
        return (await actionMatcherPromise!).matchDetailed(command, shell, descriptors, revision)
      },
      async dispose() { await (await actionMatcherPromise)?.dispose() },
      get diagnostics() { return actionMatcherPromise ? [] : ['adapter-runtime-unavailable'] },
    }
  }

  const initialBridgeContext = await bridgeResourcesPromise
  const sessionLocations = new Map<string, { cwd: string; boundary?: string; scope?: string }>()
  const locationForSession = (sessionId: string) => sessionLocations.get(sessionId)
    ?? { cwd, boundary, ...(initialBridgeContext?.scope ? { scope: initialBridgeContext.scope } : {}) }
  const settings: CoordinatorSettings = { cwd, boundary, ...(initialBridgeContext?.scope ? { scope: initialBridgeContext.scope } : {}), locationForSession }
  const sessionSelections = new Map<string, SessionSelection>()
  const capabilityFingerprints = new Map<string, { fingerprint: string; revision: number }>()
  const capabilities = async (sessionId: string, selection?: SessionSelection): Promise<Capabilities> => {
    const active = selection ?? sessionSelections.get(sessionId)
    if (!active?.agent) return { tools: [], mcp: [], skills: [], revision: capabilityFingerprints.get(sessionId)?.revision ?? 0 }
    // The pinned SDK and the installed 1.19 host SDK use different generated
    // parameter envelopes; carrying both documented forms keeps the calls
    // scoped to this project on either version.
    const projectQuery = { directory: cwd, query: { directory: cwd } } as never
    const [agentsResponse, mcpResponse, toolsResponse] = await Promise.all([
      input.client.app.agents(projectQuery),
      input.client.mcp.status(projectQuery),
      input.client.tool.ids(projectQuery),
    ])
    const unwrap = (value: unknown): unknown => {
      const wrapper = value as { data?: unknown } | null
      return wrapper && typeof wrapper === 'object' && 'data' in wrapper ? wrapper.data : value
    }
    const agents = unwrap(agentsResponse)
    const mcpState = unwrap(mcpResponse)
    const toolIds = unwrap(toolsResponse)
    const agent = Array.isArray(agents) ? agents.find((candidate) => (candidate as { name?: string }).name === active.agent) as Record<string, unknown> | undefined : undefined
    const allTools = Array.isArray(toolIds) ? toolIds.filter((item): item is string => typeof item === 'string') : []
    const enabledTools = enabledAgentTools(agent, allTools)
    const mcp = mcpState && typeof mcpState === 'object' ? Object.entries(mcpState as Record<string, { status?: string }>)
      .filter(([name, status]) => status?.status === 'connected'
        && enabledTools.some((tool) => toolBelongsToMcp(tool, name)))
      .map(([name]) => name).sort() : []
    const fingerprint = JSON.stringify({ enabledTools, mcp, model: active.model ?? null })
    const previous = capabilityFingerprints.get(sessionId)
    const revision = previous ? previous.revision + (previous.fingerprint === fingerprint ? 0 : 1) : 1
    retainSessionValue(capabilityFingerprints, sessionId, { fingerprint, revision })
    return { tools: enabledTools, mcp, skills: [], revision, ...(active.model ? { platform: `${active.model.providerID}/${active.model.modelID}` } : {}) }
  }
  const capabilityProvider = async (sessionId: string, hint?: SessionSelection | number): Promise<Capabilities> => {
    const selection = typeof hint === 'number' ? undefined : hint
    if (selection) retainSessionValue(sessionSelections, sessionId, selection)
    return capabilities(sessionId, selection ?? sessionSelections.get(sessionId))
  }
  const coordinator = createCoordinator(settings, pool, clock, capabilityProvider, actionMatcher)
  const checkpoints = createSaveCheckpoints()
  const resolvedScopes = new Map<string, { generation: number; cwd: string; boundary?: string; scope?: string }>()
  const scopeForSession = async (sessionId: string, sessionCwd: string, sessionBoundary?: string): Promise<string | undefined> => {
    const generation = coordinator.getGeneration(sessionId)
    const cached = resolvedScopes.get(sessionId)
    if (cached?.generation === generation && cached.cwd === sessionCwd && cached.boundary === sessionBoundary) return cached.scope
    if (!runtimeReady) { diagnose('runtime_scope_unavailable'); return undefined }
    const response = await pool.request({ version: 1, requestId: `scope-${randomUUID()}`, sessionId, generation,
      operation: 'bootstrap', cwd: sessionCwd, ...(sessionBoundary ? { boundary: sessionBoundary } : {}),
      origin: 'resume', capabilityRevision: 0, capabilities: { tools: [], mcp: [], skills: [] } })
    if (response.status !== 'ok') { diagnose('runtime_scope_unavailable'); return undefined }
    const current = sessionLocations.get(sessionId)
    if (coordinator.getGeneration(sessionId) !== generation
      || (current && (current.cwd !== sessionCwd || current.boundary !== sessionBoundary))) {
      diagnose('runtime_scope_stale')
      return undefined
    }
    const scope = response.scope ?? undefined
    retainSessionValue(resolvedScopes, sessionId, { generation, cwd: sessionCwd, ...(sessionBoundary ? { boundary: sessionBoundary } : {}), ...(scope ? { scope } : {}) })
    return scope
  }
  return createServerHooks({
    coordinator,
    checkpoints,
    capabilities: capabilityProvider,
    hostContext(sessionId, sessionCwd) {
      return { shell: runtime.shell, cwd: sessionCwd ?? cwd, boundary: sessionLocations.get(sessionId)?.boundary ?? boundary }
    },
    async runtime(sessionId, sessionCwd) {
      const currentCwd = resolve(sessionCwd ?? cwd)
      const within = relative(boundary, currentCwd)
      const currentBoundary = within === '' || (!within.startsWith('..') && !isAbsolute(within)) ? boundary : undefined
      const [resolvedResources, scope] = await Promise.all([
        bridgeResourcesPromise,
        scopeForSession(sessionId, currentCwd, currentBoundary),
      ])
      const packageDir = resolvedResources?.resources.packageDir
      if (runtimeReady && !packageDir) diagnose('adapter_resources_unavailable')
      retainSessionValue(sessionLocations, sessionId, { cwd: currentCwd, ...(currentBoundary ? { boundary: currentBoundary } : {}), ...(scope ? { scope } : {}) })
      return { python, vault, tools: packageDir ?? tools ?? cwd, ...(scope ? { scope } : {}) }
    },
    async resolveSessionLocation(sessionId, requestedCwd): Promise<ResolvedSessionLocation> {
      let sessionCwd = requestedCwd
      if (!sessionCwd) {
        try {
          const response = await input.client.session.get({ id: sessionId, path: { id: sessionId }, directory: cwd, query: { directory: cwd } } as never)
          const session = asRecord(responseData(response))
          if (typeof session?.directory === 'string') sessionCwd = session.directory
        } catch { diagnose('session_location_unavailable') }
      }
      const resolvedCwd = resolve(sessionCwd ?? cwd)
      const within = relative(boundary, resolvedCwd)
      const resolvedBoundary = within === '' || (!within.startsWith('..') && !isAbsolute(within)) ? boundary : undefined
      const knownLocation = sessionLocations.get(sessionId)
      if (knownLocation && (knownLocation.cwd !== resolvedCwd || knownLocation.boundary !== resolvedBoundary)) {
        resolvedScopes.delete(sessionId)
        // A location discovered lazily from the SDK is a transition, not an
        // authorization to carry the previous project's scope forward.
        retainSessionValue(sessionLocations, sessionId, { cwd: resolvedCwd, ...(resolvedBoundary ? { boundary: resolvedBoundary } : {}) })
      }
      const generation = coordinator.getGeneration(sessionId)
      const scope = await scopeForSession(sessionId, resolvedCwd, resolvedBoundary)
      const current = sessionLocations.get(sessionId)
      if (coordinator.getGeneration(sessionId) !== generation
        || (current && (current.cwd !== resolvedCwd || current.boundary !== resolvedBoundary))) {
        diagnose('runtime_scope_stale')
        return { cwd: current?.cwd ?? resolvedCwd, ...(current?.boundary ? { boundary: current.boundary } : {}), stale: true }
      }
      return { cwd: resolvedCwd, ...(resolvedBoundary ? { boundary: resolvedBoundary } : {}), ...(scope ? { scope } : {}) }
    },
    async isChildSession(sessionId) {
      const result = await input.client.session.get({ id: sessionId, path: { id: sessionId }, directory: cwd, query: { directory: cwd } } as never)
      const session = asRecord(responseData(result))
      return typeof session?.parentID === 'string' && session.parentID.length > 0
    },
    onSessionLocation(sessionId, currentCwd, currentBoundary, currentScope) {
      const resolvedCwd = resolve(currentCwd)
      const rootRelative = relative(boundary, resolvedCwd)
      const withinRoot = rootRelative === '' || (!rootRelative.startsWith('..') && !isAbsolute(rootRelative))
      const resolvedBoundary = currentBoundary ? resolve(currentBoundary) : withinRoot ? boundary : undefined
      const previous = sessionLocations.get(sessionId)
      if (previous && (previous.cwd !== resolvedCwd || previous.boundary !== resolvedBoundary)) resolvedScopes.delete(sessionId)
      const scope = currentScope ?? (resolvedCwd === cwd && resolvedBoundary === boundary ? initialBridgeContext?.scope : undefined)
      retainSessionValue(sessionLocations, sessionId, { cwd: resolvedCwd, ...(resolvedBoundary ? { boundary: resolvedBoundary } : {}), ...(scope ? { scope } : {}) })
      const generation = coordinator.getGeneration(sessionId)
      if (scope) retainSessionValue(resolvedScopes, sessionId, { generation, cwd: resolvedCwd, ...(resolvedBoundary ? { boundary: resolvedBoundary } : {}), scope })
    },
    onSessionDrop(sessionId) { sessionLocations.delete(sessionId); sessionSelections.delete(sessionId); capabilityFingerprints.delete(sessionId); resolvedScopes.delete(sessionId) },
    onDiagnostic: diagnose,
  }, settings, coordinator, capabilityProvider, checkpoints)
  }
}

const server = createServerPlugin()
export const plugin = { id: MODULE_ID, server } satisfies { id: string; server: Plugin }
export default plugin
