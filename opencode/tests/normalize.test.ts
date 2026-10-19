import test from 'node:test'
import assert from 'node:assert/strict'
import { normalizeAction } from '../src/normalize.ts'
import { capabilitiesFromSnapshot } from '../src/capabilities.ts'

test('normalizes read/edit/apply_patch/task without changing arguments', () => {
  const args = { filePath: 'src/alpha.ts', oldString: 'a', newString: 'safe text' }
  const before = structuredClone(args)
  const host = { shell: 'powershell', cwd: '/work', boundary: '/work' } as const
  const edit = normalizeAction('edit', args, host)
  assert.ok(edit)
  assert.match(edit.query, /alpha\.ts/)
  assert.match(edit.query, /safe text/)
  assert.deepEqual(args, before)
  assert.match(normalizeAction('apply_patch', { patchText: '*** Begin Patch\n*** Add File: x\n+one\n+two\n*** End Patch' }, host)!.query, /one.*two/s)
  assert.match(normalizeAction('task', { description: 'Investigate alpha behavior' }, host)!.query, /Investigate alpha/)
})

test('redacts credentials before limiting and emits safe oversize diagnostics', () => {
  const host = { shell: 'powershell', cwd: '/work', boundary: '/work' } as const
  const secret = normalizeAction('edit', { filePath: 'secret.txt', newString: 'x'.repeat(1000) + ' Authorization: Bearer synthetic-secret' }, host)!
  assert.ok(!secret.query.includes('synthetic-secret'))
  const shell = normalizeAction('bash', { command: 'git push Authorization: Bearer synthetic-secret' }, host)!
  assert.ok(shell.command && !shell.command.includes('synthetic-secret'))
  const large = normalizeAction('write', { filePath: 'big.txt', content: 'safe '.repeat(100_000) }, host)!
  assert.match(large.query, /oversize/i)
  assert.ok(!large.query.includes('safe safe'))
  const rawOversizePatch = '*** Begin Patch\n*** Update File: first.ts\n+retained\n' + '-deleted context '.repeat(1_000) +
    '*** Add File: seventh.ts\n+later body\n*** End Patch'
  const oversizedPatch = normalizeAction('apply_patch', { patchText: rawOversizePatch }, host)!
  assert.match(oversizedPatch.query, /oversize/i)
  assert.ok(!oversizedPatch.query.includes('retained') && !oversizedPatch.query.includes('later body'))
})

test('whole-line redaction covers quoted JSON, environment, connection strings and chained commands', () => {
  const host = { shell: 'powershell', cwd: '/work', boundary: '/work' } as const
  const result = normalizeAction('edit', {
    filePath: 'src/secret.txt',
    newString: 'keep before\n{"api_key":"first SYNTHETIC_SECRET"}\nkeep after',
  }, host)!
  assert.equal(result.query, 'secret.txt keep before\n[REDACTED]\nkeep after')
  for (const value of [
    'setx API_KEY "first SYNTHETIC_SECRET"',
    '$env:API_KEY = "first SYNTHETIC_SECRET"',
    'Server=example;User Id=demo;Password="first SYNTHETIC_SECRET";',
    '-----BEGIN PRIVATE KEY-----\nfirst SYNTHETIC_SECRET\n-----END PRIVATE KEY-----',
  ]) {
    const normalized = normalizeAction('edit', { filePath: 'secret.txt', newString: value }, host)!
    assert.ok(!normalized.query.includes('SYNTHETIC_SECRET'))
  }
  const patch = normalizeAction('apply_patch', {
    patchText: '*** Begin Patch\n*** Add File: secret.json\n+{"api_key":"first SYNTHETIC_SECRET"}\n*** End Patch',
  }, host)!
  assert.equal(patch.query, 'secret.json:\n+[REDACTED PATCH LINE]')
  const command = normalizeAction('bash', { command: 'git push; setx API_KEY "first SYNTHETIC_SECRET"' }, host)!
  assert.equal(command.command, '[REDACTED]')
  assert.equal(command.query, '[REDACTED]')
})

test('setx value is suppressed even when variable and value are not secret-word markers', () => {
  const host = { shell: 'powershell', cwd: '/work', boundary: '/work' } as const
  const edit = normalizeAction('edit', {
    filePath: 'src/settings.txt',
    newString: 'safe before\nsetx ACCESS_KEY SYNTHETIC_VALUE\nsafe after',
  }, host)!
  assert.equal(edit.query, 'settings.txt safe before\n[REDACTED]\nsafe after')
  const quoted = normalizeAction('write', {
    filePath: 'src/settings.txt', content: 'setx ACCESS_KEY "first SYNTHETIC_VALUE"',
  }, host)!
  assert.ok(!quoted.query.includes('first SYNTHETIC_VALUE'))
  const command = normalizeAction('bash', {
    command: 'git push; setx ACCESS_KEY "first SYNTHETIC_VALUE"',
  }, host)!
  assert.equal(command.query, '[REDACTED]')
  assert.equal(command.command, '[REDACTED]')
  const patch = normalizeAction('apply_patch', {
    patchText: '*** Begin Patch\n*** Add File: setup.ps1\n+setx ACCESS_KEY SYNTHETIC_VALUE\n*** End Patch',
  }, host)!
  assert.equal(patch.query, 'setup.ps1:\n+[REDACTED PATCH LINE]')
})

test('unknown shell stays unknown; only selected searches and shell whitelist normalize', () => {
  assert.equal(normalizeAction('bash', { command: 'git status' }, { shell: null, cwd: '/w', boundary: '/w' }), null)
  assert.ok(normalizeAction('bash', { command: 'git status' }, { shell: 'bash', cwd: '/w', boundary: '/w' }))
  assert.equal(normalizeAction('bash', { command: 'Remove-Item x' }, { shell: 'bash', cwd: '/w', boundary: '/w' }), null)
  assert.ok(normalizeAction('grep', { pattern: 'needle', path: 'x.txt' }, { shell: null, cwd: '/w', boundary: '/w' }))
})

test('capabilities reflect connected, enabled session state and packaged skills only', () => {
  assert.deepEqual(capabilitiesFromSnapshot({
    tools: [{ name: 'allowed', enabled: true }, { name: 'denied', enabled: false }],
    mcp: [{ name: 'online', connected: true }, { name: 'offline', connected: false }],
    skills: [
      { name: 'packaged', available: true, source: 'package' },
      { name: 'claude-only', available: true, source: 'external' },
    ], revision: 7,
  }), { tools: ['allowed'], mcp: ['online'], skills: ['packaged'], revision: 7 })
})
