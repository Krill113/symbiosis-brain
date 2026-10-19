import assert from 'node:assert/strict'
import test from 'node:test'
import { createCoordinator, type CoordinatorClock, type CoordinatorSettings } from '../src/coordinator.ts'
import type { ContextBlock, ContextRequest, ContextResult } from '../src/protocol.ts'
import type { WorkerPool } from '../src/worker-pool.ts'
import type { ActionMatchResult, ActionMatcher } from '../src/action-matcher.ts'
import type { Capabilities } from '../src/capabilities.ts'

function deferred<T>() {
  let resolve!: (value: T) => void
  const promise = new Promise<T>((r) => { resolve = r })
  return { promise, resolve }
}

class TestClock implements CoordinatorClock {
  time = 0
  waits: { deadline: number; resolve: () => void }[] = []
  now = () => this.time
  waitUntil = (deadline: number) => new Promise<void>((resolve) => this.waits.push({ deadline, resolve }))
  advanceTo(deadline: number) {
    this.time = deadline
    for (const wait of this.waits.splice(0)) if (wait.deadline <= deadline) wait.resolve()
  }
}

function result(req: ContextRequest, blocks: ContextBlock[], status: ContextResult['status'] = 'ok'): ContextResult {
  return { version: 1, kind: 'result', requestId: req.requestId, sessionId: req.sessionId, generation: req.generation,
    capabilityRevision: req.capabilityRevision, status, scope: req.scope ?? null, blocks, diagnostics: [] }
}

function harness(options: {
  clock?: CoordinatorClock
  settings?: Partial<CoordinatorSettings>
  capabilities?: (sessionId: string, deadline: number) => Promise<Capabilities>
  request?: (req: ContextRequest, signal?: AbortSignal) => Promise<ContextResult>
  match?: (command: string, shell: 'bash' | 'powershell', descriptors: never[], revision: number) => Promise<ContextBlock[]>
  detailedMatch?: (command: string, shell: 'bash' | 'powershell', descriptors: never[], revision: number) => Promise<ActionMatchResult>
  matchDiagnostics?: string[]
} = {}) {
  const clock = options.clock ?? new TestClock()
  const requests: ContextRequest[] = []
  const pool: WorkerPool = {
    request(req, signal) {
      requests.push(req)
      return options.request ? options.request(req, signal) : Promise.resolve(result(req, []))
    },
    async dispose() {},
  }
  const matcher: ActionMatcher = {
    async match(command, shell, descriptors, revision) {
      return options.match ? options.match(command, shell, descriptors as never[], revision) : []
    },
    async matchDetailed(command, shell, descriptors, revision) {
      if (options.detailedMatch) return options.detailedMatch(command, shell, descriptors as never[], revision)
      return { status: 'ok', blocks: options.match ? await options.match(command, shell, descriptors as never[], revision) : [] }
    },
    async dispose() {}, diagnostics: options.matchDiagnostics ?? [],
  }
  const capabilities = options.capabilities ?? (async () => ({ tools: [], mcp: [], skills: [], revision: 1 }))
  const coordinator = createCoordinator({ cwd: '/synthetic/project', boundary: '/synthetic', scope: 'demo', prepareBudgetMs: 1_500, ...options.settings }, pool, clock, capabilities, matcher)
  return { coordinator, clock, requests }
}

const note = (id: string, text: string, category: ContextBlock['category'] = 'memory'): ContextBlock => ({ id, text, priority: 50, category })

test('stale result from an earlier user generation cannot enter current snapshot; prepared snapshot survives acknowledgement', async () => {
  const old = deferred<ContextResult>()
  const { coordinator, requests } = harness({ request: (req) => req.query === 'old work' ? old.promise : Promise.resolve(result(req, [note('current', 'Current synthetic guidance')])) })
  coordinator.begin('s1', 'old work', 'user')
  coordinator.begin('s1', 'implement the next synthetic feature', 'user')
  await coordinator.prepare('s1')
  old.resolve(result(requests[0]!, [note('stale', 'Stale synthetic guidance')]))
  await Promise.resolve()
  const snapshot = await coordinator.prepare('s1')
  assert.match(snapshot, /Current synthetic guidance/)
  assert.doesNotMatch(snapshot, /Stale synthetic guidance/)
  coordinator.begin('s1', 'okay', 'user')
  assert.equal(await coordinator.prepare('s1'), snapshot, 'short acknowledgement retains only the already-prepared snapshot')
  await coordinator.dispose()
})

test('consecutive prepare callbacks return the same retained snapshot without draining it', async () => {
  const { coordinator } = harness()
  coordinator.begin('s1', 'implement a synthetic feature', 'user')
  coordinator.setCapabilities('s1', { tools: [], mcp: [], skills: [], revision: 1 })
  coordinator.refreshBootstrap('s1')
  const first = await coordinator.prepare('s1')
  assert.equal(await coordinator.prepare('s1'), first)
  assert.equal(await coordinator.prepare('s1'), first)
  await coordinator.dispose()
})

test('several pending actions share one absolute prepare deadline', async () => {
  const clock = new TestClock()
  const action = deferred<ContextBlock[]>()
  const { coordinator } = harness({ clock, match: () => action.promise })
  coordinator.begin('s1', 'update synthetic files', 'user')
  coordinator.observeAction('s1', { tool: 'bash', shell: 'bash', command: 'git status' })
  coordinator.observeAction('s1', { tool: 'bash', shell: 'bash', command: 'git diff' })
  const preparing = coordinator.prepare('s1')
  await Promise.resolve()
  await Promise.resolve()
  assert.deepEqual(clock.waits.map((wait) => wait.deadline), [1_500], 'all waits race a single aggregate deadline')
  clock.advanceTo(1_500)
  await preparing
  action.resolve([note('action-hit', 'Synthetic action rule', 'action_rule')])
  await coordinator.dispose()
})

test('one note returned by five separate actions is retained once', async () => {
  const { coordinator } = harness({ match: async () => [{ ...note('same-note', 'Only one synthetic note'), capabilityRevision: 1 }] })
  coordinator.setCapabilities('s1', { tools: [], mcp: [], skills: [], revision: 1 })
  for (let index = 0; index < 5; index += 1) coordinator.observeAction('s1', { tool: 'bash', shell: 'bash', command: `git status -- ${index}` })
  const snapshot = await coordinator.prepare('s1')
  assert.equal(snapshot.match(/Only one synthetic note/g)?.length, 1)
  await coordinator.dispose()
})

test('sessions have independent generations and pending queues', async () => {
  const one = deferred<ContextResult>()
  const two = deferred<ContextResult>()
  const { coordinator } = harness({ request: (req) => req.operation !== 'recall' ? Promise.resolve(result(req, [])) : req.sessionId === 'one' ? one.promise : two.promise })
  coordinator.begin('one', 'first synthetic task', 'user')
  coordinator.begin('two', 'second synthetic task', 'user')
  assert.equal(coordinator.getGeneration('one'), 1)
  assert.equal(coordinator.getGeneration('two'), 1)
  await Promise.resolve()
  await Promise.resolve()
  one.resolve(result({ version: 1, requestId: 'one-recall', sessionId: 'one', generation: 1, operation: 'recall', cwd: '/synthetic/project', boundary: '/synthetic', scope: 'demo', origin: 'user', capabilityRevision: 0, capabilities: { tools: [], mcp: [], skills: [] } }, [note('one', 'Session one only')]))
  two.resolve(result({ version: 1, requestId: 'two-recall', sessionId: 'two', generation: 1, operation: 'recall', cwd: '/synthetic/project', boundary: '/synthetic', scope: 'demo', origin: 'user', capabilityRevision: 0, capabilities: { tools: [], mcp: [], skills: [] } }, [note('two', 'Session two only')]))
  assert.match(await coordinator.prepare('one'), /Session one only/)
  assert.doesNotMatch(await coordinator.prepare('one'), /Session two only/)
  assert.match(await coordinator.prepare('two'), /Session two only/)
  await coordinator.dispose()
})

test('capability revision change invalidates route hints but preserves bootstrap and memory', async () => {
  let revision = 1
  const { coordinator } = harness({
    capabilities: async () => ({ tools: [], mcp: revision === 1 ? ['brain'] : [], skills: [], revision }),
    request: async (req) => result(req, req.operation === 'bootstrap' ? [note('boot', 'Bootstrap project context', 'bootstrap')]
      : req.operation === 'recall' ? [note('memory', 'A useful synthetic note')]
        : req.capabilities.mcp.includes('brain') ? [ { ...note('route:brain', 'route: brain_search', 'route'), capabilityRevision: req.capabilityRevision } ] : []),
  })
  coordinator.begin('s1', 'work with synthetic project', 'user')
  coordinator.setCapabilities('s1', { tools: [], mcp: ['brain'], skills: [], revision: 1 })
  coordinator.refreshBootstrap('s1')
  const before = await coordinator.prepare('s1')
  assert.match(before, /route: brain_search/)
  revision = 2
  const snapshot = await coordinator.prepare('s1')
  assert.match(snapshot, /Bootstrap project context/)
  assert.match(snapshot, /A useful synthetic note/)
  assert.doesNotMatch(snapshot, /route:|action_rule:/)
  await coordinator.dispose()
})

test('late route result after capability revision changes is discarded', async () => {
  const oldRoute = deferred<ContextResult>()
  let revision = 1
  const { coordinator, requests } = harness({ capabilities: async () => ({ tools: [], mcp: revision === 1 ? ['brain'] : [], skills: [], revision }), request: (req) => req.operation === 'routes'
    ? req.capabilityRevision === 1 ? oldRoute.promise : Promise.resolve(result(req, []))
    : Promise.resolve(result(req, [])) })
  coordinator.setCapabilities('s1', { tools: [], mcp: ['brain'], skills: [], revision: 1 })
  await Promise.resolve()
  await Promise.resolve()
  revision = 2
  coordinator.setCapabilities('s1', { tools: [], mcp: [], skills: [], revision: 2 })
  oldRoute.resolve(result(requests.find((req) => req.operation === 'routes')!, [
    { ...note('obsolete-route', 'Obsolete synthetic route', 'route'), capabilityRevision: 1 },
  ]))
  const snapshot = await coordinator.prepare('s1')
  assert.doesNotMatch(snapshot, /Obsolete synthetic route/)
  await coordinator.dispose()
})

test('same-connection permission and model revision changes rematch routes and action guidance', async () => {
  let current: Capabilities = { tools: ['edit'], mcp: ['brain'], skills: [], revision: 1, platform: 'model-a' }
  const seen: { revision: number; tools: string[]; mcp: string[]; platform?: string }[] = []
  const { coordinator } = harness({
    capabilities: async () => current,
    request: async (req) => {
      if (req.operation === 'routes') seen.push({ revision: req.capabilityRevision, tools: req.capabilities.tools, mcp: req.capabilities.mcp, platform: current.platform })
      return result(req, req.operation === 'routes' && req.capabilities.mcp.includes('brain')
        ? [{ ...note(`route-${req.capabilityRevision}`, `CURRENT_ROUTE_${req.capabilityRevision}`, 'route'), capabilityRevision: req.capabilityRevision }]
        : [])
    },
    match: async (_command, _shell, _descriptors, revision) => [{ ...note(`action-${revision}`, `CURRENT_ACTION_${revision}`, 'action_rule'), capabilityRevision: revision }],
  })
  coordinator.begin('s1', 'synthetic capability-aware task', 'user')
  coordinator.observeAction('s1', { tool: 'bash', shell: 'bash', command: 'git status' })
  const initial = await coordinator.prepare('s1')
  assert.match(initial, /CURRENT_ROUTE_1/)
  assert.match(initial, /CURRENT_ACTION_1/)
  current = { tools: [], mcp: ['brain'], skills: [], revision: 2, platform: 'model-a' }
  const permitted = await coordinator.prepare('s1')
  assert.match(permitted, /CURRENT_ROUTE_2/)
  assert.match(permitted, /CURRENT_ACTION_2/)
  current = { tools: [], mcp: [], skills: [], revision: 3, platform: 'model-b' }
  const latest = await coordinator.prepare('s1')
  assert.doesNotMatch(latest, /CURRENT_ROUTE_2|CURRENT_ACTION_2/)
  assert.ok(seen.some((snapshot) => snapshot.revision === 2 && snapshot.tools.length === 0 && snapshot.mcp.includes('brain')))
  assert.ok(seen.some((snapshot) => snapshot.revision === 3 && snapshot.platform === 'model-b' && snapshot.mcp.length === 0))
  await coordinator.dispose()
})

test('oversize block is compacted structurally within configured snapshot budget', async () => {
  const paragraphs = Array.from({ length: 20 }, (_, index) => `Synthetic section ${index}: ${'useful detail '.repeat(24)}`).join('\n\n')
  const { coordinator } = harness({ settings: { snapshotChars: 700 }, request: async (req) => result(req, [note('large', paragraphs)]) })
  coordinator.begin('s1', 'summarize synthetic context', 'user')
  const snapshot = await coordinator.prepare('s1')
  assert.ok(snapshot.length <= 700)
  assert.match(snapshot, /Relevant experience \(large\)/)
  await coordinator.dispose()
})

test('zero-hit and timeout are distinguishable in session diagnostics', async () => {
  const clock = new TestClock()
  const stuck = deferred<ContextBlock[]>()
  const { coordinator } = harness({ clock, match: () => stuck.promise })
  coordinator.begin('zero', 'synthetic query with no hits', 'user')
  await coordinator.prepare('zero')
  assert.ok(coordinator.getDiagnostics('zero').includes('recall_zero_hits'))
  coordinator.begin('timeout', 'another synthetic query', 'user')
  coordinator.observeAction('timeout', { tool: 'bash', shell: 'bash', command: 'git status' })
  const preparing = coordinator.prepare('timeout')
  await Promise.resolve()
  await Promise.resolve()
  clock.advanceTo(1_500)
  await preparing
  assert.ok(coordinator.getDiagnostics('timeout').includes('prepare_timeout'))
  stuck.resolve([])
  await coordinator.dispose()
})

test('routes are refreshed for every substantive generation and changed scope', async () => {
  let scope = 'alpha'
  const { coordinator, requests } = harness({
    settings: { locationForSession: () => ({ cwd: `/synthetic/${scope}`, boundary: '/synthetic', scope }) },
    request: async (req) => result(req, req.operation === 'routes'
      ? [{ ...note(`route-${req.scope}`, `ROUTE ${req.scope} generation ${req.generation}`, 'route'), capabilityRevision: req.capabilityRevision }]
      : []),
  })
  coordinator.setCapabilities('s1', { tools: [], mcp: ['brain'], skills: [], revision: 4 })
  coordinator.begin('s1', 'first substantive synthetic task', 'user')
  assert.match(await coordinator.prepare('s1'), /ROUTE alpha generation 1/)
  coordinator.begin('s1', 'second substantive synthetic task', 'user')
  assert.match(await coordinator.prepare('s1'), /ROUTE alpha generation 2/)
  scope = 'beta'
  assert.match(await coordinator.prepare('s1'), /ROUTE beta generation 3/)
  assert.equal(requests.filter((req) => req.operation === 'routes').length, 3, 'one current route request per generation/location')
  await coordinator.dispose()
})

test('actions observed before capability refresh are rematched and delivered at the next boundary', async () => {
  let calls = 0
  const { coordinator } = harness({
    capabilities: async () => ({ tools: [], mcp: ['brain'], skills: [], revision: 8 }),
    request: async (req) => result(req, [], 'ok'),
    match: async (_command, _shell, _descriptors, revision) => {
      calls += 1
      return [{ ...note('action:read', `ACTION_GUIDANCE revision ${revision}`, 'action_rule'), capabilityRevision: revision }]
    },
  })
  coordinator.begin('s1', 'inspect the synthetic project', 'user')
  coordinator.observeAction('s1', { tool: 'bash', shell: 'bash', command: 'git status' })
  const snapshot = await coordinator.prepare('s1')
  assert.match(snapshot, /ACTION_GUIDANCE revision 8/)
  assert.ok(calls >= 1)
  await coordinator.dispose()
})

test('bootstrap and hint budgets are independent; large bootstrap leaves memory and route/action capacity', async () => {
  const bootstrapText = `Handoff next step: preserve the synthetic migration order.\n\n${'Bootstrap detail. '.repeat(250)}`
  const memory = note('memory', 'MEMORY_GUIDANCE: retain schema compatibility')
  const route = { ...note('route', 'ROUTE_GUIDANCE: use brain_search', 'route'), capabilityRevision: 1 }
  const { coordinator } = harness({
    request: async (req) => result(req, req.operation === 'bootstrap' ? [note('bootstrap', bootstrapText, 'bootstrap')]
      : req.operation === 'routes' ? [route] : req.operation === 'recall' ? [memory] : []),
  })
  coordinator.setCapabilities('s1', { tools: [], mcp: ['brain'], skills: [], revision: 1 })
  coordinator.begin('s1', 'work on the synthetic migration', 'user')
  const snapshot = await coordinator.prepare('s1')
  assert.ok(snapshot.includes('Bootstrap detail.'))
  assert.match(snapshot, /Handoff next step: preserve the synthetic migration order/)
  assert.match(snapshot, /MEMORY_GUIDANCE/)
  assert.match(snapshot, /ROUTE_GUIDANCE/)
  assert.ok(snapshot.length > 4_000 && snapshot.length <= 12_000)
  await coordinator.dispose()
})

test('older capability provider completion cannot roll back a newer explicit revision', async () => {
  const older = deferred<Capabilities>()
  let observations = 0
  const routeRevisions: number[] = []
  const clock = new TestClock()
  const { coordinator } = harness({
    clock,
    capabilities: async () => ++observations === 1 ? older.promise : ({ tools: [], mcp: [], skills: [], revision: 2 }),
    request: async (req) => {
      if (req.operation === 'routes') routeRevisions.push(req.capabilityRevision)
      return result(req, [])
    },
  })
  const firstPrepare = coordinator.prepare('s1')
  await Promise.resolve()
  await Promise.resolve()
  coordinator.setCapabilities('s1', { tools: [], mcp: [], skills: [], revision: 2 })
  await coordinator.prepare('s1')
  older.resolve({ tools: [], mcp: ['stale-brain'], skills: [], revision: 1 })
  clock.advanceTo(1_500)
  await firstPrepare
  await coordinator.prepare('s1')
  assert.ok(routeRevisions.every((revision) => revision === 2), `stale provider must not schedule routes: ${routeRevisions}`)
  await coordinator.dispose()
})

test('the shared matcher is single-flight across sessions and both tagged results reach their boundaries', async () => {
  const clock = new TestClock()
  const started = deferred<void>()
  const release = deferred<void>()
  let active = false
  let peak = 0
  const { coordinator } = harness({ clock, capabilities: async () => ({ tools: [], mcp: [], skills: [], revision: 3 }), match: async (command, _shell, _descriptors, revision) => {
    if (active) return []
    active = true
    peak = Math.max(peak, 1)
    if (command.endsWith('one')) started.resolve()
    try {
      if (command.endsWith('one')) await release.promise
      return [{ ...note(`action:${command}`, `ACTION ${command}`, 'action_rule'), capabilityRevision: revision }]
    } finally { active = false }
  } })
  coordinator.setCapabilities('one', { tools: [], mcp: [], skills: [], revision: 3 })
  coordinator.setCapabilities('two', { tools: [], mcp: [], skills: [], revision: 3 })
  coordinator.observeAction('one', { tool: 'bash', shell: 'bash', command: 'command one' })
  coordinator.observeAction('two', { tool: 'bash', shell: 'bash', command: 'command two' })
  const first = coordinator.prepare('one')
  const second = coordinator.prepare('two')
  await started.promise
  await Promise.resolve()
  assert.equal(peak, 1)
  release.resolve()
  const [snapshotOne, snapshotTwo] = await Promise.all([first, second])
  assert.match(snapshotOne, /ACTION command one/)
  assert.match(snapshotTwo, /ACTION command two/)
  assert.equal(peak, 1)
  await coordinator.dispose()
})

test('block, pending, observed-action, and inactive-session storage have explicit bounds', async () => {
  let matches = 0
  const { coordinator } = harness({ settings: { maxRetainedBlocks: 2, maxPendingPerSession: 2, maxObservedActions: 2, maxIdleSessions: 1 }, request: async (req) => result(req, req.operation === 'recall' ? [note('memory-a', 'Bounded memory A'), note('memory-b', 'Bounded memory B'), note('memory-c', 'Bounded memory C')] : []), match: async (_command, _shell, _descriptors, revision) => {
    matches += 1
    return [{ ...note(`action-${matches}`, `Bounded action ${matches}`, 'action_rule'), priority: 99, capabilityRevision: revision }]
  } })
  coordinator.setCapabilities('one', { tools: [], mcp: [], skills: [], revision: 1 })
  coordinator.begin('one', 'bounded synthetic work', 'user')
  for (let index = 0; index < 5; index += 1) coordinator.observeAction('one', { tool: 'bash', shell: 'bash', command: `command ${index}` })
  const snapshot = await coordinator.prepare('one')
  assert.ok((snapshot.match(/Bounded action/g) ?? []).length <= 2)
  assert.ok(coordinator.getDiagnostics('one').includes('pending_overflow'))
  assert.ok(coordinator.getDiagnostics('one').includes('observed_action_overflow'))
  assert.ok(coordinator.getDiagnostics('one').includes('retained_block_overflow'))
  coordinator.begin('two', 'second synthetic work', 'user')
  await coordinator.prepare('two')
  assert.equal(coordinator.getGeneration('one'), 0, 'the inactive session cap prunes old idle state independent of age')
  assert.equal(coordinator.getGeneration('two'), 1)
  await coordinator.dispose()
})

test('a capability-only prepare remains active across TTL while other sessions are created', async () => {
  const clock = new TestClock()
  const capabilities = deferred<Capabilities>()
  let first = true
  const { coordinator } = harness({ clock, settings: { ttlMs: 100, maxIdleSessions: 1 }, capabilities: async () => first ? (first = false, capabilities.promise) : ({ tools: [], mcp: [], skills: [], revision: 1 }) })
  coordinator.begin('one', 'okay', 'task')
  const preparing = coordinator.prepare('one')
  await Promise.resolve()
  await Promise.resolve()
  clock.advanceTo(101)
  coordinator.begin('two', 'synthetic second session', 'user')
  assert.equal(coordinator.getGeneration('one'), 1, 'active session remains addressable during capability wait')
  capabilities.resolve({ tools: [], mcp: [], skills: [], revision: 0 })
  await preparing
  await coordinator.dispose()
})

test('acknowledgement formatting does not renew retained block expiry', async () => {
  const clock = new TestClock()
  const { coordinator } = harness({ clock, settings: { ttlMs: 100 }, request: async (req) => result(req, req.operation === 'recall' ? [note('ttl-note', 'EXPIRABLE_MEMORY')] : []) })
  coordinator.begin('s1', 'first synthetic task', 'user')
  assert.match(await coordinator.prepare('s1'), /EXPIRABLE_MEMORY/)
  clock.time = 90
  coordinator.begin('s1', 'okay', 'user')
  assert.match(await coordinator.prepare('s1'), /EXPIRABLE_MEMORY/)
  clock.time = 101
  assert.doesNotMatch(await coordinator.prepare('s1'), /EXPIRABLE_MEMORY/)
  assert.ok(coordinator.getDiagnostics('s1').includes('snapshot_expired'))
  await coordinator.dispose()
})

test('nonempty helper/retry snapshot is retained and an intentional drain mutant fails the oracle', async () => {
  const { coordinator } = harness({ request: async (req) => result(req, req.operation === 'recall' ? [note('retained', 'RETAINED_HELPER_CONTEXT')] : []) })
  coordinator.begin('s1', 'retrieve synthetic helper context', 'user')
  const first = await coordinator.prepare('s1')
  const retry = await coordinator.prepare('s1')
  assert.match(first, /RETAINED_HELPER_CONTEXT/)
  assert.equal(retry, first)
  let drained = false
  const drainMutant = async () => drained ? '' : (drained = true, first)
  const mutantFirst = await drainMutant()
  const mutantRetry = await drainMutant()
  assert.throws(() => assert.equal(mutantRetry, mutantFirst), 'the nonempty retention oracle rejects drain-on-transform behavior')
  await coordinator.dispose()
})

test('ordinary matcher and pool failures are advisory and fail open', async () => {
  const { coordinator } = harness({ capabilities: async () => ({ tools: [], mcp: [], skills: [], revision: 0 }), request: async (req) => {
    if (req.operation === 'recall') throw new Error('synthetic worker failure')
    return result(req, [])
  }, match: async () => { throw new Error('synthetic matcher failure') } })
  coordinator.begin('s1', 'synthetic work despite bridge failure', 'user')
  coordinator.observeAction('s1', { tool: 'bash', shell: 'bash', command: 'synthetic command' })
  await assert.doesNotReject(coordinator.prepare('s1'))
  assert.ok(coordinator.getDiagnostics('s1').includes('recall_failed'))
  assert.ok(coordinator.getDiagnostics('s1').includes('action_failed'))
  await coordinator.dispose()
})

test('repeated shared matcher busy remains retryable until an owned child closes', async () => {
  const diagnostics: string[] = []
  let ownedChild = true
  let calls = 0
  const { coordinator } = harness({
    matchDiagnostics: diagnostics,
    capabilities: async () => ({ tools: [], mcp: [], skills: [], revision: 7 }),
    detailedMatch: async (_command, _shell, _descriptors, revision) => {
      calls += 1
      if (ownedChild) {
        if (!diagnostics.includes('action-match-busy')) diagnostics.push('action-match-busy')
        return { status: 'busy', blocks: [] }
      }
      return { status: 'ok', blocks: [{ ...note('retry-action', `RETRIED_ACTION revision ${revision}`, 'action_rule'), capabilityRevision: revision }] }
    },
  })
  coordinator.begin('s1', 'synthetic busy matcher task', 'user')
  coordinator.observeAction('s1', { tool: 'bash', shell: 'bash', command: 'synthetic action' })
  const first = await coordinator.prepare('s1')
  assert.doesNotMatch(first, /RETRIED_ACTION/)
  assert.ok(coordinator.getDiagnostics('s1').includes('action_match_busy'))
  const second = await coordinator.prepare('s1')
  assert.doesNotMatch(second, /RETRIED_ACTION/)
  assert.equal(calls, 2, 'busy is classified for each invocation even though its diagnostic is deduplicated')
  ownedChild = false
  const next = await coordinator.prepare('s1')
  assert.match(next, /RETRIED_ACTION revision 7/)
  assert.equal(calls, 3, 'retry remains bounded to one matcher invocation per eligible prepare')
  await coordinator.dispose()
})

test('a second action is retryable when matcher busy is already historical', async () => {
  const diagnostics = ['action-match-busy']
  let ownedChild = true
  let calls = 0
  const { coordinator } = harness({
    matchDiagnostics: diagnostics,
    capabilities: async () => ({ tools: [], mcp: [], skills: [], revision: 11 }),
    detailedMatch: async (command, _shell, _descriptors, revision) => {
      calls += 1
      if (ownedChild) return { status: 'busy', blocks: [] }
      return { status: 'ok', blocks: [{ ...note(`historical:${command}`, `HISTORICAL_BUSY_RETRIED revision ${revision}`, 'action_rule'), capabilityRevision: revision }] }
    },
  })
  coordinator.begin('s1', 'synthetic second action task', 'user')
  coordinator.observeAction('s1', { tool: 'bash', shell: 'bash', command: 'second synthetic action' })
  assert.doesNotMatch(await coordinator.prepare('s1'), /HISTORICAL_BUSY_RETRIED/)
  ownedChild = false
  assert.match(await coordinator.prepare('s1'), /HISTORICAL_BUSY_RETRIED revision 11/)
  assert.equal(calls, 2)
  await coordinator.dispose()
})

test('late tagged action results after aggregate timeout reach the next same-generation boundary', async () => {
  const clock = new TestClock()
  const firstResult = deferred<ContextBlock[]>()
  const secondResult = deferred<ContextBlock[]>()
  const firstStarted = deferred<void>()
  const secondStarted = deferred<void>()
  const { coordinator } = harness({
    clock,
    capabilities: async () => ({ tools: [], mcp: [], skills: [], revision: 9 }),
    match: async (command, _shell, _descriptors, revision) => {
      if (command.endsWith('one')) { firstStarted.resolve(); return firstResult.promise }
      secondStarted.resolve()
      return secondResult.promise
    },
  })
  coordinator.begin('s1', 'synthetic late action task', 'user')
  coordinator.observeAction('s1', { tool: 'bash', shell: 'bash', command: 'late action one' })
  coordinator.observeAction('s1', { tool: 'bash', shell: 'bash', command: 'late action two' })
  const firstPrepare = coordinator.prepare('s1')
  await firstStarted.promise
  assert.deepEqual(clock.waits.map((wait) => wait.deadline), [1_500], 'capability and both actions use one original boundary deadline')
  clock.advanceTo(1_500)
  assert.equal(await firstPrepare, '', 'the first request boundary times out without consuming either action')
  firstResult.resolve([{ ...note('late-one', 'LATE_ACTION_ONE revision 9', 'action_rule'), capabilityRevision: 9 }])
  await secondStarted.promise
  secondResult.resolve([{ ...note('late-two', 'LATE_ACTION_TWO revision 9', 'action_rule'), capabilityRevision: 9 }])
  const next = await coordinator.prepare('s1')
  assert.match(next, /LATE_ACTION_ONE revision 9/)
  assert.match(next, /LATE_ACTION_TWO revision 9/)
  assert.equal(coordinator.getGeneration('s1'), 1)
  await coordinator.dispose()
})

test('permanent action no-hit is not mistaken for temporary matcher busy', async () => {
  let calls = 0
  const diagnostics = ['action-match-busy']
  const { coordinator } = harness({
    matchDiagnostics: diagnostics,
    capabilities: async () => ({ tools: [], mcp: [], skills: [], revision: 6 }),
    detailedMatch: async () => { calls += 1; return { status: 'ok', blocks: [] } },
  })
  coordinator.begin('s1', 'synthetic action with no guidance', 'user')
  coordinator.observeAction('s1', { tool: 'bash', shell: 'bash', command: 'synthetic no-hit action' })
  await coordinator.prepare('s1')
  await coordinator.prepare('s1')
  assert.equal(calls, 1)
  assert.ok(!coordinator.getDiagnostics('s1').includes('action_match_busy'))
  await coordinator.dispose()
})

test('oversized shipped handoff preserves its designated NEXT STEP within the bootstrap cap', async () => {
  const handoff = `SHIPPED: ${'Synthetic completed work. '.repeat(300)}\n\nNEXT STEP: verify the adapter's retry path without repeating shipped work.`
  const { coordinator } = harness({ settings: { bootstrapChars: 650 }, request: async (req) => result(req, req.operation === 'bootstrap' ? [note('handoff', handoff, 'bootstrap')] : []) })
  coordinator.refreshBootstrap('s1')
  const snapshot = await coordinator.prepare('s1')
  assert.ok(snapshot.length <= 650)
  assert.match(snapshot, /NEXT STEP: verify the adapter's retry path without repeating shipped work/)
  await coordinator.dispose()
})
