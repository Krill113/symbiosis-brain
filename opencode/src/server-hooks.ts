import type { Hooks } from '@opencode-ai/plugin'
import { capabilitiesFromSnapshot, type CapabilitySnapshot, type Capabilities } from './capabilities.ts'
import type { Coordinator, CoordinatorSettings, ObservedAction } from './coordinator.ts'
import { normalizeAction, type HostContext } from './normalize.ts'
import type { SaveCheckpoints, TodoItem } from './save-checkpoint.ts'

export interface RuntimeSessionConfig {
  python?: string
  vault?: string
  tools?: string
  scope?: string
}

export interface SessionSelection { agent?: string; model?: { providerID: string; modelID: string } }
export interface ResolvedSessionLocation { cwd: string; boundary?: string; scope?: string; stale?: boolean }

export interface ServerHookDependencies {
  coordinator: Coordinator
  checkpoints: SaveCheckpoints
  capabilities: (sessionId: string, selection?: SessionSelection) => Promise<CapabilitySnapshot | Capabilities>
  hostContext: (sessionId: string, cwd?: string) => HostContext
  runtime: (sessionId: string, cwd?: string) => RuntimeSessionConfig | Promise<RuntimeSessionConfig>
  isChildSession?: (sessionId: string) => Promise<boolean>
  resolveSessionLocation?: (sessionId: string, cwd?: string) => Promise<ResolvedSessionLocation>
  onSessionLocation?: (sessionId: string, cwd: string, boundary?: string, scope?: string) => void
  onSessionDrop?: (sessionId: string) => void
  onDiagnostic?: (code: string) => void
}

type AnyRecord = Record<string, unknown>
const SAVE_TOOLS = new Set([
  'symbiosis-brain_brain_write', 'symbiosis-brain_brain_append', 'symbiosis-brain_brain_patch',
  'symbiosis_brain_brain_write', 'symbiosis_brain_brain_append', 'symbiosis_brain_brain_patch',
])
const MAX_HOOK_SESSIONS = 256

function record(value: unknown): AnyRecord | undefined {
  return typeof value === 'object' && value !== null && !Array.isArray(value) ? value as AnyRecord : undefined
}

function textParts(parts: unknown): string {
  if (!Array.isArray(parts)) return ''
  return parts.flatMap((part) => {
    const value = record(part)
    return value?.type === 'text' && typeof value.text === 'string' ? [value.text] : []
  }).join('\n').trim()
}

function isSyntheticContinue(parts: unknown, body: string): boolean {
  if (!Array.isArray(parts) || !/^continue\b/i.test(body)) return false
  return parts.some((part) => {
    const value = record(part)
    const metadata = record(value?.metadata)
    return value?.synthetic === true || metadata?.compaction_continue === true
  })
}

function normalizeCapabilities(value: CapabilitySnapshot | Capabilities): Capabilities {
  const snapshot = value as CapabilitySnapshot
  if (snapshot.tools.every((item) => typeof item === 'string')
    && snapshot.mcp.every((item) => typeof item === 'string')
    && snapshot.skills.every((item) => typeof item === 'string')) return value as Capabilities
  return capabilitiesFromSnapshot(snapshot)
}

function usefulTodoList(value: unknown): TodoItem[] | undefined {
  return Array.isArray(value) ? value.filter((item): item is TodoItem => !!record(item)) : undefined
}

function shellEnvValue(output: { env: Record<string, string> }, key: string, value: string | undefined, ownership: Map<string, string>): void {
  if (!value) return
  const existing = output.env[key]
  const owned = ownership.get(key)
  if (existing === undefined || (owned !== undefined && existing === owned)) {
    output.env[key] = value
    ownership.set(key, value)
  }
}

export function createServerHooks(input: ServerHookDependencies, settings: CoordinatorSettings, coordinator: Coordinator = input.coordinator, capabilities: ServerHookDependencies['capabilities'] = input.capabilities, checkpoints: SaveCheckpoints = input.checkpoints): Hooks {
  const sessions = new Map<string, { cwd?: string; boundary?: string; scope?: string; staleLocation: boolean; child: boolean; childKnown: boolean; agent?: string; model?: { providerID: string; modelID: string }; seenMessages: Set<string>; seenCalls: Set<string> }>()
  const bootstrapped = new Set<string>()
  let envOwnership = new WeakMap<Record<string, string>, Map<string, string>>()
  const diagnostics = new Set<string>()

  const stateFor = (sessionId: string) => {
    let state = sessions.get(sessionId)
    if (!state) {
      state = { staleLocation: false, child: false, childKnown: false, seenMessages: new Set(), seenCalls: new Set() }
    }
    sessions.delete(sessionId)
    sessions.set(sessionId, state)
    while (sessions.size > MAX_HOOK_SESSIONS) sessions.delete(sessions.keys().next().value!)
    return state
  }

  const safe = async (operation: () => void | Promise<void>): Promise<void> => {
    try { await operation() }
    catch {
      if (!diagnostics.has('hook_callback_failed')) {
        diagnostics.add('hook_callback_failed')
        try { input.onDiagnostic?.('hook_callback_failed') } catch { /* diagnostics never affect host behavior */ }
      }
    }
  }

  const reportSessionDiagnostics = (sessionId: string): void => {
    for (const code of coordinator.getDiagnostics(sessionId)) {
      try { input.onDiagnostic?.(code) } catch { /* diagnostics never affect host behavior */ }
    }
  }

  const ensureSessionLocation = async (sessionId: string, cwd?: string) => {
    const state = stateFor(sessionId)
    if (cwd && cwd !== state.cwd) {
      state.cwd = cwd
      state.boundary = undefined
      state.scope = undefined
      state.staleLocation = false
      input.onSessionLocation?.(sessionId, cwd)
    }
    if (!input.resolveSessionLocation) {
      return state
    }
    const resolved = await input.resolveSessionLocation(sessionId, cwd)
    if (resolved.stale) { state.staleLocation = true; return state }
    state.staleLocation = false
    state.cwd = resolved.cwd
    state.boundary = resolved.boundary
    state.scope = resolved.scope
    input.onSessionLocation?.(sessionId, resolved.cwd, resolved.boundary, resolved.scope)
    return state
  }

  const hooks: Hooks = {
    'experimental.chat.system.transform': async (hookInput, output) => safe(async () => {
      if (!hookInput.sessionID) return
      await ensureSessionLocation(hookInput.sessionID)
      const block = await coordinator.prepare(hookInput.sessionID)
      if (block && !output.system.includes(block)) output.system.push(block)
      reportSessionDiagnostics(hookInput.sessionID)
    }),

    'chat.message': async (hookInput, output) => safe(async () => {
      const sessionId = hookInput.sessionID
      if (!sessionId) return
      const state = await ensureSessionLocation(sessionId)
      if (state.staleLocation) return
      if (!state.childKnown && input.isChildSession) {
        try { state.child = await input.isChildSession(sessionId) }
        catch {
          state.child = false
          try { input.onDiagnostic?.('session_metadata_unavailable') } catch { /* diagnostics never affect host behavior */ }
        }
        state.childKnown = true
      }
      const message = record(output.message)
      const messageId = typeof message?.id === 'string' ? message.id : hookInput.messageID
      if (messageId && state.seenMessages.has(messageId)) return
      const body = textParts(output.parts)
      if (!body) return
      if (isSyntheticContinue(output.parts, body)) return
      if (messageId) {
        state.seenMessages.add(messageId)
        if (state.seenMessages.size > 256) state.seenMessages.delete(state.seenMessages.values().next().value!)
      }
      state.agent = hookInput.agent
      state.model = hookInput.model
      const generation = coordinator.begin(sessionId, body, state.child ? 'task' : 'user')
      checkpoints.onActivity(sessionId, generation)
      void capabilities(sessionId, { agent: state.agent, model: state.model }).then((next) => {
        coordinator.setCapabilities(sessionId, normalizeCapabilities(next))
      }).catch(() => {
        try { input.onDiagnostic?.('capability_discovery_failed') } catch { /* diagnostics never affect the host */ }
      })
    }),

    'command.execute.before': async (hookInput) => safe(() => {
      const text = `/${hookInput.command} ${hookInput.arguments ?? ''}`.trim()
      const generation = coordinator.begin(hookInput.sessionID, text, 'user')
      checkpoints.onActivity(hookInput.sessionID, generation)
    }),

    'tool.execute.before': async (hookInput, output) => safe(async () => {
      const sessionId = hookInput.sessionID
      const state = stateFor(sessionId)
      if (state.seenCalls.has(hookInput.callID)) return
      state.seenCalls.add(hookInput.callID)
      if (state.seenCalls.size > 512) state.seenCalls.delete(state.seenCalls.values().next().value!)
      const args = output.args
      await ensureSessionLocation(sessionId, state.cwd)
      if (state.staleLocation) return
      const host = input.hostContext(sessionId, state.cwd)
      const action = normalizeAction(hookInput.tool, args, host)
      let generation = coordinator.getGeneration(sessionId)
      if (action) {
        const observation: ObservedAction = { tool: action.tool, query: action.query,
          ...(action.shell ? { shell: action.shell } : {}), ...(action.command ? { command: action.command } : {}) }
        coordinator.observeAction(sessionId, observation)
        checkpoints.onActivity(sessionId, generation)
      }
      if (SAVE_TOOLS.has(hookInput.tool.toLocaleLowerCase())) {
        const path = record(args)?.path
        checkpoints.onBrainStart(sessionId, hookInput.callID, hookInput.tool, generation, typeof path === 'string' ? path : '')
      }
    }),

    'tool.execute.after': async (hookInput, output) => safe(() => {
      const sessionId = hookInput.sessionID
      const tool = hookInput.tool.toLocaleLowerCase()
      if (SAVE_TOOLS.has(tool)) checkpoints.onBrainResult(sessionId, hookInput.callID, tool, output)
      if (tool === 'todowrite' || tool === 'todo_write') {
        const args = record(hookInput.args)
        const todos = usefulTodoList(args?.todos ?? hookInput.args)
        if (todos) checkpoints.onTodo(sessionId, todos)
      }
      checkpoints.appendPending(sessionId, tool, output)
    }),

    'shell.env': async (hookInput, output) => safe(async () => {
      const sessionId = hookInput.sessionID ?? `cwd:${hookInput.cwd}`
      const state = await ensureSessionLocation(sessionId, hookInput.cwd)
      if (state.staleLocation) return
      const resolved = await input.runtime(sessionId, hookInput.cwd)
      let owned = envOwnership.get(output.env)
      if (!owned) { owned = new Map(); envOwnership.set(output.env, owned) }
      shellEnvValue(output, 'SYMBIOSIS_BRAIN_OPENCODE_PYTHON', resolved.python, owned)
      shellEnvValue(output, 'SYMBIOSIS_BRAIN_OPENCODE_VAULT', resolved.vault, owned)
      shellEnvValue(output, 'SYMBIOSIS_BRAIN_OPENCODE_TOOLS', resolved.tools, owned)
      shellEnvValue(output, 'SYMBIOSIS_BRAIN_VAULT', resolved.vault, owned)
      shellEnvValue(output, 'SYMBIOSIS_BRAIN_TOOLS', resolved.tools, owned)
      shellEnvValue(output, 'SYMBIOSIS_BRAIN_SCOPE', resolved.scope, owned)
    }),

    event: async ({ event }) => safe(async () => {
      const ev = record(event)
      const properties = record(ev?.properties)
      const info = record(properties?.info)
      const eventType = typeof ev?.type === 'string' ? ev.type : ''
      if ((eventType === 'session.created' || eventType === 'session.updated') && typeof info?.id === 'string') {
        const sessionId = info.id
        const state = stateFor(sessionId)
        if (typeof info.directory === 'string' && info.directory !== state.cwd) {
          state.cwd = info.directory
          state.boundary = undefined
          state.scope = undefined
        }
        input.onSessionLocation?.(sessionId, state.cwd ?? settings.cwd, state.boundary, state.scope)
        state.child = typeof info.parentID === 'string' && !!info.parentID
        state.childKnown = true
        if (!bootstrapped.has(sessionId)) {
          bootstrapped.add(sessionId)
          coordinator.refreshBootstrap(sessionId)
          coordinator.setCapabilities(sessionId, normalizeCapabilities(await capabilities(sessionId)))
        }
        while (bootstrapped.size > MAX_HOOK_SESSIONS) bootstrapped.delete(bootstrapped.values().next().value!)
      }
      if (eventType === 'session.compacted' && typeof properties?.sessionID === 'string') coordinator.refreshBootstrap(properties.sessionID)
      if ((eventType === 'todo.updated' || eventType === 'session.todo.updated') && typeof properties?.sessionID === 'string') {
        const todos = usefulTodoList(properties.todos)
        if (todos) checkpoints.onTodo(properties.sessionID, todos)
      }
      const deletedSessionId = typeof properties?.sessionID === 'string' ? properties.sessionID : typeof info?.id === 'string' ? info.id : undefined
      if ((eventType === 'session.deleted' || eventType === 'session.removed') && deletedSessionId) {
        coordinator.drop(deletedSessionId)
        checkpoints.drop(deletedSessionId)
        sessions.delete(deletedSessionId)
        bootstrapped.delete(deletedSessionId)
        input.onSessionDrop?.(deletedSessionId)
      }
      const sid = typeof properties?.sessionID === 'string' ? properties.sessionID : undefined
      if (sid && (eventType === 'mcp.connected' || eventType === 'mcp.disconnected' || eventType === 'permission.updated' || eventType === 'config.updated')) {
        const state = stateFor(sid)
        coordinator.setCapabilities(sid, normalizeCapabilities(await capabilities(sid, { agent: state.agent, model: state.model })))
      }
    }),

    'experimental.session.compacting': async (hookInput, output) => safe(async () => {
      const context = await coordinator.prepare(hookInput.sessionID)
      if (context && !output.context.includes(context)) output.context.push(context)
    }),

    dispose: async () => safe(async () => {
      sessions.clear()
      bootstrapped.clear()
      envOwnership = new WeakMap()
      checkpoints.clear()
      await coordinator.dispose()
    }),
  }

  // Keep the settings reference explicit: per-session location is supplied by
  // the caller's normalized host context, never inferred from tool names.
  void settings
  return hooks
}
