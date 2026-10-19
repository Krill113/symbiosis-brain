import assert from 'node:assert/strict'
import test from 'node:test'
import { createServerHooks } from '../src/server-hooks.ts'
import { createSaveCheckpoints } from '../src/save-checkpoint.ts'
import { createCoordinator } from '../src/coordinator.ts'
import type { ContextRequest, ContextResult } from '../src/protocol.ts'
import type { WorkerPool } from '../src/worker-pool.ts'
import type { ActionMatcher } from '../src/action-matcher.ts'
import { createServerPlugin } from '../server.ts'
import { mkdtemp, mkdir, rm, writeFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'

function setup(options: { capabilityFailure?: boolean; hostFailure?: boolean } = {}) {
  const calls: string[] = []
  const diagnostics: string[] = []
  const generations = new Map<string, number>()
  const coordinator = {
    begin(session: string, text: string, origin: string) { calls.push(`begin:${session}:${origin}:${text}`); const generation = (generations.get(session) ?? 0) + (origin === 'user' || origin === 'task' ? 1 : 0); generations.set(session, generation); return generation },
    getGeneration(session: string) { return generations.get(session) ?? 0 },
    observeAction(session: string, action: unknown) { calls.push(`action:${session}:${JSON.stringify(action)}`) },
    async prepare(session: string) { calls.push(`prepare:${session}`); return `INJECTED_${session}` },
    setCapabilities() {}, refreshBootstrap(session: string) { calls.push(`bootstrap:${session}`) },
    drop(session: string) { calls.push(`drop:${session}`) },
    async dispose() {}, getDiagnostics() { return [] },
  }
  const checkpoints = createSaveCheckpoints()
  const hooks = createServerHooks({
    coordinator: coordinator as never,
    checkpoints,
    capabilities: async () => {
      if (options.capabilityFailure) throw new Error('synthetic capability failure')
      return { tools: ['edit'], mcp: [], skills: [], revision: 1 }
    },
    hostContext: () => {
      if (options.hostFailure) throw new Error('synthetic host-context failure')
      return { shell: 'bash' as const, cwd: '/synthetic/project', boundary: '/synthetic' }
    },
    runtime: () => ({ python: '/synthetic/python', vault: '/synthetic/vault', tools: '/synthetic/tools', scope: 'demo' }),
    onDiagnostic: (code: string) => { diagnostics.push(code); calls.push(`diagnostic:${code}`) },
  }, { cwd: '/synthetic/project', boundary: '/synthetic', scope: 'demo' })
  return { hooks, calls, checkpoints, diagnostics }
}

test('tool before observes without throwing or mutating arguments, including Brain writes independent of action normalization', async () => {
  const { hooks, calls } = setup()
  const args = { patchText: 'synthetic patch' }
  const before = structuredClone(args)
  await hooks['tool.execute.before']!({ tool: 'apply_patch', sessionID: 's', callID: '1' }, { args })
  await hooks['tool.execute.before']!({ tool: 'apply_patch', sessionID: 's', callID: '1' }, { args })
  await hooks['tool.execute.before']!({ tool: 'symbiosis-brain_brain_write', sessionID: 's', callID: '2' }, { args: { path: 'demo/lesson.md' } })
  assert.deepEqual(args, before)
  assert.ok(calls.some((call) => call.startsWith('action:s:')))
  assert.equal(calls.filter((call) => call.startsWith('action:s:')).length, 1)
})

test('system transform without a session id is a no-op and errors fail open', async () => {
  const { hooks, calls } = setup()
  const output = { system: ['original'] }
  await hooks['experimental.chat.system.transform']!({ model: {} } as never, output)
  assert.deepEqual(output.system, ['original'])
  assert.equal(calls.length, 0)
})

test('duplicate event callbacks bootstrap and query only once', async () => {
  const { hooks, calls } = setup()
  const event = { event: { type: 'session.created', properties: { info: { id: 's' } } } }
  await hooks.event!(event as never)
  await hooks.event!(event as never)
  const message = { sessionID: 's' }
  const output = { message: { role: 'user', id: 'm' }, parts: [{ type: 'text', text: 'Implement this synthetic task' }] }
  await hooks['chat.message']!(message as never, output as never)
  await hooks['chat.message']!(message as never, output as never)
  assert.equal(calls.filter((call) => call.startsWith('begin:s:user:')).length, 1)
})

test('real task child independently begins/bootstrap/queries; synthetic continue is not a generation', async () => {
  const { hooks, calls } = setup()
  await hooks.event!({ event: { type: 'session.created', properties: { info: { id: 'child', parentID: 'parent' } } } } as never)
  const output = { message: { role: 'user', id: 'child-message' }, parts: [{ type: 'text', text: 'Investigate fixture behavior' }] }
  await hooks['chat.message']!({ sessionID: 'child' } as never, output as never)
  const system = { system: [] as string[] }
  await hooks['experimental.chat.system.transform']!({ sessionID: 'child', model: {} } as never, system)
  assert.ok(calls.some((call) => call.startsWith('begin:child:task:')))
  assert.ok(calls.includes('prepare:child'))
  await hooks.event!({ event: { type: 'session.compacted', properties: { sessionID: 'child' } } } as never)
  await hooks['chat.message']!({ sessionID: 'child' } as never, { message: { role: 'user', id: 'synthetic' }, parts: [{ type: 'text', text: 'Continue if you have next steps', synthetic: true, metadata: { compaction_continue: true } }] } as never)
  assert.equal(calls.filter((call) => call.startsWith('begin:child:')).length, 1)
  await hooks['chat.message']!({ sessionID: 'other' } as never, { message: { role: 'user', id: 'synthetic-part' }, parts: [{ type: 'text', text: 'Continue if you have next steps', synthetic: true, metadata: { compaction_continue: true } }] } as never)
  assert.equal(calls.filter((call) => call.startsWith('begin:other:')).length, 0)
})

test('plain continue text after compaction is not guessed to be synthetic', async () => {
  const { hooks, calls } = setup()
  await hooks['chat.message']!({ sessionID: 's' } as never, {
    message: { role: 'user', id: 'before-compact' }, parts: [{ type: 'text', text: 'Finish the fixture' }],
  } as never)
  await hooks.event!({ event: { type: 'session.compacted', properties: { sessionID: 's' } } } as never)
  await hooks['chat.message']!({ sessionID: 's' } as never, {
    message: { role: 'user', id: 'real-continue' }, parts: [{ type: 'text', text: 'continue' }] as never,
  } as never)
  assert.equal(calls.filter((call) => call.startsWith('begin:s:user:')).length, 2)
})

test('real coordinator receives normalized edit, patch, and search queries and delivers action hints next turn', async () => {
  const requests: ContextRequest[] = []
  const pool: WorkerPool = {
    async request(request) {
      requests.push(request)
      const blocks = request.operation === 'recall' && request.origin === 'action'
        ? [{ id: `action-${request.requestId}`, text: `FIXTURE_HINT_${request.query}`, priority: 50, category: 'memory' as const }]
        : []
      return { version: 1, kind: 'result', requestId: request.requestId, sessionId: request.sessionId,
        generation: request.generation, capabilityRevision: request.capabilityRevision, status: 'ok',
        scope: request.scope ?? null, blocks, diagnostics: [] }
    },
    async dispose() {},
  }
  const matcher: ActionMatcher = {
    async match() { return [] },
    async matchDetailed() { return { status: 'ok', blocks: [] } },
    async dispose() {},
    diagnostics: [],
  }
  const coordinator = createCoordinator({ cwd: '/synthetic/project', boundary: '/synthetic', scope: 'demo' }, pool, {
    now: () => Date.now(), waitUntil: (deadline) => new Promise((resolve) => setTimeout(resolve, Math.max(0, deadline - Date.now()))),
  }, async () => ({ tools: [], mcp: [], skills: [], revision: 1 }), matcher)
  const hooks = createServerHooks({
    coordinator, checkpoints: createSaveCheckpoints(),
    capabilities: async () => ({ tools: [], mcp: [], skills: [], revision: 1 }),
    hostContext: () => ({ shell: null, cwd: '/synthetic/project', boundary: '/synthetic' }),
    runtime: () => ({}),
  }, { cwd: '/synthetic/project', boundary: '/synthetic', scope: 'demo' })

  await hooks['chat.message']!({ sessionID: 'parent' } as never, {
    message: { role: 'user', id: 'initial' }, parts: [{ type: 'text', text: 'Implement a synthetic fixture' }],
  } as never)
  await coordinator.prepare('parent')
  const parentGeneration = coordinator.getGeneration('parent')
  const actions = [
    ['edit', { filePath: 'src/fixture.ts', newString: 'preserve the synthetic migration rule' }],
    ['apply_patch', { patchText: '*** Begin Patch\n*** Update File: src/fixture.ts\n+preserve the synthetic patch invariant\n*** End Patch' }],
    ['grep', { pattern: 'synthetic invariant', path: 'src' }],
  ] as const
  for (const [index, [tool, args]] of actions.entries()) {
    await hooks['tool.execute.before']!({ tool, sessionID: 'parent', callID: `action-${index}` }, { args } as never)
    assert.equal(coordinator.getGeneration('parent'), parentGeneration, 'an action observation must not create a user generation')
    const snapshot = await coordinator.prepare('parent')
    assert.ok(snapshot.includes(`FIXTURE_HINT_`), `${tool} hint must arrive at the next request boundary`)
  }
  assert.equal(requests.filter((request) => request.operation === 'recall' && request.origin === 'action').length, 3)
  assert.ok(requests.some((request) => request.origin === 'action' && request.query?.includes('synthetic migration rule')))
  assert.ok(requests.some((request) => request.origin === 'action' && request.query?.includes('synthetic patch invariant')))
  assert.ok(requests.some((request) => request.origin === 'action' && request.query?.includes('synthetic invariant')))
  await coordinator.dispose()
})

test('parent task observation preserves its generation, pending recall and checkpoint while child recalls independently', async () => {
  const requests: ContextRequest[] = []
  let parentPending: ContextRequest | undefined
  let parentSignal: AbortSignal | undefined
  let releaseParent: ((result: ContextResult) => void) | undefined
  const pool: WorkerPool = {
    request(request, signal) {
      requests.push(request)
      if (request.sessionId === 'parent' && request.query?.includes('PARENT_PENDING_QUERY')) {
        parentPending = request
        parentSignal = signal
        return new Promise<ContextResult>((resolve) => { releaseParent = resolve })
      }
      return Promise.resolve({ version: 1, kind: 'result', requestId: request.requestId, sessionId: request.sessionId,
        generation: request.generation, capabilityRevision: request.capabilityRevision, status: 'ok', scope: request.scope ?? null,
        blocks: request.operation === 'recall' ? [{ id: `recall-${request.sessionId}-${request.requestId}`, text: `OWN_HINT_${request.sessionId}`, priority: 50, category: 'memory' }] : [],
        diagnostics: [] })
    },
    async dispose() {},
  }
  const matcher: ActionMatcher = { async match() { return [] }, async matchDetailed() { return { status: 'ok', blocks: [] } }, async dispose() {}, diagnostics: [] }
  const coordinator = createCoordinator({ cwd: '/synthetic/project', boundary: '/synthetic', scope: 'demo' }, pool, {
    now: () => Date.now(), waitUntil: (deadline) => new Promise((resolve) => setTimeout(resolve, Math.max(0, deadline - Date.now()))),
  }, async () => ({ tools: [], mcp: [], skills: [], revision: 1 }), matcher)
  const checkpoints = createSaveCheckpoints()
  const hooks = createServerHooks({
    coordinator, checkpoints, capabilities: async () => ({ tools: [], mcp: [], skills: [], revision: 1 }),
    hostContext: () => ({ shell: null, cwd: '/synthetic/project', boundary: '/synthetic' }), runtime: () => ({}),
    isChildSession: async (id) => id === 'child',
  }, { cwd: '/synthetic/project', boundary: '/synthetic', scope: 'demo' })

  await hooks['chat.message']!({ sessionID: 'parent' } as never, {
    message: { role: 'user', id: 'parent-next' }, parts: [{ type: 'text', text: 'PARENT_PENDING_QUERY continue the parent work' }],
  } as never)
  const parentGeneration = coordinator.getGeneration('parent')
  checkpoints.onTodo('parent', [{ status: 'in_progress' }])
  checkpoints.onTodo('parent', [{ status: 'completed' }])
  await Promise.resolve()
  await Promise.resolve()
  assert.ok(parentPending, 'parent recall should be in flight before the task tool starts')

  await hooks['tool.execute.before']!({ tool: 'task', sessionID: 'parent', callID: 'task-call' }, {
    args: { description: 'Inspect child fixture', prompt: 'CHILD_OWN_QUERY inspect fixture behavior' },
  } as never)
  assert.equal(coordinator.getGeneration('parent'), parentGeneration, 'task tool calls must not start a parent user generation')
  assert.equal(parentSignal?.aborted, false, 'parent pending recall must not be cancelled by task launch')
  const taskResult = { title: 'Inspect child fixture', output: 'Synthetic child result', metadata: {} }
  await hooks['tool.execute.after']!({ tool: 'task', sessionID: 'parent', callID: 'task-call', args: {} } as never, taskResult as never)
  assert.match(taskResult.output, /\[SB save checkpoint\]/, 'parent checkpoint remains pending through task launch')

  const taskRecall = requests.find((request) => request.sessionId === 'parent' && request.origin === 'action' && request.query?.includes('CHILD_OWN_QUERY'))
  assert.ok(taskRecall, 'parent observes task prompt as an action query without changing generation')
  await hooks['chat.message']!({ sessionID: 'child' } as never, {
    message: { role: 'user', id: 'child-first' }, parts: [{ type: 'text', text: 'CHILD_OWN_QUERY inspect fixture behavior' }],
  } as never)
  assert.equal(coordinator.getGeneration('child'), 1)
  const childSnapshot = await coordinator.prepare('child')
  assert.match(childSnapshot, /OWN_HINT_child/)
  assert.ok(requests.some((request) => request.sessionId === 'child' && request.origin === 'task' && request.query?.includes('CHILD_OWN_QUERY')))
  assert.equal(coordinator.getGeneration('parent'), parentGeneration)

  assert.ok(parentPending && releaseParent)
  releaseParent({ version: 1, kind: 'result', requestId: parentPending.requestId, sessionId: parentPending.sessionId,
    generation: parentPending.generation, capabilityRevision: parentPending.capabilityRevision, status: 'ok', scope: parentPending.scope ?? null,
    blocks: [{ id: 'parent-pending', text: 'PARENT_PENDING_HINT', priority: 50, category: 'memory' }], diagnostics: [] })
  await coordinator.prepare('parent')
  assert.ok(requests.some((request) => request.sessionId === 'parent' && request.query?.includes('PARENT_PENDING_QUERY')))
  await coordinator.dispose()
})

test('production server factory retains active selection and refreshes enabled guidance across agent/model changes', async () => {
  const endpointCalls: string[] = []
  const requests: ContextRequest[] = []
  const agents = [
    { name: 'build', tools: { edit: true, view: false, demo_brain_search: true } },
    { name: 'read', tools: { edit: false, view: true, demo_brain_search: true } },
  ]
  const pool: WorkerPool = {
    async request(request) {
      requests.push(request)
      const common = { version: 1 as const, kind: 'result' as const, requestId: request.requestId, sessionId: request.sessionId,
        generation: request.generation, capabilityRevision: request.capabilityRevision, status: 'ok' as const,
        scope: request.operation === 'bootstrap' ? 'demo' : request.scope ?? null, diagnostics: [] as string[] }
      if (request.operation === 'resources') return { ...common, blocks: [], resources: {
        pythonExe: '/synthetic/python', packageDir: '/synthetic/site-packages/symbiosis_brain', skillsDir: '/synthetic/skills',
        grepExe: null, actionRulesFile: '/synthetic/action-rules.tsv', mcpCommand: [],
      } }
      if (request.operation === 'routes') {
        const route = request.capabilities.tools.includes('edit') ? 'BUILD_ONLY_ROUTE' : request.capabilities.tools.includes('view') ? 'READ_ONLY_ROUTE' : 'NO_ENABLED_ROUTE'
        return { ...common, blocks: [{ id: `route-${request.capabilityRevision}`, text: route, priority: 80, category: 'route' as const, capabilityRevision: request.capabilityRevision }] }
      }
      return { ...common, blocks: request.operation === 'bootstrap' ? [{ id: 'boot', text: 'SYNTHETIC_BOOTSTRAP', priority: 100, category: 'bootstrap' as const }] : [] }
    },
    async dispose() {},
  }
  const configResponse = { data: {
    shell: 'pwsh.exe',
    mcp: { demo: { type: 'local', enabled: true, command: ['/synthetic/python', '-m', 'symbiosis_brain.server'], environment: { SYMBIOSIS_BRAIN_VAULT: '/synthetic/vault' } } },
  } }
  const client = {
    config: { async get() { return configResponse } },
    app: { async agents() { endpointCalls.push('agents'); return { data: agents } } },
    mcp: { async status() { endpointCalls.push('mcp'); return { data: { demo: { status: 'connected' } } } } },
    tool: { async ids() { endpointCalls.push('tools'); return { data: ['edit', 'view', 'demo_brain_search'] } } },
    session: { async get() { return { data: { id: 's', parentID: null } } } },
  }
  const server = createServerPlugin({ workerPool: () => pool, actionMatcher: () => ({
    async match() { return [] }, async matchDetailed() { return { status: 'ok', blocks: [] } }, async dispose() {}, diagnostics: [],
  }) })
  const hooks = await server({ client, directory: '/synthetic/project', worktree: '/synthetic' } as never,
    { python: '/synthetic/python', vault: '/synthetic/vault', tools: '/synthetic/tools' } as never)

  await hooks['chat.message']!({ sessionID: 's', agent: 'build', model: { providerID: 'synthetic', modelID: 'model-one' } } as never,
    { message: { role: 'user', id: 'm1' }, parts: [{ type: 'text', text: 'Build a synthetic fixture' }] } as never)
  assert.equal(endpointCalls.length, 3, 'chat callback discovers active agent, MCP status, and tool IDs')
  const first = { system: [] as string[] }
  await hooks['experimental.chat.system.transform']!({ sessionID: 's', model: {} } as never, first)
  assert.equal(endpointCalls.length, 6, 'coordinator receives the same retained selection provider')
  assert.ok(first.system.some((block) => block.includes('BUILD_ONLY_ROUTE')))

  await hooks['chat.message']!({ sessionID: 's', agent: 'read', model: { providerID: 'synthetic', modelID: 'model-two' } } as never,
    { message: { role: 'user', id: 'm2' }, parts: [{ type: 'text', text: 'Inspect a synthetic fixture' }] } as never)
  const second = { system: [] as string[] }
  await hooks['experimental.chat.system.transform']!({ sessionID: 's', model: {} } as never, second)
  assert.equal(endpointCalls.length, 12)
  assert.ok(second.system.some((block) => block.includes('READ_ONLY_ROUTE')))
  assert.ok(!second.system.some((block) => block.includes('BUILD_ONLY_ROUTE')))
  const shellOutput: { env: Record<string, string> } = { env: {} }
  await hooks['shell.env']!({ cwd: '/synthetic/project', sessionID: 's' }, shellOutput)
  assert.equal(shellOutput.env.SYMBIOSIS_BRAIN_OPENCODE_PYTHON, '/synthetic/python')
  assert.equal(shellOutput.env.SYMBIOSIS_BRAIN_VAULT, '/synthetic/vault')
  assert.equal(shellOutput.env.SYMBIOSIS_BRAIN_TOOLS, '/synthetic/site-packages/symbiosis_brain')
  assert.equal(shellOutput.env.SYMBIOSIS_BRAIN_SCOPE, 'demo')
  await hooks['tool.execute.before']!({ tool: 'powershell', sessionID: 's', callID: 'shell-action' }, { args: { command: 'npm install' } })
  const afterShell = { system: [] as string[] }
  await hooks['experimental.chat.system.transform']!({ sessionID: 's', model: {} } as never, afterShell)
  assert.ok(requests.some((request) => request.sessionId === 's' && request.origin === 'action' && request.query?.includes('npm install')),
    'verified host shell selection enables its normalized action query')
  await hooks.dispose?.()
})

test('production scope invalidates on A-to-B cwd change before B inference without shell.env', async () => {
  const requests: ContextRequest[] = []
  const pool: WorkerPool = {
    async request(request) {
      requests.push(request)
      const scope = request.cwd.toLocaleLowerCase().endsWith('project-a') ? 'project-a'
        : request.cwd.toLocaleLowerCase().endsWith('project-b') ? 'project-b'
        : undefined
      const common = { version: 1 as const, kind: 'result' as const, requestId: request.requestId, sessionId: request.sessionId,
        generation: request.generation, capabilityRevision: request.capabilityRevision, status: 'ok' as const,
        scope: scope ?? request.scope ?? null, diagnostics: [] as string[] }
      if (request.operation === 'resources') return { ...common, blocks: [], resources: {
        pythonExe: '/synthetic/python', packageDir: '/synthetic/site-packages/symbiosis_brain', skillsDir: '/synthetic/skills',
        grepExe: null, actionRulesFile: '/synthetic/action-rules.tsv', mcpCommand: [],
      } }
      if (request.operation === 'recall') {
        const text = request.scope === 'project-a' ? 'PROJECT_A_ONLY_HINT' : request.scope === 'project-b' ? 'PROJECT_B_ONLY_HINT' : 'UNKNOWN_SCOPE_HINT'
        return { ...common, blocks: [{ id: `recall-${request.requestId}`, text, priority: 50, category: 'memory' as const }] }
      }
      if (request.operation === 'bootstrap') return { ...common, blocks: [{ id: `boot-${request.scope}`, text: `BOOT_${request.scope}`, priority: 100, category: 'bootstrap' as const }] }
      return { ...common, blocks: [] }
    },
    async dispose() {},
  }
  let sdkDirectory = '/synthetic/project-a'
  const client = {
    config: { async get() { return { data: { shell: 'pwsh.exe' } } } },
    app: { async agents() { return { data: [{ name: 'build', permission: [{ permission: '*', pattern: '*', action: 'allow' }] }] } } },
    mcp: { async status() { return { data: {} } } },
    tool: { async ids() { return { data: ['read'] } } },
    session: { async get({ path }: { path?: { id?: string } }) {
      return { data: { id: path?.id ?? 's', directory: sdkDirectory } }
    } },
  }
  const server = createServerPlugin({ workerPool: () => pool, actionMatcher: () => ({
    async match() { return [] }, async matchDetailed() { return { status: 'ok', blocks: [] } }, async dispose() {}, diagnostics: [],
  }) })
  const hooks = await server({ client, directory: '/synthetic/root', worktree: '/synthetic' } as never,
    { python: '/synthetic/python', vault: '/synthetic/vault', tools: '/synthetic/tools', shell: 'pwsh.exe' } as never)

  await hooks.event!({ event: { type: 'session.created', properties: { info: { id: 's', directory: '/synthetic/project-a' } } } } as never)
  await hooks['chat.message']!({ sessionID: 's', agent: 'build', model: { providerID: 'synthetic', modelID: 'model-a' } } as never,
    { message: { role: 'user', id: 'msg-a' }, parts: [{ type: 'text', text: 'Investigate project A fixture' }] } as never)
  const aOutput = { system: [] as string[] }
  await hooks['experimental.chat.system.transform']!({ sessionID: 's', model: {} } as never, aOutput)
  assert.ok(requests.some((request) => request.sessionId === 's' && request.operation === 'recall'
    && request.query?.includes('project A fixture') && request.scope === 'project-a'),
  `A inference should use canonical project A scope before its recall request; got ${JSON.stringify(requests.map(({ operation, cwd, scope, query, generation }) => ({ operation, cwd, scope, query, generation })))}`)
  assert.ok(aOutput.system.some((block) => block.includes('PROJECT_A_ONLY_HINT')))

  await hooks.event!({ event: { type: 'session.updated', properties: { info: { id: 's', directory: '/synthetic/project-b' } } } } as never)
  sdkDirectory = '/synthetic/project-b'
  await hooks['chat.message']!({ sessionID: 's', agent: 'build', model: { providerID: 'synthetic', modelID: 'model-a' } } as never,
    { message: { role: 'user', id: 'msg-b' }, parts: [{ type: 'text', text: 'Investigate project B fixture' }] } as never)
  const bOutput = { system: [] as string[] }
  await hooks['experimental.chat.system.transform']!({ sessionID: 's', model: {} } as never, bOutput)
  const bRecalls = requests.filter((request) => request.sessionId === 's' && request.operation === 'recall'
    && request.query?.includes('project B fixture'))
  assert.ok(bRecalls.length > 0)
  assert.ok(bRecalls.every((request) => request.cwd.toLocaleLowerCase().endsWith('project-b') && request.scope === 'project-b'))
  assert.ok(bOutput.system.some((block) => block.includes('PROJECT_B_ONLY_HINT')))
  assert.ok(!bOutput.system.some((block) => block.includes('PROJECT_A_ONLY_HINT')))
  await hooks.dispose?.()
})

test('shell.env resolves a new cwd canonically before exporting its scope', async () => {
  const requests: ContextRequest[] = []
  const pool: WorkerPool = {
    async request(request) {
      requests.push(request)
      const scope = request.cwd.toLocaleLowerCase().endsWith('project-a') ? 'project-a'
        : request.cwd.toLocaleLowerCase().endsWith('project-b') ? 'project-b'
        : request.cwd.toLocaleLowerCase().endsWith('project-c') ? 'project-c' : undefined
      const common = { version: 1 as const, kind: 'result' as const, requestId: request.requestId, sessionId: request.sessionId,
        generation: request.generation, capabilityRevision: request.capabilityRevision, status: 'ok' as const,
        scope: scope ?? request.scope ?? null, diagnostics: [] as string[] }
      if (request.operation === 'resources') return { ...common, blocks: [], resources: {
        pythonExe: '/synthetic/python', packageDir: '/synthetic/site-packages/symbiosis_brain', skillsDir: '/synthetic/skills',
        grepExe: null, actionRulesFile: '/synthetic/action-rules.tsv', mcpCommand: [],
      } }
      if (request.operation === 'recall') return { ...common, blocks: [{ id: `recall-${request.requestId}`,
        text: request.scope === 'project-a' ? 'PROJECT_A_ONLY_HINT' : request.scope === 'project-b' ? 'PROJECT_B_ONLY_HINT' : 'PROJECT_C_ONLY_HINT',
        priority: 50, category: 'memory' as const }] }
      return { ...common, blocks: [] }
    },
    async dispose() {},
  }
  let sdkDirectory = '/synthetic/project-a'
  const client = {
    config: { async get() { return { data: { shell: 'pwsh.exe' } } } },
    app: { async agents() { return { data: [{ name: 'build', permission: [{ permission: '*', pattern: '*', action: 'allow' }] }] } } },
    mcp: { async status() { return { data: {} } } },
    tool: { async ids() { return { data: ['read'] } } },
    session: { async get({ path }: { path?: { id?: string } }) {
      return { data: { id: path?.id ?? 's', directory: sdkDirectory } }
    } },
  }
  const server = createServerPlugin({ workerPool: () => pool, actionMatcher: () => ({
    async match() { return [] }, async matchDetailed() { return { status: 'ok', blocks: [] } }, async dispose() {}, diagnostics: [],
  }) })
  const hooks = await server({ client, directory: '/synthetic/root', worktree: '/synthetic' } as never,
    { python: '/synthetic/python', vault: '/synthetic/vault', tools: '/synthetic/tools', shell: 'pwsh.exe' } as never)

  await hooks['experimental.chat.system.transform']!({ sessionID: 's', model: {} } as never, { system: [] })
  const envA = { env: {} as Record<string, string> }
  await hooks['shell.env']!({ cwd: '/synthetic/project-a', sessionID: 's' }, envA)
  assert.equal(envA.env.SYMBIOSIS_BRAIN_SCOPE, 'project-a')

  const envB = { env: {} as Record<string, string> }
  await hooks['shell.env']!({ cwd: '/synthetic/project-b', sessionID: 's' }, envB)
  assert.equal(envB.env.SYMBIOSIS_BRAIN_SCOPE, 'project-b')
  sdkDirectory = '/synthetic/project-b'
  assert.ok(requests.some((request) => request.operation === 'bootstrap' && request.cwd.endsWith('project-b')),
    'B canonical resources/scope must be requested before shell.env exports B')

  await hooks['chat.message']!({ sessionID: 's', agent: 'build', model: { providerID: 'synthetic', modelID: 'model-a' } } as never,
    { message: { role: 'user', id: 'msg-b-shell' }, parts: [{ type: 'text', text: 'Investigate project B fixture' }] } as never)
  const bOutput = { system: [] as string[] }
  await hooks['experimental.chat.system.transform']!({ sessionID: 's', model: {} } as never, bOutput)
  const bRecalls = requests.filter((request) => request.operation === 'recall' && request.query?.includes('project B fixture'))
  assert.ok(bRecalls.length > 0 && bRecalls.every((request) => request.cwd.endsWith('project-b') && request.scope === 'project-b'))
  assert.ok(bOutput.system.some((block) => block.includes('PROJECT_B_ONLY_HINT')))
  assert.ok(!bOutput.system.some((block) => block.includes('PROJECT_A_ONLY_HINT')))

  sdkDirectory = '/synthetic/project-c'
  await hooks['chat.message']!({ sessionID: 's', agent: 'build', model: { providerID: 'synthetic', modelID: 'model-a' } } as never,
    { message: { role: 'user', id: 'msg-c-lazy' }, parts: [{ type: 'text', text: 'Investigate project C fixture' }] } as never)
  const cOutput = { system: [] as string[] }
  await hooks['experimental.chat.system.transform']!({ sessionID: 's', model: {} } as never, cOutput)
  const cRecalls = requests.filter((request) => request.operation === 'recall' && request.query?.includes('project C fixture'))
  assert.ok(cRecalls.length > 0 && cRecalls.every((request) => request.cwd.endsWith('project-c') && request.scope === 'project-c'))
  assert.ok(cOutput.system.some((block) => block.includes('PROJECT_C_ONLY_HINT')))
  assert.ok(!cOutput.system.some((block) => block.includes('PROJECT_A_ONLY_HINT') || block.includes('PROJECT_B_ONLY_HINT')))
  await hooks.dispose?.()
})

test('late A scope resolution is discarded after B location supersedes it', async () => {
  const requests: ContextRequest[] = []
  let releaseA!: (result: ContextResult) => void
  let pendingA!: ContextRequest
  const delayedA = new Promise<ContextResult>((resolve) => { releaseA = resolve })
  const pool: WorkerPool = {
    request(request) {
      requests.push(request)
      const scope = request.cwd.toLocaleLowerCase().endsWith('project-a') ? 'project-a'
        : request.cwd.toLocaleLowerCase().endsWith('project-b') ? 'project-b' : undefined
      const common = { version: 1 as const, kind: 'result' as const, requestId: request.requestId, sessionId: request.sessionId,
        generation: request.generation, capabilityRevision: request.capabilityRevision, status: 'ok' as const,
        scope: scope ?? request.scope ?? null, diagnostics: [] as string[] }
      if (request.operation === 'resources') return Promise.resolve({ ...common, blocks: [], resources: {
        pythonExe: '/synthetic/python', packageDir: '/synthetic/package', skillsDir: '/synthetic/skills', grepExe: null,
        actionRulesFile: '/synthetic/actions.tsv', mcpCommand: [],
      } })
      if (request.operation === 'bootstrap' && request.requestId.startsWith('scope-') && scope === 'project-a') {
        pendingA = request
        return delayedA
      }
      if (request.operation === 'recall') return Promise.resolve({ ...common, blocks: [{
        id: `hint-${request.requestId}`, text: request.scope === 'project-b' ? 'PROJECT_B_CURRENT_ONLY' : 'UNKNOWN_SCOPE',
        priority: 50, category: 'memory' as const,
      }] })
      return Promise.resolve({ ...common, blocks: [] })
    },
    async dispose() {},
  }
  let sdkDirectory = '/synthetic/project-a'
  const client = {
    config: { async get() { return { data: { shell: 'pwsh.exe' } } } },
    app: { async agents() { return { data: [{ name: 'build', permission: [{ permission: '*', pattern: '*', action: 'allow' }] }] } } },
    mcp: { async status() { return { data: {} } } },
    tool: { async ids() { return { data: ['read'] } } },
    session: { async get({ path }: { path?: { id?: string } }) { return { data: { id: path?.id ?? 's', directory: sdkDirectory } } } },
  }
  const server = createServerPlugin({ workerPool: () => pool, actionMatcher: () => ({
    async match() { return [] }, async matchDetailed() { return { status: 'ok', blocks: [] } }, async dispose() {}, diagnostics: [],
  }) })
  const hooks = await server({ client, directory: '/synthetic/root', worktree: '/synthetic' } as never,
    { python: '/synthetic/python', vault: '/synthetic/vault', tools: '/synthetic/tools', shell: 'pwsh.exe' } as never)
  const lateA = hooks['chat.message']!({ sessionID: 's', agent: 'build', model: { providerID: 'fixture', modelID: 'model' } } as never,
    { message: { role: 'user', id: 'turn-a' }, parts: [{ type: 'text', text: 'TURN_A_QUERY stale old workspace' }] } as never)
  await Promise.resolve()
  await Promise.resolve()
  assert.ok(pendingA, 'A canonical scope query should be in flight before B arrives')
  await hooks.event!({ event: { type: 'session.updated', properties: { info: { id: 's', directory: '/synthetic/project-b' } } } } as never)
  sdkDirectory = '/synthetic/project-b'
  await hooks['chat.message']!({ sessionID: 's', agent: 'build', model: { providerID: 'fixture', modelID: 'model' } } as never,
    { message: { role: 'user', id: 'turn-b' }, parts: [{ type: 'text', text: 'TURN_B_QUERY current workspace' }] } as never)
  const output = { system: [] as string[] }
  await hooks['experimental.chat.system.transform']!({ sessionID: 's', model: {} } as never, output)
  releaseA({ version: 1, kind: 'result', requestId: pendingA.requestId, sessionId: pendingA.sessionId,
    generation: pendingA.generation, capabilityRevision: pendingA.capabilityRevision, status: 'ok', scope: 'project-a', blocks: [], diagnostics: [] })
  await lateA
  const current = requests.filter((request) => request.sessionId === 's' && request.query?.includes('TURN_B_QUERY'))
  assert.ok(current.length > 0 && current.every((request) => request.scope === 'project-b'))
  assert.ok(!requests.some((request) => request.sessionId === 's' && request.query?.includes('TURN_A_QUERY')),
    'superseded prompt cannot publish its stale A scope/query')
  assert.ok(output.system.some((block) => block.includes('PROJECT_B_CURRENT_ONLY')))
  assert.ok(!output.system.some((block) => block.includes('UNKNOWN_SCOPE')))
  await hooks.dispose?.()
})

test('helper request does not receive save imperative and compaction only adds reference context', async () => {
  const { hooks, calls, checkpoints } = setup()
  await hooks['chat.message']!({ sessionID: 's' } as never, { message: { role: 'user', id: 'helper' }, parts: [{ type: 'text', text: 'Create a title for this session' }] } as never)
  checkpoints.onTodo('s', [{ status: 'in_progress' }])
  checkpoints.onTodo('s', [{ status: 'completed' }])
  const output = { system: [] as string[] }
  await hooks['experimental.chat.system.transform']!({ sessionID: 's', model: {} } as never, output)
  assert.equal(output.system.filter((text) => text.includes('[SB save checkpoint]')).length, 0)
  const compact = { context: [] as string[] }
  await hooks['experimental.session.compacting']!({ sessionID: 's' }, compact)
  assert.ok(compact.context.length > 0)
  assert.ok(!calls.some((call) => call.includes('brain_write')))
})

test('throwing dependencies fail open and async capability failures become safe diagnostics', async () => {
  const hostFailure = setup({ hostFailure: true })
  const args = { filePath: 'fixture.ts', newString: 'synthetic content' }
  await hostFailure.hooks['tool.execute.before']!({ tool: 'edit', sessionID: 's', callID: 'throwing-host' }, { args })
  assert.deepEqual(args, { filePath: 'fixture.ts', newString: 'synthetic content' })
  assert.deepEqual(hostFailure.diagnostics, ['hook_callback_failed'])

  const capabilityFailure = setup({ capabilityFailure: true })
  await capabilityFailure.hooks['chat.message']!({ sessionID: 's' } as never, {
    message: { role: 'user', id: 'capability-failure' }, parts: [{ type: 'text', text: 'Synthetic task continues' }],
  } as never)
  await Promise.resolve()
  await Promise.resolve()
  assert.ok(capabilityFailure.diagnostics.includes('capability_discovery_failed'))
})

test('shell env writes only owned runtime values and preserves foreign values', async () => {
  const { hooks } = setup()
  const output: { env: Record<string, string> } = { env: { PATH: 'FOREIGN_PATH', SYMBIOSIS_BRAIN_VAULT: 'FOREIGN_VAULT' } }
  await hooks['shell.env']!({ cwd: '/synthetic/project', sessionID: 's' }, output)
  assert.equal(output.env.PATH, 'FOREIGN_PATH')
  assert.equal(output.env.SYMBIOSIS_BRAIN_VAULT, 'FOREIGN_VAULT')
  assert.equal(output.env.SYMBIOSIS_BRAIN_OPENCODE_PYTHON, '/synthetic/python')
  assert.equal(output.env.SYMBIOSIS_BRAIN_TOOLS, '/synthetic/tools')
  assert.equal(output.env.SYMBIOSIS_BRAIN_SCOPE, 'demo')
})

test('clean shell env exposes the OpenCode path aliases consumed by canonical skills', async () => {
  const { hooks } = setup()
  const output: { env: Record<string, string> } = { env: {} }
  await hooks['shell.env']!({ cwd: '/synthetic/project', sessionID: 's' }, output)
  assert.equal(output.env.SYMBIOSIS_BRAIN_OPENCODE_VAULT, '/synthetic/vault')
  assert.equal(output.env.SYMBIOSIS_BRAIN_OPENCODE_TOOLS, '/synthetic/tools')
  assert.equal(output.env.SYMBIOSIS_BRAIN_OPENCODE_PYTHON, '/synthetic/python')
  assert.equal(output.env.SYMBIOSIS_BRAIN_VAULT, output.env.SYMBIOSIS_BRAIN_OPENCODE_VAULT)
  assert.equal(output.env.SYMBIOSIS_BRAIN_TOOLS, output.env.SYMBIOSIS_BRAIN_OPENCODE_TOOLS)
})

test('shell env does not overwrite foreign OpenCode aliases while exporting generic runtime paths', async () => {
  const { hooks } = setup()
  const output = { env: { SYMBIOSIS_BRAIN_OPENCODE_VAULT: 'FOREIGN_VAULT', SYMBIOSIS_BRAIN_OPENCODE_TOOLS: 'FOREIGN_TOOLS' } as Record<string, string> }
  await hooks['shell.env']!({ cwd: '/synthetic/project', sessionID: 's' }, output)
  assert.equal(output.env.SYMBIOSIS_BRAIN_OPENCODE_VAULT, 'FOREIGN_VAULT')
  assert.equal(output.env.SYMBIOSIS_BRAIN_OPENCODE_TOOLS, 'FOREIGN_TOOLS')
  assert.equal(output.env.SYMBIOSIS_BRAIN_VAULT, '/synthetic/vault')
  assert.equal(output.env.SYMBIOSIS_BRAIN_TOOLS, '/synthetic/tools')
})

test('project-entry discovery disables the new advisory plugin when a legacy copy is active', async () => {
  const project = await mkdtemp(join(tmpdir(), 'sb-legacy-project-'))
  try {
    const legacyDir = join(project, '.opencode', 'plugins')
    await mkdir(legacyDir, { recursive: true })
    await writeFile(join(legacyDir, 'brain-recall.ts'), '/* synthetic legacy adapter */\nexport default {}\n')
    let workerStarts = 0
    const plugin = createServerPlugin({
      workerPool() { workerStarts++; throw new Error('new adapter must stay disabled beside legacy') },
      actionMatcher: () => ({ async match() { return [] }, async matchDetailed() { return { status: 'ok', blocks: [] } }, async dispose() {}, diagnostics: [] }),
    })
    const hooks = await plugin({ directory: project, worktree: project, client: { config: { async get() { throw new Error('explicit options must avoid host lookup') } } } } as never,
      { python: 'synthetic-python', tools: project, vault: project, shell: 'powershell' } as never)
    assert.equal(workerStarts, 0)
    await hooks.dispose?.()
  } finally { await rm(project, { recursive: true, force: true }) }
})
