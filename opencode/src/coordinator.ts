import { randomUUID } from 'node:crypto'
import type { ActionMatchResult, ActionMatcher } from './action-matcher.ts'
import type { Capabilities } from './capabilities.ts'
import type { ContextBlock, ContextRequest, ContextResult, Origin } from './protocol.ts'
import type { WorkerPool } from './worker-pool.ts'

const DEFAULT_BUDGET_MS = 1_500
const DEFAULT_TTL_MS = 30 * 60_000
const DEFAULT_MAX_IDLE_SESSIONS = 128
const DEFAULT_BLOCK_LIMIT = 5
const DEFAULT_SNAPSHOT_CHARS = 4_000
const DEFAULT_BOOTSTRAP_CHARS = 8_000
const SECRET = /\b(?:authorization|bearer)\b|\b(?:[A-Za-z0-9]+[_-])*(?:api[_-]?key|token|password|passwd|secret|credential|client[_-]?secret)\b|:\/\/[^:/\s]+:[^@/\s]+@|-----BEGIN [^-]*PRIVATE KEY-----/i
const SHORT_ACK = /^(?:ok(?:ay)?|yes|yep|sure|thanks|thank you|got it|continue|do it|proceed)[.!\s]*$/i

export interface CoordinatorClock {
  now(): number
  waitUntil(deadline: number): Promise<void>
}

export interface SessionLocation { cwd: string; boundary?: string; scope?: string }

export interface CoordinatorSettings extends SessionLocation {
  prepareBudgetMs?: number
  ttlMs?: number
  maxIdleSessions?: number
  blockLimit?: number
  snapshotChars?: number
  bootstrapChars?: number
  maxRetainedBlocks?: number
  maxPendingPerSession?: number
  maxObservedActions?: number
  locationForSession?: (sessionId: string) => SessionLocation
}

export type CapabilityProvider = (sessionId: string, deadline: number) => Promise<Capabilities>

export interface ObservedAction { tool: string; query?: string; shell?: 'bash' | 'powershell'; command?: string }

export interface Coordinator {
  begin(sessionId: string, text: string, origin: Origin): number
  getGeneration(sessionId: string): number
  observeAction(sessionId: string, action: ObservedAction): void
  prepare(sessionId: string): Promise<string>
  setCapabilities(sessionId: string, capabilities: Capabilities): void
  refreshBootstrap(sessionId: string): void
  drop(sessionId: string): void
  dispose(): Promise<void>
  getDiagnostics(sessionId: string): string[]
}

interface PendingJob {
  key: string
  generation: number
  revision: number
  kind: 'recall' | 'routes' | 'action'
  controller: AbortController
  promise: Promise<void>
}

interface SessionState {
  id: string
  generation: number
  location: SessionLocation
  capabilities: Capabilities
  blocks: Map<string, ContextBlock>
  blockExpires: Map<string, number>
  pending: Map<string, PendingJob>
  observedActions: Map<string, RetainedAction>
  activePrepares: number
  prepareSequence: number
  capabilitySequence: number
  latestCapabilityObservation: number
  bootstrapPromise?: Promise<void>
  bootstrapController?: AbortController
  routeDescriptors: NonNullable<ContextResult['actionDescriptors']>
  snapshot: string
  snapshotGeneration: number
  snapshotExpires: number
  lastActivity: number
  diagnostics: string[]
}

const emptyCapabilities = (): Capabilities => ({ tools: [], mcp: [], skills: [], revision: 0 })

interface NormalizedObservedAction {
  tool: string
  shell: 'bash' | 'powershell'
  command: string
  attemptedGeneration?: number
  attemptedRevision?: number
  retryAfterPrepare?: number
}
interface RetainedAction { action: NormalizedObservedAction; expiresAt: number }

function safeQuery(source: string): string {
  const query = source.replace(/-----BEGIN [^-]*PRIVATE KEY-----[\s\S]*?(?:-----END [^-]*PRIVATE KEY-----|$)/gi, '[private key omitted]')
    .split(/\r?\n/).map((line) => SECRET.test(line) ? '[sensitive content omitted]' : line).join('\n')
    .replace(/\s+/g, ' ').trim().slice(0, 4_000)
  return query
}

function normalizeKey(source: string): string { return source.toLocaleLowerCase().replace(/\s+/g, ' ').trim() }

function sameCapabilities(a: Capabilities, b: Capabilities): boolean {
  return a.revision === b.revision && a.platform === b.platform
    && ['tools', 'mcp', 'skills'].every((key) => [...a[key as keyof Capabilities] as string[]].sort().join('\0') === [...b[key as keyof Capabilities] as string[]].sort().join('\0'))
}

export function formatSnapshot(bootstrap: Iterable<ContextBlock>, blocks: Iterable<ContextBlock>, limits: Pick<CoordinatorSettings, 'blockLimit' | 'snapshotChars' | 'bootstrapChars'> = {}): string {
  const bootstrapCap = limits.bootstrapChars ?? DEFAULT_BOOTSTRAP_CHARS
  const totalCap = limits.snapshotChars ?? DEFAULT_SNAPSHOT_CHARS
  const blockLimit = limits.blockLimit ?? DEFAULT_BLOCK_LIMIT
  const structural = (block: ContextBlock, cap: number): string => {
    const heading = `### ${block.category === 'action_rule' ? 'Action guidance' : block.category === 'route' ? 'Available tool guidance' : block.category === 'bootstrap' ? 'Project context' : 'Relevant experience'} (${block.id})`
    const text = block.text.trim()
    if (heading.length + text.length + 2 <= cap) return `${heading}\n${text}`
    if (block.category === 'bootstrap') {
      const paragraphs = text.split(/\n\s*\n/).map((paragraph) => paragraph.trim()).filter(Boolean)
      const nextIndex = paragraphs.findIndex((paragraph) => /^(?:#{1,6}\s*)?NEXT\s+STEP\s*:/i.test(paragraph))
      if (nextIndex >= 0) {
        const bodyCap = Math.max(0, cap - heading.length - 1)
        const nextStep = paragraphs[nextIndex]!
        let body: string
        if (nextStep.length >= bodyCap) body = nextStep.slice(0, bodyCap).replace(/\s+\S*$/, '')
        else {
          const earlierBudget = Math.max(0, bodyCap - nextStep.length - 2)
          const earlier = paragraphs.slice(0, nextIndex).join('\n\n')
          const marker = ' [earlier handoff compacted]'
          const earlierText = earlier.length <= earlierBudget ? earlier : earlierBudget > marker.length
            ? `${earlier.slice(0, earlierBudget - marker.length).replace(/\s+\S*$/, '')}${marker}` : ''
          body = [earlierText, nextStep].filter(Boolean).join('\n\n')
        }
        return `${heading}\n${body}`
      }
    }
    const available = Math.max(0, cap - heading.length - 24)
    if (available < 80) return `${heading}\n[Context omitted: exceeds snapshot budget]`
    const paragraphs = text.split(/\n\s*\n/)
    let compact = ''
    for (const paragraph of paragraphs) {
      const next = compact ? `${compact}\n\n${paragraph}` : paragraph
      if (next.length > available) break
      compact = next
    }
    if (!compact) compact = `${text.slice(0, available).replace(/\s+\S*$/, '')} [summary compacted]`
    return `${heading}\n${compact}`
  }
  const boot = [...bootstrap].sort((a, b) => b.priority - a.priority || a.id.localeCompare(b.id))
  const chosen = [...blocks].sort((a, b) => b.priority - a.priority || a.id.localeCompare(b.id)).slice(0, blockLimit)
  const bootstrapSections: string[] = []
  let remainingBoot = bootstrapCap
  for (const block of boot) {
    if (remainingBoot <= 0) break
    const section = structural(block, remainingBoot)
    if (section.length > remainingBoot) continue
    bootstrapSections.push(section)
    remainingBoot -= section.length + 2
  }
  const hintSections: string[] = []
  let total = 0
  for (const block of chosen) {
    const available = totalCap - total - (hintSections.length ? 2 : 0)
    if (available <= 0) break
    const section = structural(block, available)
    if (section.length > available) continue
    hintSections.push(section)
    total += section.length + 2
  }
  return [...bootstrapSections, ...hintSections].join('\n\n')
}

export function createCoordinator(
  settings: CoordinatorSettings,
  pool: WorkerPool,
  clock: CoordinatorClock,
  capabilityProvider: CapabilityProvider,
  actionMatcher: ActionMatcher,
): Coordinator {
  const sessions = new Map<string, SessionState>()
  let disposed = false
  let disposePromise: Promise<void> | undefined
  const prepareBudget = Number.isSafeInteger(settings.prepareBudgetMs) && settings.prepareBudgetMs! > 0 ? settings.prepareBudgetMs! : DEFAULT_BUDGET_MS
  const ttl = Number.isSafeInteger(settings.ttlMs) && settings.ttlMs! > 0 ? settings.ttlMs! : DEFAULT_TTL_MS
  const maxIdle = Number.isSafeInteger(settings.maxIdleSessions) && settings.maxIdleSessions! > 0 ? settings.maxIdleSessions! : DEFAULT_MAX_IDLE_SESSIONS
  const maxRetainedBlocks = Number.isSafeInteger(settings.maxRetainedBlocks) && settings.maxRetainedBlocks! > 0 ? settings.maxRetainedBlocks! : 64
  const maxPendingPerSession = Number.isSafeInteger(settings.maxPendingPerSession) && settings.maxPendingPerSession! > 0 ? settings.maxPendingPerSession! : 32
  const maxObservedActions = Number.isSafeInteger(settings.maxObservedActions) && settings.maxObservedActions! > 0 ? settings.maxObservedActions! : 32
  const maxMatcherQueue = Math.max(32, maxIdle * maxPendingPerSession)
  let matcherQueueDepth = 0
  let matcherTail: Promise<void> = Promise.resolve()

  function locationFor(sessionId: string): SessionLocation {
    return settings.locationForSession?.(sessionId) ?? { cwd: settings.cwd, ...(settings.boundary ? { boundary: settings.boundary } : {}), ...(settings.scope ? { scope: settings.scope } : {}) }
  }

  function stateFor(sessionId: string): SessionState {
    let state = sessions.get(sessionId)
    const location = locationFor(sessionId)
    if (!state) {
      state = { id: sessionId, generation: 0, location, capabilities: emptyCapabilities(), blocks: new Map(), blockExpires: new Map(), pending: new Map(), observedActions: new Map(), activePrepares: 0, prepareSequence: 0, capabilitySequence: 0, latestCapabilityObservation: 0, routeDescriptors: [], snapshot: '', snapshotGeneration: -1, snapshotExpires: 0, lastActivity: clock.now(), diagnostics: [] }
      sessions.set(sessionId, state)
    } else if (state.location.scope !== location.scope || state.location.cwd !== location.cwd || state.location.boundary !== location.boundary) {
      state.generation += 1
      state.location = location
      state.blocks.clear()
      state.blockExpires.clear()
      state.observedActions.clear()
      for (const job of state.pending.values()) job.controller.abort()
      state.pending.clear()
      state.bootstrapController?.abort()
      state.bootstrapPromise = undefined
      state.routeDescriptors = []
      state.snapshot = ''
      state.snapshotGeneration = -1
      scheduleRoutes(state)
    }
    state.lastActivity = clock.now()
    evictIdle(state.id)
    return state
  }

  function evictIdle(protectId?: string): void {
    const idle = [...sessions.values()].filter((state) => state.id !== protectId && state.activePrepares === 0 && state.pending.size === 0 && !state.bootstrapPromise)
      .sort((a, b) => a.lastActivity - b.lastActivity)
    while (idle.length > maxIdle) {
      const state = idle.shift()!
      sessions.delete(state.id)
    }
  }

  function diagnostic(state: SessionState, item: string): void {
    if (!state.diagnostics.includes(item)) state.diagnostics.push(item)
    if (state.diagnostics.length > 32) state.diagnostics.shift()
  }

  function makeRequest(state: SessionState, operation: ContextRequest['operation'], origin: Origin, generation = state.generation, query?: string): ContextRequest {
    return { version: 1, requestId: randomUUID(), sessionId: state.id, generation, operation,
      cwd: state.location.cwd, ...(state.location.boundary ? { boundary: state.location.boundary } : {}), ...(state.location.scope ? { scope: state.location.scope } : {}),
      ...(query ? { query } : {}), origin, capabilityRevision: state.capabilities.revision,
      capabilities: { tools: [...state.capabilities.tools], mcp: [...state.capabilities.mcp], skills: [...state.capabilities.skills] } }
  }

  function valid(state: SessionState, generation: number, revision: number, kind: PendingJob['kind']): boolean {
    return !disposed && sessions.get(state.id) === state && state.generation === generation
      && (kind === 'recall' || state.capabilities.revision === revision)
  }

  function storeBlock(state: SessionState, block: ContextBlock): void {
    const old = state.blocks.get(block.id)
    if (old && old.text === block.text && old.priority >= block.priority && old.category === block.category) return
    if (old && old.priority > block.priority && old.text !== block.text) return
    state.blocks.set(block.id, block)
    state.blockExpires.set(block.id, clock.now() + ttl)
    while (state.blocks.size > maxRetainedBlocks) {
      const oldestLowest = [...state.blocks.values()].sort((a, b) => a.priority - b.priority || a.id.localeCompare(b.id))[0]
      if (!oldestLowest) break
      state.blocks.delete(oldestLowest.id)
      state.blockExpires.delete(oldestLowest.id)
      diagnostic(state, 'retained_block_overflow')
    }
    state.snapshot = ''
    state.snapshotGeneration = -1
  }

  function accept(state: SessionState, frame: ContextResult, generation: number, revision: number, kind: PendingJob['kind']): void {
    if (!valid(state, generation, revision, kind) || frame.sessionId !== state.id || frame.generation !== generation) return
    if (kind !== 'recall' && frame.capabilityRevision !== state.capabilities.revision) return
    for (const block of frame.blocks) {
      if ((block.category === 'route' || block.category === 'action_rule') && block.capabilityRevision !== state.capabilities.revision) continue
      storeBlock(state, block)
    }
    if (frame.status === 'degraded') for (const item of frame.diagnostics) diagnostic(state, item)
    else if (kind === 'recall' && frame.blocks.length === 0) diagnostic(state, 'recall_zero_hits')
  }

  function track(state: SessionState, key: string, generation: number, revision: number, kind: PendingJob['kind'], task: (signal: AbortSignal) => Promise<void>): void {
    if (state.pending.has(key)) return
    if (state.pending.size >= maxPendingPerSession) {
      const victim = kind === 'routes' ? [...state.pending.values()].find((job) => job.kind === 'action') ?? [...state.pending.values()].find((job) => job.kind === 'recall') : undefined
      if (!victim) { diagnostic(state, 'pending_overflow'); return }
      victim.controller.abort()
      state.pending.delete(victim.key)
      diagnostic(state, 'pending_overflow')
    }
    const controller = new AbortController()
    const promise = Promise.resolve().then(() => controller.signal.aborted ? undefined : task(controller.signal)).catch(() => { if (valid(state, generation, revision, kind)) diagnostic(state, `${kind}_failed`) }).finally(() => {
      const current = state.pending.get(key)
      if (current?.promise === promise) state.pending.delete(key)
      state.lastActivity = clock.now()
      if (valid(state, generation, revision, kind)) rematchObservedActions(state)
      evictIdle()
    })
    state.pending.set(key, { key, generation, revision, kind, controller, promise })
  }

  function serializedMatch(action: NormalizedObservedAction, descriptors: NonNullable<ContextResult['actionDescriptors']>, revision: number, isCurrent: () => boolean): Promise<ActionMatchResult> {
    if (matcherQueueDepth >= maxMatcherQueue) return Promise.reject(new Error('matcher_queue_full'))
    matcherQueueDepth += 1
    const match = matcherTail.then(() => isCurrent()
      ? actionMatcher.matchDetailed(action.command, action.shell, descriptors, revision)
      : ({ status: 'degraded', blocks: [] } satisfies ActionMatchResult))
    matcherTail = match.then(() => undefined, () => undefined).finally(() => { matcherQueueDepth -= 1 })
    return match
  }

  function scheduleActionMatch(state: SessionState, action: NormalizedObservedAction): void {
    const generation = state.generation
    const revision = state.capabilities.revision
    if (action.attemptedGeneration === generation && action.attemptedRevision === revision) return
    const key = `action\0${generation}\0${revision}\0${normalizeKey(action.tool + ' ' + action.command)}`
    track(state, key, generation, revision, 'action', async (signal) => {
      const routeJob = [...state.pending.values()].find((job) => job.kind === 'routes' && job.generation === generation && job.revision === revision)
      if (routeJob) await routeJob.promise
      if (!valid(state, generation, revision, 'action')) return
      const isCurrent = () => !signal.aborted && valid(state, generation, revision, 'action')
      try {
        const outcome = await serializedMatch(action, state.routeDescriptors, revision, isCurrent)
        if (!isCurrent()) return
        if (outcome.status === 'busy') {
          action.attemptedGeneration = generation
          action.attemptedRevision = revision
          action.retryAfterPrepare = state.prepareSequence + 1
          diagnostic(state, 'action_match_busy')
          return
        }
        if (outcome.status === 'degraded') diagnostic(state, 'action_match_degraded')
        else if (outcome.blocks.length === 0) diagnostic(state, state.routeDescriptors.length === 0 ? 'action_descriptors_empty' : 'action_match_no_hit')
        action.attemptedGeneration = generation
        action.attemptedRevision = revision
        action.retryAfterPrepare = undefined
        for (const block of outcome.blocks) if (block.capabilityRevision === revision) storeBlock(state, block)
      } catch (error) {
        if (isCurrent()) {
          action.attemptedGeneration = generation
          action.attemptedRevision = revision
          action.retryAfterPrepare = undefined
          diagnostic(state, error instanceof Error && error.message === 'matcher_queue_full' ? 'matcher_queue_overflow' : 'action_failed')
        }
        throw error
      }
    })
  }

  function rematchObservedActions(state: SessionState): void {
    for (const [key, retained] of state.observedActions) {
      if (retained.expiresAt <= clock.now()) { state.observedActions.delete(key); continue }
      if (retained.action.attemptedGeneration === state.generation && retained.action.attemptedRevision === state.capabilities.revision) {
        if (retained.action.retryAfterPrepare === undefined || state.prepareSequence < retained.action.retryAfterPrepare) continue
        retained.action.attemptedGeneration = undefined
        retained.action.attemptedRevision = undefined
        retained.action.retryAfterPrepare = undefined
      }
      scheduleActionMatch(state, retained.action)
    }
  }

  function scheduleRecall(state: SessionState, querySource: string, origin: Origin): void {
    const query = safeQuery(querySource)
    if (!query) return
    const generation = state.generation
    const revision = state.capabilities.revision
    const key = `recall\0${generation}\0${origin}\0${revision}\0${normalizeKey(query)}`
    track(state, key, generation, revision, 'recall', async (signal) => {
      const req = makeRequest(state, 'recall', origin, generation, query)
      const frame = await pool.request(req, signal)
      accept(state, frame, generation, revision, 'recall')
    })
  }

  function scheduleRoutes(state: SessionState): void {
    const generation = state.generation
    const revision = state.capabilities.revision
    const key = `routes\0${generation}\0${revision}`
    track(state, key, generation, revision, 'routes', async (signal) => {
      const req = makeRequest(state, 'routes', 'resume', generation)
      const frame = await pool.request(req, signal)
      if (!valid(state, generation, revision, 'routes') || frame.capabilityRevision !== revision) return
      state.routeDescriptors = frame.actionDescriptors ?? []
      accept(state, frame, generation, revision, 'routes')
    })
  }

  function refreshCaps(state: SessionState, deadline: number): Promise<void> {
    const generation = state.generation
    const observation = ++state.capabilitySequence
    state.latestCapabilityObservation = observation
    return Promise.resolve().then(() => capabilityProvider(state.id, deadline)).then((next) => {
      if (disposed || sessions.get(state.id) !== state || state.generation !== generation || state.latestCapabilityObservation !== observation) return
      if (!Number.isSafeInteger(next.revision) || next.revision < state.capabilities.revision) return
      if (!sameCapabilities(state.capabilities, next)) applyCaps(state, next)
    }).catch(() => {
      if (!disposed && sessions.get(state.id) === state && state.generation === generation && state.latestCapabilityObservation === observation) diagnostic(state, 'capabilities_unavailable')
    })
  }

  function applyCaps(state: SessionState, capabilities: Capabilities): void {
    if (sameCapabilities(state.capabilities, capabilities)) return
    state.capabilities = { tools: [...capabilities.tools], mcp: [...capabilities.mcp], skills: [...capabilities.skills], revision: capabilities.revision, ...(capabilities.platform ? { platform: capabilities.platform } : {}) }
    state.snapshot = ''
    state.snapshotGeneration = -1
    for (const [key, job] of state.pending) if (job.kind === 'routes' || job.kind === 'action') { job.controller.abort(); state.pending.delete(key) }
    state.routeDescriptors = []
    for (const [id, block] of state.blocks) if ((block.category === 'route' || block.category === 'action_rule') && block.capabilityRevision !== capabilities.revision) { state.blocks.delete(id); state.blockExpires.delete(id) }
    scheduleRoutes(state)
  }

  function setCaps(state: SessionState, capabilities: Capabilities): void {
    if (!Number.isSafeInteger(capabilities.revision) || capabilities.revision < state.capabilities.revision) return
    state.latestCapabilityObservation = ++state.capabilitySequence
    applyCaps(state, capabilities)
  }

  function scheduleBootstrap(state: SessionState): void {
    const generation = state.generation
    const revision = state.capabilities.revision
    if (state.bootstrapPromise) return
    const req = makeRequest(state, 'bootstrap', 'resume', generation)
    const bootstrapController = new AbortController()
    state.bootstrapController = bootstrapController
    const bootstrapPromise = pool.request(req, bootstrapController.signal).then((frame) => {
      if (disposed || sessions.get(state.id) !== state || state.generation !== generation || frame.sessionId !== state.id || frame.generation !== generation) return
      for (const block of frame.blocks) if (block.category === 'bootstrap') {
        storeBlock(state, block)
      }
    }).catch(() => { if (valid(state, generation, revision, 'routes')) diagnostic(state, 'bootstrap_failed') }).finally(() => { if (state.bootstrapPromise === bootstrapPromise) { state.bootstrapPromise = undefined; state.bootstrapController = undefined } })
    state.bootstrapPromise = bootstrapPromise
  }

  function expire(state: SessionState): void {
    let expired = false
    for (const [id, expires] of state.blockExpires) if (expires <= clock.now()) {
      state.blockExpires.delete(id)
      state.blocks.delete(id)
      expired = true
    }
    if (expired) {
      state.snapshot = ''
      state.snapshotGeneration = -1
      diagnostic(state, 'snapshot_expired')
    }
  }

  async function raceDeadline<T>(promise: Promise<T>, deadline: number, deadlinePromise?: Promise<void>): Promise<{ hit: true; value: T } | { hit: false }> {
    if (clock.now() >= deadline) return { hit: false }
    let timeout: Promise<void>
    try { timeout = deadlinePromise ?? clock.waitUntil(deadline) } catch { timeout = Promise.resolve() }
    const winner = await Promise.race([promise.then((value) => ({ hit: true as const, value })), timeout.then(() => ({ hit: false as const }))])
    return winner
  }

  async function waitForPending(state: SessionState, generation: number, deadline: number, deadlinePromise: Promise<void>): Promise<boolean> {
    while (clock.now() < deadline) {
      const pending = [...state.pending.values()].filter((job) => job.generation === generation).map((job) => job.promise)
      if (!pending.length) return true
      const result = await raceDeadline(Promise.all(pending).then(() => undefined), deadline, deadlinePromise)
      if (!result.hit) return false
    }
    return ![...state.pending.values()].some((job) => job.generation === generation)
  }

  return {
    begin(sessionId, text, origin) {
      const state = stateFor(sessionId)
      const generation = state.generation + (origin === 'user' || origin === 'task' ? 1 : 0)
      if (generation !== state.generation) {
        state.generation = generation
        for (const job of state.pending.values()) job.controller.abort()
        state.pending.clear()
        state.bootstrapController?.abort()
        state.bootstrapPromise = undefined
        const shortAck = SHORT_ACK.test(text.trim())
        if (!shortAck) {
          for (const [id, block] of state.blocks) if (block.category !== 'bootstrap') { state.blocks.delete(id); state.blockExpires.delete(id) }
          state.observedActions.clear()
          state.snapshot = ''
          state.snapshotGeneration = -1
          scheduleRecall(state, text, origin)
          scheduleRoutes(state)
        }
      }
      return state.generation
    },
    getGeneration(sessionId) { return sessions.get(sessionId)?.generation ?? 0 },
    observeAction(sessionId, action) {
      const state = stateFor(sessionId)
      const query = typeof action.query === 'string' ? safeQuery(action.query) : ''
      if (query) scheduleRecall(state, query, 'action')
      if (!action.command || !action.shell) return
      const command = safeQuery(action.command)
      if (!command) return
      const normalized: NormalizedObservedAction = { tool: action.tool.slice(0, 100), shell: action.shell, command }
      const key = normalizeKey(`${normalized.tool}\0${normalized.shell}\0${normalized.command}`)
      state.observedActions.delete(key)
      state.observedActions.set(key, { action: normalized, expiresAt: clock.now() + ttl })
      while (state.observedActions.size > maxObservedActions) {
        state.observedActions.delete(state.observedActions.keys().next().value!)
        diagnostic(state, 'observed_action_overflow')
      }
      scheduleActionMatch(state, normalized)
    },
    async prepare(sessionId) {
      const state = stateFor(sessionId)
      state.activePrepares += 1
      state.prepareSequence += 1
      try {
        expire(state)
        const generation = state.generation
        const deadline = clock.now() + prepareBudget
        const waitUntilDeadline = clock.waitUntil(deadline)
        const caps = refreshCaps(state, deadline)
        const capWait = await raceDeadline(caps, deadline, waitUntilDeadline)
        if (!capWait.hit) diagnostic(state, 'prepare_timeout')
        if (generation !== state.generation) return ''
        scheduleBootstrap(state)
        rematchObservedActions(state)
        const hasCurrentWork = [...state.pending.values()].some((job) => job.generation === generation)
        if (!hasCurrentWork && state.snapshot && state.snapshotExpires > clock.now() && state.snapshotGeneration === generation) return state.snapshot
        const allReady = await waitForPending(state, generation, deadline, waitUntilDeadline)
        if (!allReady) diagnostic(state, 'prepare_timeout')
        if (generation !== state.generation) return ''
        expire(state)
        for (const [id, block] of state.blocks) if ((block.category === 'route' || block.category === 'action_rule') && block.capabilityRevision !== state.capabilities.revision) { state.blocks.delete(id); state.blockExpires.delete(id) }
        if (state.bootstrapPromise && clock.now() < deadline) await raceDeadline(state.bootstrapPromise, deadline, waitUntilDeadline)
        if (generation !== state.generation) return ''
        const boot = [...state.blocks.values()].filter((block) => block.category === 'bootstrap')
        const rest = [...state.blocks.values()].filter((block) => block.category !== 'bootstrap')
        state.snapshot = formatSnapshot(boot, rest, settings)
        state.snapshotGeneration = generation
        state.snapshotExpires = Math.min(...state.blockExpires.values())
        return state.snapshot
      } finally {
        state.activePrepares = Math.max(0, state.activePrepares - 1)
        state.lastActivity = clock.now()
        evictIdle()
      }
    },
    setCapabilities(sessionId, capabilities) { setCaps(stateFor(sessionId), capabilities) },
    refreshBootstrap(sessionId) {
      const state = stateFor(sessionId)
      state.snapshot = ''
      state.snapshotGeneration = -1
      scheduleBootstrap(state)
    },
    drop(sessionId) {
      const state = sessions.get(sessionId)
      if (state) { for (const job of state.pending.values()) job.controller.abort(); state.bootstrapController?.abort(); state.pending.clear(); state.blocks.clear(); state.blockExpires.clear(); state.observedActions.clear(); state.snapshot = ''; sessions.delete(sessionId) }
    },
    async dispose() {
      if (disposePromise) return disposePromise
      disposed = true
      for (const state of sessions.values()) { for (const job of state.pending.values()) job.controller.abort(); state.bootstrapController?.abort(); state.blocks.clear(); state.blockExpires.clear(); state.observedActions.clear() }
      sessions.clear()
      disposePromise = Promise.allSettled([pool.dispose(), actionMatcher.dispose()]).then(() => undefined)
      return disposePromise
    },
    getDiagnostics(sessionId) { return [...(sessions.get(sessionId)?.diagnostics ?? [])] },
  }
}
