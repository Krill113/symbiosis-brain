import { spawn, type ChildProcess } from 'node:child_process'
import { randomUUID } from 'node:crypto'
import type { Readable, Writable } from 'node:stream'
import { parseWorkerFrame, type ContextRequest, type ContextResult, type Operation, type WorkerFrame } from './protocol.ts'

const REQUEST_DEADLINE_MS = 5_000
const DISPOSE_GRACE_MS = 250
const RESTART_BACKOFF_MS = 100

export interface WorkerProcess {
  stdin: Writable | null
  stdout: Readable | null
  kill(signal?: NodeJS.Signals): boolean
  on(event: 'error', listener: (error: Error) => void): this
  on(event: 'close', listener: (code: number | null, signal: NodeJS.Signals | null) => void): this
}

export interface WorkerPoolSettings {
  pythonExe: string
  packageDir: string
  vault: string
  cwd?: string
  env?: NodeJS.ProcessEnv
  deadlineMs?: number
  warmupTimeoutMs?: number
  maxPendingPerSlot?: number
  restartBackoffMaxMs?: number
}

export type SpawnWorker = (role: 'control' | 'retrieval') => WorkerProcess

export interface WorkerPool {
  request(req: ContextRequest, signal?: AbortSignal): Promise<ContextResult>
  dispose(): Promise<void>
}

type Role = 'control' | 'retrieval'
interface Pending {
  req: ContextRequest
  resolve: (result: ContextResult) => void
  reject: (error: Error) => void
  signal?: AbortSignal
  abort?: () => void
  timer: ReturnType<typeof setTimeout>
  sent: boolean
  slot: Slot
  external: boolean
  settled: boolean
}
interface Slot {
  role: Role
  child?: WorkerProcess
  buffer: string
  hello: boolean
  ready: boolean
  busy?: Pending
  queue: Pending[]
  disposed: boolean
  restarting: boolean
  handshakeTimer?: ReturnType<typeof setTimeout>
  restartAttempts: number
  restartDelayMs: number
  externalPending: number
}

function roleFor(operation: Operation): Role {
  return operation === 'recall' || operation === 'warmup' ? 'retrieval' : 'control'
}

function degraded(req: ContextRequest, reason: string): ContextResult {
  return {
    version: 1, kind: 'result', requestId: req.requestId, sessionId: req.sessionId,
    generation: req.generation, capabilityRevision: req.capabilityRevision,
    status: 'degraded', scope: req.scope ?? null, blocks: [], diagnostics: [reason],
  }
}

function makeSpawn(settings: WorkerPoolSettings): SpawnWorker {
  return (role) => spawn(settings.pythonExe, [
    '-m', 'symbiosis_brain.adapters.opencode', '--worker', '--role', role, '--vault', settings.vault,
  ], {
    cwd: settings.packageDir,
    env: { ...process.env, ...settings.env },
    stdio: ['pipe', 'pipe', 'ignore'],
    windowsHide: true,
  }) as WorkerProcess
}

export function createWorkerPool(settings: WorkerPoolSettings, spawnWorker: SpawnWorker = makeSpawn(settings)): WorkerPool {
  const slots: Record<Role, Slot> = {
    control: { role: 'control', buffer: '', hello: false, ready: true, queue: [], disposed: false, restarting: false, restartAttempts: 0, restartDelayMs: RESTART_BACKOFF_MS, externalPending: 0 },
    retrieval: { role: 'retrieval', buffer: '', hello: false, ready: false, queue: [], disposed: false, restarting: false, restartAttempts: 0, restartDelayMs: RESTART_BACKOFF_MS, externalPending: 0 },
  }
  let disposed = false
  let disposePromise: Promise<void> | undefined
  const deadlineMs = settings.deadlineMs ?? REQUEST_DEADLINE_MS
  const warmupTimeoutMs = Number.isSafeInteger(settings.warmupTimeoutMs)
    && settings.warmupTimeoutMs! >= 1 && settings.warmupTimeoutMs! <= 30_000
    ? settings.warmupTimeoutMs! : 30_000
  const maxPendingPerSlot = Number.isSafeInteger(settings.maxPendingPerSlot)
    && settings.maxPendingPerSlot! >= 1 && settings.maxPendingPerSlot! <= 1024
    ? settings.maxPendingPerSlot! : 32
  const restartBackoffMaxMs = Number.isSafeInteger(settings.restartBackoffMaxMs)
    && settings.restartBackoffMaxMs! >= RESTART_BACKOFF_MS && settings.restartBackoffMaxMs! <= 5_000
    ? settings.restartBackoffMaxMs! : 5_000
  const lifecycleTimers = new Set<ReturnType<typeof setTimeout>>()

  function scheduleTimer(callback: () => void, delay: number): ReturnType<typeof setTimeout> {
    let timer!: ReturnType<typeof setTimeout>
    timer = setTimeout(() => {
      lifecycleTimers.delete(timer)
      if (!disposed) callback()
    }, delay)
    lifecycleTimers.add(timer)
    timer.unref?.()
    return timer
  }

  function cancelTimer(timer: ReturnType<typeof setTimeout> | undefined): void {
    if (!timer) return
    clearTimeout(timer)
    lifecycleTimers.delete(timer)
  }

  function cleanupPending(pending: Pending): void {
    if (pending.settled) return
    pending.settled = true
    cancelTimer(pending.timer)
    if (pending.abort) pending.signal?.removeEventListener('abort', pending.abort)
    if (pending.external) pending.slot.externalPending -= 1
  }

  function settle(pending: Pending, result: ContextResult): void {
    cleanupPending(pending)
    pending.resolve(result)
  }

  function fail(pending: Pending, error: Error): void {
    cleanupPending(pending)
    pending.reject(error)
  }

  function schedule(slot: Slot): void {
    if (disposed || slot.disposed || slot.busy || !slot.hello) return
    while (slot.queue.length) {
      if (!slot.ready && slot.queue[0]!.req.operation !== 'warmup') return
      const next = slot.queue.shift()!
      if (next.signal?.aborted) {
        fail(next, new Error('worker request aborted'))
        continue
      }
      const child = slot.child
      if (!child?.stdin || !child.stdin.writable) {
        settle(next, degraded(next.req, 'worker_lost'))
        restart(slot)
        return
      }
      slot.busy = next
      next.sent = true
      try {
        child.stdin.write(`${JSON.stringify(next.req)}\n`, 'utf8', (error) => {
          if (error && slot.child === child && !disposed && !slot.disposed) workerLost(slot)
        })
      } catch {
        if (slot.child === child && !disposed && !slot.disposed) workerLost(slot)
      }
      return
    }
  }

  function restart(slot: Slot): void {
    if (disposed || slot.disposed || slot.restarting) return
    slot.restarting = true
    slot.restartAttempts += 1
    slot.restartDelayMs = Math.min(RESTART_BACKOFF_MS * (2 ** Math.min(slot.restartAttempts - 1, 6)), restartBackoffMaxMs)
    if (slot.busy) {
      settle(slot.busy, degraded(slot.busy.req, 'worker_lost'))
      slot.busy = undefined
    }
    const old = slot.child
    slot.hello = false
    slot.ready = slot.role === 'control'
    if (old) {
      try { old.stdin?.end() } catch { /* owned handle cleanup continues */ }
      try { old.kill('SIGTERM') } catch { /* owned handle cleanup continues */ }
      scheduleTimer(() => {
        if (slot.child !== old || disposed || slot.disposed) return
        try { old.kill('SIGKILL') } catch { /* retain ownership until close */ }
      }, 100)
      // The close handler is the ownership boundary. Never overlap a replacement
      // child with an old process that has not closed yet.
      return
    }
    scheduleTimer(() => {
      slot.restarting = false
      if (!disposed && !slot.disposed) start(slot)
    }, slot.restartDelayMs)
  }

  function workerLost(slot: Slot): void {
    if (disposed || slot.disposed) return
    const current = slot.busy
    slot.busy = undefined
    if (current) settle(current, degraded(current.req, 'worker_lost'))
    for (const queued of slot.queue.splice(0)) settle(queued, degraded(queued.req, 'worker_lost'))
    restart(slot)
  }

  function onFrame(slot: Slot, child: WorkerProcess, raw: string): void {
    if (disposed || slot.disposed || slot.restarting || slot.child !== child) return
    let frame: WorkerFrame
    try { frame = parseWorkerFrame(raw, slot.busy?.req.operation) } catch {
      const current = slot.busy
      if (current) {
        slot.busy = undefined
        settle(current, degraded(current.req, 'invalid-worker-reply'))
      }
      restart(slot)
      return
    }
    if (!slot.hello) {
      if (frame.kind !== 'hello' || frame.role !== slot.role) { restart(slot); return }
      slot.hello = true
      cancelTimer(slot.handshakeTimer)
      slot.handshakeTimer = undefined
      if (slot.role === 'control') {
        slot.ready = true
        schedule(slot)
      } else {
        // WorkerHello acknowledges transport only. Require a successful explicit
        // warmup result before dispatching retrieval jobs.
        const warmup = makeWarmup()
        void enqueue(slot, warmup, undefined, warmupTimeoutMs, true).then((frame) => {
          if (frame.status !== 'ok' && !disposed) restart(slot)
        }).catch(() => { if (!disposed) restart(slot) })
      }
      return
    }
    if (frame.kind !== 'result') {
      restart(slot)
      return
    }
    const current = slot.busy
    if (!current || frame.requestId !== current.req.requestId
      || frame.sessionId !== current.req.sessionId || frame.generation !== current.req.generation
      || frame.capabilityRevision !== current.req.capabilityRevision) {
      restart(slot)
      return
    }
    slot.busy = undefined
    if (frame.status === 'ok') {
      slot.restartAttempts = 0
      slot.restartDelayMs = RESTART_BACKOFF_MS
      if (slot.role === 'retrieval' && current.req.operation === 'warmup') slot.ready = true
    }
    settle(current, frame)
    schedule(slot)
  }

  function enqueue(slot: Slot, req: ContextRequest, signal?: AbortSignal, timeoutMs = deadlineMs, priority = false): Promise<ContextResult> {
    if (disposed || slot.disposed) return Promise.reject(new Error('worker pool disposed'))
    if (!priority && slot.externalPending >= maxPendingPerSlot) {
      return Promise.resolve(degraded(req, 'queue_full'))
    }
    if (!priority) slot.externalPending += 1
    return new Promise<ContextResult>((resolve, reject) => {
      const pending: Pending = {
        req, resolve, reject, signal, sent: false, slot, external: !priority, settled: false,
        timer: undefined as unknown as ReturnType<typeof setTimeout>,
      }
      pending.timer = scheduleTimer(() => {
          const index = slot.queue.indexOf(pending)
          if (index >= 0) {
            slot.queue.splice(index, 1)
            settle(pending, degraded(req, req.operation === 'warmup' ? 'worker_warmup_timeout' : 'worker_timeout'))
          } else if (slot.busy === pending) {
            slot.busy = undefined
            settle(pending, degraded(req, req.operation === 'warmup' ? 'worker_warmup_timeout' : 'worker_timeout'))
            restart(slot)
          }
        }, timeoutMs)
      if (signal) {
        pending.abort = () => {
          const index = slot.queue.indexOf(pending)
          if (index >= 0) slot.queue.splice(index, 1)
          if (slot.busy === pending) {
            slot.busy = undefined
            restart(slot)
          }
          fail(pending, new Error('worker request aborted'))
        }
        if (signal.aborted) { pending.abort(); return }
        signal.addEventListener('abort', pending.abort, { once: true })
      }
      if (priority) slot.queue.unshift(pending)
      else slot.queue.push(pending)
      schedule(slot)
    })
  }

  function start(slot: Slot): void {
    if (disposed || slot.disposed) return
    let child: WorkerProcess
    try { child = spawnWorker(slot.role) } catch {
      restart(slot)
      return
    }
    slot.child = child
    slot.buffer = ''
    slot.hello = false
    slot.ready = slot.role === 'control'
    const lostCurrentChild = () => {
      if (!disposed && !slot.disposed && slot.child === child && !slot.restarting) workerLost(slot)
    }
    child.stdout?.setEncoding('utf8')
    child.stdout?.on('data', (chunk: string | Buffer) => {
      if (disposed || slot.disposed || slot.child !== child || slot.restarting) return
      slot.buffer += chunk.toString()
      if (Buffer.byteLength(slot.buffer, 'utf8') > 64 * 1024 && !slot.buffer.includes('\n')) {
        lostCurrentChild()
        return
      }
      while (true) {
        const newline = slot.buffer.indexOf('\n')
        if (newline < 0) break
        const line = slot.buffer.slice(0, newline)
        slot.buffer = slot.buffer.slice(newline + 1)
        onFrame(slot, child, line)
      }
    })
    child.stdin?.on('error', lostCurrentChild)
    child.stdout?.on('error', lostCurrentChild)
    child.on('error', lostCurrentChild)
    child.on('close', () => {
      cancelTimer(slot.handshakeTimer)
      if (slot.handshakeTimer && slot.child === child) slot.handshakeTimer = undefined
      if (disposed || slot.disposed || slot.child !== child) return
      if (slot.child === child && slot.restarting && !disposed && !slot.disposed) {
        slot.child = undefined
        scheduleTimer(() => {
          slot.restarting = false
          start(slot)
        }, slot.restartDelayMs)
      } else if (!disposed && slot.child === child) {
        slot.child = undefined
        workerLost(slot)
      }
    })
    slot.handshakeTimer = scheduleTimer(() => {
      if (!slot.hello && slot.child === child) workerLost(slot)
    }, slot.role === 'control' ? deadlineMs : 30_000)
  }

  function makeWarmup(): ContextRequest {
    const cwd = settings.cwd ?? settings.packageDir
    return {
      version: 1,
      requestId: `warmup-${randomUUID()}`, sessionId: `worker-${randomUUID()}`,
      generation: 0, operation: 'warmup', cwd, origin: 'resume', capabilityRevision: 0,
      capabilities: { tools: [], mcp: [], skills: [] },
    }
  }

  start(slots.control)
  start(slots.retrieval)

  return {
    request(req, signal) {
      if (disposed) return Promise.reject(new Error('worker pool disposed'))
      return enqueue(slots[roleFor(req.operation)], req, signal)
    },
    async dispose() {
      if (disposePromise) return disposePromise
      disposed = true
      for (const timer of lifecycleTimers) clearTimeout(timer)
      lifecycleTimers.clear()
      disposePromise = (async () => {
        const waits: Promise<void>[] = []
        for (const slot of Object.values(slots)) {
          slot.disposed = true
          for (const item of slot.queue.splice(0)) fail(item, new Error('worker pool disposed'))
          if (slot.busy) {
            fail(slot.busy, new Error('worker pool disposed'))
            slot.busy = undefined
          }
          const child = slot.child
          if (slot.handshakeTimer) cancelTimer(slot.handshakeTimer)
          slot.handshakeTimer = undefined
          if (!child) continue
          waits.push(new Promise<void>((resolve) => {
            let done = false
            let timer: ReturnType<typeof setTimeout>
            let finalTimer: ReturnType<typeof setTimeout> | undefined
            const finish = () => {
              if (done) return
              done = true
              clearTimeout(timer)
              if (finalTimer) clearTimeout(finalTimer)
              resolve()
            }
            child.on('close', finish)
            timer = setTimeout(() => {
              try { child.kill('SIGKILL') } catch { /* ownership is retained until close */ }
              finalTimer = setTimeout(finish, DISPOSE_GRACE_MS)
              finalTimer.unref?.()
            }, DISPOSE_GRACE_MS)
            try { child.stdin?.end() } catch { /* still terminate owned process */ }
            try { child.kill('SIGTERM') } catch { finish() }
            timer.unref?.()
          }))
        }
        await Promise.all(waits)
      })()
      return disposePromise
    },
  }
}
