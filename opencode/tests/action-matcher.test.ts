import test from 'node:test'
import assert from 'node:assert/strict'
import { mkdtemp, writeFile, rm } from 'node:fs/promises'
import { EventEmitter } from 'node:events'
import { tmpdir } from 'node:os'
import { delimiter, join, resolve } from 'node:path'
import { existsSync } from 'node:fs'
import { createActionMatcher } from '../src/action-matcher.ts'

function grepPath(): string {
  const entries = (process.env.PATH ?? '').split(delimiter)
  for (const entry of entries) {
    for (const candidate of [join(entry, 'grep.exe'), join(entry, 'grep')]) {
      if (existsSync(candidate)) return resolve(candidate)
    }
    const gitCmd = /[\\/]Git[\\/]cmd$/i.test(entry)
    if (gitCmd) {
      const candidate = join(entry, '..', 'usr', 'bin', 'grep.exe')
      if (existsSync(candidate)) return resolve(candidate)
    }
  }
  throw new Error('No grep executable available for POSIX ERE test')
}

test('uses selected-pattern grep ERE for fast reject and rows; stale shared fast file cannot hide current rules', async () => {
  const dir = await mkdtemp(join(tmpdir(), 'sb-opencode-match-'))
  try {
    const tsv = join(dir, 'action-rules.tsv')
    await writeFile(tsv, 'bash\tdeploy\t^deploy[[:space:]]+prod$\tUse guarded deploy.\n', 'utf8')
    await writeFile(join(dir, 'action-rules.bash.re'), '^never-match-this-command$\n', 'utf8')
    const matcher = createActionMatcher({ grepExe: grepPath(), actionRulesFile: tsv })
    const blocks = await matcher.match('deploy\tprod', 'bash', [{ id: 'deploy', shell: 'bash', pattern: '^deploy[[:space:]]+prod$', hint: 'Use guarded deploy.', priority: 8, capabilityRevision: 4 }], 4)
    assert.equal(blocks.length, 1)
    assert.equal(blocks[0]?.category, 'action_rule')
    assert.deepEqual(await matcher.match('deploy\tprod', 'bash', [{ id: 'deploy', shell: 'bash', pattern: '^deploy[[:space:]]+prod$', hint: 'Use guarded deploy.', priority: 8, capabilityRevision: 3 }], 4), [])
    assert.ok(matcher.diagnostics.includes('action-descriptors-stale'))
    assert.equal((await matcher.match('echo harmless', 'bash', [{ id: 'deploy', shell: 'bash', pattern: '^deploy[[:space:]]+prod$', hint: 'Use guarded deploy.', priority: 8, capabilityRevision: 4 }], 4)).length, 0)
    assert.equal(matcher.diagnostics.includes('action-fast-reject-stale'), false)
    await matcher.dispose()
  } finally {
    await rm(dir, { recursive: true, force: true })
  }
})

test('fails closed and diagnoses a partially stale selected TSV row set', async () => {
  const dir = await mkdtemp(join(tmpdir(), 'sb-opencode-dispose-'))
  try {
    const tsv = join(dir, 'action-rules.tsv')
    await writeFile(tsv, 'bash\tcurrent\t^current$\tcurrent hint\n', 'utf8')
    const matcher = createActionMatcher({ grepExe: grepPath(), actionRulesFile: tsv })
    const blocks = await matcher.match('current', 'bash', [
      { id: 'current', shell: 'bash', pattern: '^current$', hint: 'current hint', priority: 2, capabilityRevision: 1 },
      { id: 'missing', shell: 'bash', pattern: '^missing$', hint: 'missing hint', priority: 1, capabilityRevision: 1 },
    ], 1)
    assert.deepEqual(blocks, [])
    assert.ok(matcher.diagnostics.includes('action-artifact-stale'))
    await matcher.dispose()
  } finally { await rm(dir, { recursive: true, force: true }) }
})

function fakeChild(failFirstKill = false) {
  const child = new EventEmitter() as EventEmitter & { stdin: EventEmitter & { end: () => void }; kill: (signal?: string) => boolean; killCalls: number; closeSeen: boolean }
  const input = new EventEmitter() as EventEmitter & { end: () => void }
  input.end = () => {}
  child.stdin = input
  child.killCalls = 0
  child.closeSeen = false
  child.kill = () => {
    child.killCalls += 1
    if (failFirstKill && child.killCalls === 1) return false
    queueMicrotask(() => { child.closeSeen = true; child.emit('close', null, 'SIGKILL') })
    return true
  }
  return child
}

test('stdin EPIPE is handled safely and produces prompt termination', async () => {
  const dir = await mkdtemp(join(tmpdir(), 'sb-opencode-epipe-'))
  try {
    const tsv = join(dir, 'action-rules.tsv')
    await writeFile(tsv, 'bash\tx\t.*\thint\n', 'utf8')
    await writeFile(join(dir, 'action-rules.bash.re'), '.*\n', 'utf8')
    const child = fakeChild()
    let observed!: () => void
    const spawned = new Promise<void>((resolve) => { observed = resolve })
    let unhandled = false
    let killsAtError = 0
    child.stdin.end = () => queueMicrotask(() => {
      try { child.stdin.emit('error', Object.assign(new Error('synthetic pipe close'), { code: 'EPIPE' })) }
      catch { unhandled = true }
      killsAtError = child.killCalls
      observed()
    })
    const matcher = createActionMatcher({ grepExe: 'grep', actionRulesFile: tsv }, (() => child) as never)
    const matching = matcher.match('command', 'bash', [{ id: 'x', shell: 'bash', pattern: '.*', hint: 'hint', priority: 1, capabilityRevision: 0 }], 0)
    await spawned
    await matcher.dispose()
    assert.equal(unhandled, false)
    assert.ok(killsAtError > 0)
    assert.equal(child.closeSeen, true)
    assert.deepEqual(await matching, [])
  } finally { await rm(dir, { recursive: true, force: true }) }
})

test('child error retains ownership until dispose termination and close', async () => {
  const dir = await mkdtemp(join(tmpdir(), 'sb-opencode-child-error-'))
  try {
    const tsv = join(dir, 'action-rules.tsv')
    await writeFile(tsv, 'bash\tx\t.*\thint\n', 'utf8')
    await writeFile(join(dir, 'action-rules.bash.re'), '.*\n', 'utf8')
    const child = fakeChild(true)
    let observed!: () => void
    const errored = new Promise<void>((resolve) => { observed = resolve })
    const matcher = createActionMatcher({ grepExe: 'grep', actionRulesFile: tsv }, (() => {
      queueMicrotask(() => { child.emit('error', new Error('synthetic spawn/runtime error')); observed() })
      return child
    }) as never)
    const matching = matcher.match('command', 'bash', [{ id: 'x', shell: 'bash', pattern: '.*', hint: 'hint', priority: 1, capabilityRevision: 0 }], 0)
    await errored
    await matcher.dispose()
    assert.ok(child.killCalls >= 2)
    assert.equal(child.closeSeen, true)
    assert.deepEqual(await matching, [])
  } finally { await rm(dir, { recursive: true, force: true }) }
})

test('timeout failure kills the owned grep and dispose observes close', async () => {
  const dir = await mkdtemp(join(tmpdir(), 'sb-opencode-timeout-'))
  try {
    const tsv = join(dir, 'action-rules.tsv')
    await writeFile(tsv, 'bash\tx\t.*\thint\n', 'utf8')
    const child = fakeChild()
    const matcher = createActionMatcher({ grepExe: 'grep', actionRulesFile: tsv }, (() => child) as never)
    const blocks = await matcher.match('command', 'bash', [
      { id: 'x', shell: 'bash', pattern: '.*', hint: 'hint', priority: 1, capabilityRevision: 0 },
    ], 0)
    assert.deepEqual(blocks, [])
    assert.ok(matcher.diagnostics.includes('grep-timeout'))
    await matcher.dispose()
    assert.ok(child.killCalls >= 1)
    assert.equal(child.closeSeen, true)
  } finally { await rm(dir, { recursive: true, force: true }) }
})

test('matchDetailed captures busy per call while a failed child remains owned, then recovers after close', async () => {
  const dir = await mkdtemp(join(tmpdir(), 'sb-opencode-detailed-match-'))
  try {
    const tsv = join(dir, 'action-rules.tsv')
    await writeFile(tsv, 'bash\tstatus\t^git status$\tUse the synthetic status route.\n', 'utf8')
    const owned = fakeChild()
    owned.kill = () => { owned.killCalls += 1; return true }
    let spawnCount = 0
    const matcher = createActionMatcher({ grepExe: 'grep', actionRulesFile: tsv }, (() => {
      spawnCount += 1
      if (spawnCount === 1) {
        queueMicrotask(() => owned.emit('error', new Error('synthetic grep failure while retaining child')))
        return owned
      }
      const child = fakeChild()
      child.stdin.end = () => queueMicrotask(() => { child.closeSeen = true; child.emit('close', 0, null) })
      return child
    }) as never)
    const descriptors = [{ id: 'status', shell: 'bash' as const, pattern: '^git status$', hint: 'Use the synthetic status route.', priority: 8, capabilityRevision: 5 }]
    const failed = await matcher.matchDetailed('git status', 'bash', descriptors, 5)
    assert.equal(failed.status, 'degraded')
    assert.deepEqual(failed.blocks, [])
    const busy = await matcher.matchDetailed('git status', 'bash', descriptors, 5)
    assert.equal(busy.status, 'busy')
    assert.deepEqual(busy.blocks, [])
    assert.ok(matcher.diagnostics.includes('action-match-busy'))
    owned.closeSeen = true
    owned.emit('close', null, 'SIGKILL')
    const recovered = await matcher.matchDetailed('git status', 'bash', descriptors, 5)
    assert.equal(recovered.status, 'ok')
    assert.equal(recovered.blocks[0]?.text, 'Use the synthetic status route.')
    assert.equal((await matcher.match('git status', 'bash', descriptors, 5)).length, 1, 'legacy match wrapper still returns its block array')
    await matcher.dispose()
  } finally { await rm(dir, { recursive: true, force: true }) }
})
