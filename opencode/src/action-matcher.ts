import { spawn, type ChildProcess } from 'node:child_process'
import { mkdtemp, readFile, rm, stat, writeFile } from 'node:fs/promises'
import { join } from 'node:path'
import { tmpdir } from 'node:os'
import type { ActionDescriptor, BridgeResources, ContextBlock } from './protocol.ts'

export interface MatcherResources {
  grepExe: string | null
  actionRulesFile: string
}

export interface ActionMatchResult {
  status: 'ok' | 'busy' | 'degraded'
  blocks: ContextBlock[]
}

export interface ActionMatcher {
  match(command: string, shell: 'bash' | 'powershell', descriptors: ActionDescriptor[], capabilityRevision: number): Promise<ContextBlock[]>
  matchDetailed(command: string, shell: 'bash' | 'powershell', descriptors: ActionDescriptor[], capabilityRevision: number): Promise<ActionMatchResult>
  dispose(): Promise<void>
  readonly diagnostics: string[]
}

type SpawnGrep = typeof spawn
const MAX_COMMAND_BYTES = 16 * 1024
const MAX_TSV_BYTES = 1024 * 1024
const MAX_ROWS = 100
const MATCH_BUDGET_MS = 1_000

interface RuleRow { shell: string; id: string; pattern: string; hint: string }

function parseRows(source: string): RuleRow[] {
  const rows: RuleRow[] = []
  for (const line of source.split(/\r?\n/)) {
    if (!line) continue
    const [shell, id, pattern, ...hintParts] = line.split('\t')
    if (!shell || !id || !pattern || hintParts.length === 0) continue
    let hint: string
    try { hint = JSON.parse(`"${hintParts.join('\t')}"`) as string } catch { continue }
    rows.push({ shell, id, pattern, hint })
    if (rows.length >= MAX_ROWS) break
  }
  return rows
}

function sameRule(descriptor: ActionDescriptor, row: RuleRow): boolean {
  return descriptor.id === row.id && descriptor.shell === row.shell && descriptor.pattern === row.pattern && descriptor.hint === row.hint
}

export function createActionMatcher(resources: MatcherResources | BridgeResources, spawnGrep: SpawnGrep = spawn): ActionMatcher {
  const children = new Set<ChildProcess>()
  const closePromises = new Map<ChildProcess, Promise<void>>()
  const escalations = new Map<ChildProcess, ReturnType<typeof setTimeout>>()
  const diagnostics: string[] = []
  let disposed = false
  let active = false
  let disposePromise: Promise<void> | undefined

  const addDiagnostic = (message: string) => { if (!diagnostics.includes(message)) diagnostics.push(message) }

  function kill(child: ChildProcess, signal?: NodeJS.Signals): void {
    if (!children.has(child)) return
    try {
      if (!child.kill(signal)) addDiagnostic('grep-kill-failed')
    } catch {
      addDiagnostic('grep-kill-failed')
    }
  }

  function scheduleEscalation(child: ChildProcess): void {
    if (escalations.has(child)) return
    const timer = setTimeout(() => {
      escalations.delete(child)
      if (children.has(child)) kill(child, 'SIGKILL')
    }, 100)
    timer.unref?.()
    escalations.set(child, timer)
  }

  async function run(pattern: string, command: string, deadline: number, patternFile = false): Promise<boolean | null> {
    if (disposed || !resources.grepExe) return null
    const remaining = deadline - Date.now()
    if (remaining <= 0) return null
    let child: ChildProcess
    try {
      // The command is exclusively stdin. shell:false ensures it cannot execute.
      child = spawnGrep(resources.grepExe, patternFile ? ['-E', '-q', '-f', pattern] : ['-E', '-q', '-e', pattern], {
        shell: false, windowsHide: true, stdio: ['pipe', 'ignore', 'ignore'],
      })
    } catch {
      return null
    }
    children.add(child)
    let resolveClose!: (code: number | null) => void
    const closed = new Promise<number | null>((resolve) => { resolveClose = resolve })
    let outcomeResolved = false
    let resolveOutcome!: (result: boolean | null) => void
    const outcome = new Promise<boolean | null>((resolve) => { resolveOutcome = resolve })
    const settle = (result: boolean | null) => {
      if (outcomeResolved) return
      outcomeResolved = true
      resolveOutcome(result)
    }
    const closeWait = closed.then(() => undefined)
    closePromises.set(child, closeWait)
    child.once('error', () => {
      addDiagnostic('grep-process-error')
      settle(null)
      kill(child)
      scheduleEscalation(child)
    })
    child.stdin?.on('error', () => {
      addDiagnostic('grep-stdin-error')
      settle(null)
      kill(child)
      scheduleEscalation(child)
    })
    child.once('close', (code) => {
      const closeCode = typeof code === 'number' ? code : null
      const escalation = escalations.get(child)
      if (escalation) clearTimeout(escalation)
      escalations.delete(child)
      children.delete(child)
      closePromises.delete(child)
      resolveClose(closeCode)
      settle(closeCode === 0 ? true : closeCode === 1 ? false : null)
    })
    try { child.stdin?.end(command, 'utf8') }
    catch {
      addDiagnostic('grep-stdin-error')
      settle(null)
      kill(child)
      scheduleEscalation(child)
    }
    const timeout = Math.max(0, deadline - Date.now())
    const timer = setTimeout(() => {
      addDiagnostic('grep-timeout')
      settle(null)
      kill(child)
      scheduleEscalation(child)
    }, timeout)
    const result = await outcome
    clearTimeout(timer)
    // `error`/EPIPE/timeout settle the matching result but do not release the
    // process handle. Ownership ends only when ChildProcess emits `close`.
    return result
  }

  async function matchDetailed(command: string, shell: 'bash' | 'powershell', descriptors: ActionDescriptor[], revision: number): Promise<ActionMatchResult> {
    if (disposed) return { status: 'degraded', blocks: [] }
    if (active || children.size > 0) { addDiagnostic('action-match-busy'); return { status: 'busy', blocks: [] } }
    active = true
    try {
      if (!resources.grepExe) { addDiagnostic('grep-unavailable'); return { status: 'degraded', blocks: [] } }
      if (typeof command !== 'string' || !Array.isArray(descriptors) || !Number.isSafeInteger(revision) || revision < 0) {
        addDiagnostic('action-input-invalid')
        return { status: 'degraded', blocks: [] }
      }
      if (Buffer.byteLength(command, 'utf8') > MAX_COMMAND_BYTES) { addDiagnostic('command-oversize'); return { status: 'degraded', blocks: [] } }
      let source: string
      try {
        const artifactStat = await stat(resources.actionRulesFile)
        if (!artifactStat.isFile() || artifactStat.size > MAX_TSV_BYTES) { addDiagnostic('action-artifact-invalid'); return { status: 'degraded', blocks: [] } }
        source = await readFile(resources.actionRulesFile, 'utf8')
      } catch { addDiagnostic('action-artifact-missing'); return { status: 'degraded', blocks: [] } }
      const rows = parseRows(source)
      if (rows.length === 0) { addDiagnostic('action-artifact-empty-or-stale'); return { status: 'degraded', blocks: [] } }
      const forShell = descriptors.filter((item) => item.shell === shell)
      const relevant = descriptors.filter((item) => item.shell === shell && item.capabilityRevision === revision)
      if (forShell.length > 0 && relevant.length === 0) { addDiagnostic('action-descriptors-stale'); return { status: 'degraded', blocks: [] } }
      if (!relevant.length) return { status: 'ok', blocks: [] }
      const selected = rows.filter((row) => row.shell === shell && relevant.some((item) => sameRule(item, row)))
      if (!selected.length || relevant.some((item) => !selected.some((row) => sameRule(item, row)))) {
        addDiagnostic('action-artifact-stale')
        return { status: 'degraded', blocks: [] }
      }
      const deadline = Date.now() + MATCH_BUDGET_MS
      const hits: ContextBlock[] = []
      const patternDir = await mkdtemp(join(tmpdir(), 'sb-opencode-ere-'))
      const patternFile = join(patternDir, 'selected.re')
      let degraded = false
      try {
        try { await writeFile(patternFile, selected.map((row) => row.pattern).join('\n') + '\n', { encoding: 'utf8', mode: 0o600 }) }
        catch { addDiagnostic('action-pattern-file-error'); return { status: 'degraded', blocks: [] } }
        const fast = await run(patternFile, command, deadline, true)
        if (fast === null) { addDiagnostic('action-grep-error-or-timeout'); return { status: 'degraded', blocks: [] } }
        if (!fast) return { status: 'ok', blocks: [] }
        for (const row of selected) {
          if (Date.now() >= deadline) { addDiagnostic('action-match-timeout'); degraded = true; break }
          try { await writeFile(patternFile, `${row.pattern}\n`, { encoding: 'utf8', mode: 0o600 }) }
          catch { addDiagnostic('action-pattern-file-error'); degraded = true; break }
          const result = await run(patternFile, command, deadline, true)
          if (result === null) { addDiagnostic('action-grep-error-or-timeout'); degraded = true; break }
          if (!result) continue
          const descriptor = relevant.find((item) => sameRule(item, row))
          if (!descriptor) continue
          hits.push({
            id: `action:${row.id}`, text: row.hint, priority: descriptor.priority,
            category: 'action_rule', capabilityRevision: revision,
          })
        }
        return { status: degraded ? 'degraded' : 'ok', blocks: hits.sort((a, b) => b.priority - a.priority || a.id.localeCompare(b.id)).slice(0, 5) }
      } finally {
        await rm(patternDir, { recursive: true, force: true })
      }
    } catch {
      addDiagnostic('action-match-error')
      return { status: 'degraded', blocks: [] }
    } finally { active = false }
  }

  async function match(command: string, shell: 'bash' | 'powershell', descriptors: ActionDescriptor[], revision: number): Promise<ContextBlock[]> {
    return (await matchDetailed(command, shell, descriptors, revision)).blocks
  }

  async function dispose(): Promise<void> {
    if (disposePromise) return disposePromise
    disposed = true
    disposePromise = (async () => {
      for (const child of children) kill(child)
      const graceful = Promise.all([...closePromises.values()])
      await Promise.race([graceful, new Promise<void>((resolve) => setTimeout(resolve, 100))])
      for (const child of children) kill(child, 'SIGKILL')
      await Promise.all([...closePromises.values()])
    })()
    return disposePromise
  }

  return { match, matchDetailed, dispose, diagnostics }
}
