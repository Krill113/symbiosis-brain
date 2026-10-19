import assert from 'node:assert/strict'
import { EventEmitter } from 'node:events'
import { PassThrough, Writable } from 'node:stream'
import test from 'node:test'
import { createWorkerPool, type WorkerProcess } from '../src/worker-pool.ts'
import type { ContextRequest } from '../src/protocol.ts'

class FakeWorker extends EventEmitter implements WorkerProcess {
  stdin: Writable = new PassThrough()
  stdout = new PassThrough()
  signals: (NodeJS.Signals | undefined)[] = []
  readonly role: 'control' | 'retrieval'
  readonly writes: string[] = []
  readonly ignoreTerm: boolean
  readonly emitHello: boolean
  respond: (request: Record<string, any>) => void

  constructor(role: 'control' | 'retrieval', respond?: (request: Record<string, any>) => void, ignoreTerm = false, emitHello = true) {
    super()
    this.role = role
    this.ignoreTerm = ignoreTerm
    this.emitHello = emitHello
    this.respond = respond ?? ((req) => this.reply(req))
    this.stdin.on('data', (chunk) => {
      const raw = chunk.toString()
      this.writes.push(raw)
      for (const line of raw.split('\n').filter(Boolean)) this.respond(JSON.parse(line))
    })
    if (emitHello) queueMicrotask(() => this.stdout.write(`${JSON.stringify({ version: 1, kind: 'hello', ready: true, role })}\n`))
  }

  reply(req: Record<string, any>, status: 'ok' | 'degraded' = 'ok'): void {
    this.stdout.write(`${JSON.stringify({
      version: 1, kind: 'result', requestId: req.requestId, sessionId: req.sessionId,
      generation: req.generation, capabilityRevision: req.capabilityRevision,
      status, scope: req.scope ?? null,
      blocks: req.operation === 'bootstrap'
        ? [{ id: `bootstrap:${req.sessionId}`, text: `Synthetic bootstrap for ${req.sessionId}`, priority: 90, category: 'bootstrap' }]
        : [],
      diagnostics: [],
    })}\n`)
  }

  kill(signal?: NodeJS.Signals): boolean {
    this.signals.push(signal)
    if (signal === 'SIGTERM' && this.ignoreTerm) return true
    setImmediate(() => this.emit('close', null, signal ?? null))
    return true
  }
}

function request(requestId: string, operation: ContextRequest['operation'] = 'bootstrap', sessionId = requestId): ContextRequest {
  return {
    version: 1, requestId, sessionId, generation: 2, operation,
    cwd: '/synthetic/project', boundary: '/synthetic', scope: 'sample',
    origin: 'user', capabilityRevision: 4,
    capabilities: { tools: [], mcp: [], skills: [] },
  }
}

test('starts one control and one retrieval worker; retrieval waits for warmup', async () => {
  const workers: FakeWorker[] = []
  const pool = createWorkerPool({ pythonExe: 'python', packageDir: '.', vault: '/synthetic/vault', cwd: '/synthetic/project' }, (role) => {
    const worker = new FakeWorker(role)
    workers.push(worker)
    return worker
  })
  const [bootstrap, recall] = await Promise.all([
    pool.request(request('b1', 'bootstrap', 'session-one')),
    pool.request(request('r1', 'recall', 'session-two')),
  ])
  assert.equal(bootstrap.sessionId, 'session-one')
  assert.equal(recall.sessionId, 'session-two')
  const retrieval = workers.find((worker) => worker.role === 'retrieval')!
  assert.equal(retrieval.writes.length, 2)
  assert.equal(JSON.parse(retrieval.writes[0]!.trim()).operation, 'warmup')
  await pool.dispose()
  assert.equal(workers.length, 2)
})

test('two successful bootstraps complete within budget while retrieval warmup result latch is closed', async () => {
  const workers: FakeWorker[] = []
  let releaseWarmup!: () => void
  let markWarmupStarted!: () => void
  let warmupReleased = false
  const warmupStarted = new Promise<void>((resolve) => { markWarmupStarted = resolve })
  const warmupLatch = new Promise<void>((resolve) => { releaseWarmup = resolve })
  const pool = createWorkerPool({
    pythonExe: 'python', packageDir: '.', vault: '/synthetic/vault',
    deadlineMs: 1_500, warmupTimeoutMs: 30_000,
  }, (role) => {
    let worker!: FakeWorker
    worker = new FakeWorker(role, (req) => {
      if (req.operation === 'warmup') {
        markWarmupStarted()
        void warmupLatch.then(() => {
          warmupReleased = true
          worker.reply(req)
        })
        return
      }
      worker.reply(req)
    })
    workers.push(worker)
    return worker
  })
  const started = Date.now()
  const bootstrapPromise = Promise.all([
    pool.request(request('first-bootstrap', 'bootstrap', 'session-first')),
    pool.request(request('second-bootstrap', 'bootstrap', 'session-second')),
  ])
  await warmupStarted
  const results = await bootstrapPromise
  assert.ok(Date.now() - started < 1_500)
  assert.equal(warmupReleased, false, 'bootstrap oracle must observe warmup result still withheld')
  assert.deepEqual(results.map((result) => result.sessionId), ['session-first', 'session-second'])
  assert.ok(results.every((result) => result.status === 'ok'))
  assert.deepEqual(results.map((result) => result.blocks[0]?.text), [
    'Synthetic bootstrap for session-first', 'Synthetic bootstrap for session-second',
  ])
  assert.equal(workers.find((worker) => worker.role === 'retrieval')!.writes.length, 1)
  releaseWarmup()
  await warmupLatch
  assert.equal((await pool.request(request('retrieval-ready', 'recall'))).status, 'ok')
  await pool.dispose()
})

test('negative control: bootstrap path held behind the closed retrieval latch fails the independence deadline', async () => {
  let releaseWarmup!: () => void
  let warmupStarted!: () => void
  const warmupGate = new Promise<void>((resolve) => { releaseWarmup = resolve })
  const startedGate = new Promise<void>((resolve) => { warmupStarted = resolve })
  const pool = createWorkerPool({ pythonExe: 'python', packageDir: '.', vault: '/synthetic/vault', deadlineMs: 1_500 }, (role) => {
    let worker!: FakeWorker
    worker = new FakeWorker(role, (req) => {
      if (req.operation === 'warmup') {
        warmupStarted()
        return
      }
      if (req.operation === 'bootstrap') {
        void warmupGate.then(() => worker.reply(req))
        return
      }
      worker.reply(req)
    })
    return worker
  })
  try {
    const bootstrap = pool.request(request('forbidden-dependent-bootstrap'))
    await startedGate
    const oracle = await Promise.race([
      bootstrap.then(() => 'bootstrap-returned'),
      new Promise<'deadline'>(resolve => setTimeout(() => resolve('deadline'), 30)),
    ])
    assert.equal(oracle, 'deadline', 'the same closed-latch oracle must reject a worker held behind warmup')
  } finally {
    releaseWarmup()
    await pool.dispose()
  }
})

test('scaled 30-second warmup cutoff restarts retrieval; readiness waits for a successful retry', async () => {
  const retrievalWorkers: FakeWorker[] = []
  const pool = createWorkerPool({
    pythonExe: 'python', packageDir: '.', vault: '/synthetic/vault',
    // 1000x accelerated unit test; the production default remains 30_000ms.
    deadlineMs: 2_000, warmupTimeoutMs: 30,
  }, (role) => {
    let worker!: FakeWorker
    worker = new FakeWorker(role, (req) => {
      if (req.operation === 'warmup') {
        if (retrievalWorkers.length > 1) worker.reply(req)
      } else worker.reply(req)
    })
    if (role === 'retrieval') retrievalWorkers.push(worker)
    return worker
  })
  const started = Date.now()
  const result = await pool.request(request('wait-for-ready', 'recall'))
  assert.equal(result.status, 'ok')
  assert.equal(retrievalWorkers.length, 2)
  assert.ok(Date.now() - started >= 120, 'timing must include the scaled warmup timeout and backoff')
  await pool.dispose()
})

test('active timeout kills only owned child and returns a degraded worker_timeout result', async () => {
  let control!: FakeWorker
  const pool = createWorkerPool({ pythonExe: 'python', packageDir: '.', vault: '/synthetic/vault', deadlineMs: 20 }, (role) => {
    const worker = new FakeWorker(role, (req) => { if (req.operation !== 'warmup') return })
    if (role === 'control') control = worker
    return worker
  })
  const result = await pool.request(request('stuck'))
  assert.equal(result.status, 'degraded')
  assert.deepEqual(result.diagnostics, ['worker_timeout'])
  assert.ok(control.signals.includes('SIGTERM'))
  await pool.dispose()
})

test('dispose rejects queued and inflight work and escalates owned process cleanup', async () => {
  const workers: FakeWorker[] = []
  const pool = createWorkerPool({ pythonExe: 'python', packageDir: '.', vault: '/synthetic/vault', deadlineMs: 10_000 }, (role) => {
    const worker = new FakeWorker(role, () => {}, true)
    workers.push(worker)
    return worker
  })
  const first = pool.request(request('active'))
  const second = pool.request(request('queued'))
  await new Promise((resolve) => setTimeout(resolve, 0))
  const disposed = pool.dispose()
  await assert.rejects(first, /disposed/)
  await assert.rejects(second, /disposed/)
  await disposed
  assert.ok(workers.every((worker) => worker.signals.includes('SIGTERM')))
  assert.ok(workers.every((worker) => worker.signals.includes('SIGKILL')))
})

test('invalid replies fail open and a foreign reply cannot satisfy another request', async () => {
  const pool = createWorkerPool({ pythonExe: 'python', packageDir: '.', vault: '/synthetic/vault', deadlineMs: 100 }, (role) =>
    (() => {
      let worker!: FakeWorker
      worker = new FakeWorker(role, (req) => {
        if (req.operation === 'warmup') return worker.reply(req)
        worker.reply({ ...req, requestId: 'foreign-id' })
      })
      return worker
    })()
  )
  const result = await pool.request(request('valid'))
  assert.equal(result.status, 'degraded')
  assert.deepEqual(result.diagnostics, ['worker_lost'])
  await pool.dispose()
})

test('malformed reply is rejected without exposing worker output', async () => {
  const pool = createWorkerPool({ pythonExe: 'python', packageDir: '.', vault: '/synthetic/vault', deadlineMs: 100 }, (role) => {
    let worker!: FakeWorker
    worker = new FakeWorker(role, (req) => {
      if (req.operation === 'warmup') worker.reply(req)
      else worker.stdout.write('not-json\n')
    })
    return worker
  })
  const result = await pool.request(request('malformed'))
  assert.equal(result.status, 'degraded')
  assert.deepEqual(result.diagnostics, ['invalid-worker-reply'])
  assert.equal(JSON.stringify(result).includes('not-json'), false)
  await pool.dispose()
})

test('owned stdin EPIPE callback and emitted error are consumed; request returns worker_lost', async () => {
  let control!: FakeWorker
  const pool = createWorkerPool({ pythonExe: 'python', packageDir: '.', vault: '/synthetic/vault', deadlineMs: 100 }, (role) => {
    const worker = new FakeWorker(role)
    if (role === 'control') {
      control = worker
      worker.stdin = new Writable({ write(_chunk, _encoding, callback) {
        setImmediate(() => callback(Object.assign(new Error('synthetic EPIPE'), { code: 'EPIPE' })))
      } })
    }
    return worker
  })
  const result = await pool.request(request('stdin-epipe'))
  assert.deepEqual(result.diagnostics, ['worker_lost'])
  assert.ok(control.signals.includes('SIGTERM'))
  await pool.dispose()
})

test('owned stdout error is consumed and fails the active request without exposing stderr', async () => {
  let control!: FakeWorker
  const pool = createWorkerPool({ pythonExe: 'python', packageDir: '.', vault: '/synthetic/vault', deadlineMs: 100 }, (role) => {
    const worker = new FakeWorker(role, (req) => { if (req.operation !== 'warmup') return })
    if (role === 'control') control = worker
    return worker
  })
  const pending = pool.request(request('stdout-error'))
  setImmediate(() => control.stdout.destroy(new Error('synthetic stdout EPIPE')))
  const result = await pending
  assert.deepEqual(result.diagnostics, ['worker_lost'])
  await pool.dispose()
})

test('dispose before hello cancels handshake state and ignores retired buffered frames', async () => {
  const workers: FakeWorker[] = []
  const scheduled: { timer: ReturnType<typeof setTimeout>; delay: number; cleared: boolean }[] = []
  const originalSetTimeout = globalThis.setTimeout
  const originalClearTimeout = globalThis.clearTimeout
  globalThis.setTimeout = ((callback: TimerHandler, delay?: number, ...args: any[]) => {
    const timer = originalSetTimeout(callback as (...args: any[]) => void, delay, ...args) as ReturnType<typeof setTimeout>
    scheduled.push({ timer, delay: Number(delay ?? 0), cleared: false })
    return timer
  }) as unknown as typeof setTimeout
  globalThis.clearTimeout = ((timer?: ReturnType<typeof setTimeout>) => {
    const found = scheduled.find((item) => item.timer === timer)
    if (found) found.cleared = true
    return originalClearTimeout(timer)
  }) as typeof clearTimeout
  try {
    const pool = createWorkerPool({ pythonExe: 'python', packageDir: '.', vault: '/synthetic/vault', deadlineMs: 20 }, (role) => {
      const worker = new FakeWorker(role, undefined, false, false)
      workers.push(worker)
      return worker
    })
    const dispose = pool.dispose()
    await dispose
    assert.ok(scheduled.some((item) => item.delay === 30_000 && item.cleared))
    assert.ok(scheduled.filter((item) => item.delay !== 250).every((item) => item.cleared))
    for (const worker of workers) worker.stdout.write(`${JSON.stringify({ version: 1, kind: 'hello', ready: true, role: worker.role })}\n`)
    await new Promise((resolve) => setTimeout(resolve, 150))
    await assert.rejects(pool.request(request('after-dispose')), /disposed/)
    assert.equal(workers.length, 2)
    assert.ok(workers.every((worker) => worker.writes.length === 0))
  } finally {
    globalThis.setTimeout = originalSetTimeout
    globalThis.clearTimeout = originalClearTimeout
  }
})

test('degraded warmups retain exponential capped restart backoff until a successful warmup', async () => {
  const retrievalStarts: number[] = []
  let warmups = 0
  const pool = createWorkerPool({
    pythonExe: 'python', packageDir: '.', vault: '/synthetic/vault',
    deadlineMs: 5_000, restartBackoffMaxMs: 250,
  }, (role) => {
    if (role === 'retrieval') retrievalStarts.push(Date.now())
    let worker!: FakeWorker
    worker = new FakeWorker(role, (req) => {
      if (req.operation === 'warmup') {
        warmups += 1
        worker.reply(req, warmups < 4 ? 'degraded' : 'ok')
      } else worker.reply(req)
    })
    return worker
  })
  const result = await pool.request(request('after-warmup', 'recall'))
  assert.equal(result.status, 'ok')
  assert.equal(retrievalStarts.length, 4)
  const gaps = retrievalStarts.slice(1).map((at, index) => at - retrievalStarts[index]!)
  assert.ok(gaps[0]! >= 80 && gaps[0]! < 500, `first retry gap ${gaps[0]}`)
  assert.ok(gaps[1]! >= 160 && gaps[1]! < 700, `second retry gap ${gaps[1]}`)
  assert.ok(gaps[2]! >= 210 && gaps[2]! < 700, `capped retry gap ${gaps[2]}`)
  await pool.dispose()
})

test('per-slot pending limit rejects excess requests immediately, including during cold start', async () => {
  const pool = createWorkerPool({
    pythonExe: 'python', packageDir: '.', vault: '/synthetic/vault',
    maxPendingPerSlot: 1, deadlineMs: 5_000,
  }, (role) => new FakeWorker(role, (req) => { if (req.operation !== 'warmup') return }))
  const activeControl = pool.request(request('control-active'))
  const overflowControl = await pool.request(request('control-overflow'))
  assert.equal(overflowControl.status, 'degraded')
  assert.deepEqual(overflowControl.diagnostics, ['queue_full'])
  const activeRetrieval = pool.request(request('retrieval-active', 'recall'))
  const overflowRetrieval = await pool.request(request('retrieval-overflow', 'recall'))
  assert.equal(overflowRetrieval.status, 'degraded')
  assert.deepEqual(overflowRetrieval.diagnostics, ['queue_full'])
  const disposed = pool.dispose()
  await assert.rejects(activeControl, /disposed/)
  await assert.rejects(activeRetrieval, /disposed/)
  await disposed
})

test('per-slot limit counts an inflight request after the slot is ready', async () => {
  let control!: FakeWorker
  const pool = createWorkerPool({
    pythonExe: 'python', packageDir: '.', vault: '/synthetic/vault',
    maxPendingPerSlot: 1, deadlineMs: 5_000,
  }, (role) => {
    let worker!: FakeWorker
    worker = new FakeWorker(role, (req) => {
      if (req.operation === 'warmup' || req.requestId !== 'inflight') worker.reply(req)
    })
    if (role === 'control') control = worker
    return worker
  })
  assert.equal((await pool.request(request('initial-ready'))).status, 'ok')
  const inflight = pool.request(request('inflight'))
  await new Promise((resolve) => setImmediate(resolve))
  assert.ok(control.writes.some((wire) => wire.includes('"requestId":"inflight"')))
  const overflow = await pool.request(request('overflow-ready'))
  assert.deepEqual(overflow.diagnostics, ['queue_full'])
  const disposing = pool.dispose()
  await assert.rejects(inflight, /disposed/)
  await disposing
})

test('correlated successful resources reply without payload is rejected', async () => {
  const pool = createWorkerPool({ pythonExe: 'python', packageDir: '.', vault: '/synthetic/vault', deadlineMs: 100 }, (role) => {
    let worker!: FakeWorker
    worker = new FakeWorker(role, (req) => {
      if (req.operation === 'warmup') return worker.reply(req)
      worker.stdout.write(`${JSON.stringify({
        version: 1, kind: 'result', requestId: req.requestId, sessionId: req.sessionId,
        generation: req.generation, capabilityRevision: req.capabilityRevision,
        status: 'ok', scope: null, blocks: [], diagnostics: [],
      })}\n`)
    })
    return worker
  })
  const result = await pool.request(request('missing-resources', 'resources'))
  assert.equal(result.status, 'degraded')
  assert.ok(result.diagnostics.includes('invalid-worker-reply') || result.diagnostics.includes('worker_lost'))
  await pool.dispose()
})
