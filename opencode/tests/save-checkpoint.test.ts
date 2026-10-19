import assert from 'node:assert/strict'
import test from 'node:test'
import { createSaveCheckpoints } from '../src/save-checkpoint.ts'

test('native reminder preserves output; failed Brain write cannot suppress it', () => {
  const cp = createSaveCheckpoints()
  cp.onActivity('a', 1)
  cp.onBrainStart('a', 'write-a', 'symbiosis-brain_brain_write', 1, 'alpha/lesson.md')
  cp.onTodo('a', [{ content: 'fixture', status: 'in_progress' }])
  cp.onTodo('a', [{ content: 'fixture', status: 'completed' }])
  cp.onBrainResult('a', 'write-a', 'symbiosis-brain_brain_write', { isError: true })
  const result = { title: 'Read', output: 'ORIGINAL', metadata: {} }
  cp.appendPending('a', 'read', result)
  assert.ok(result.output.startsWith('ORIGINAL'))
  assert.equal(result.title, 'Read')
  assert.deepEqual(result.metadata, {})
  assert.ok(result.output.includes('[SB save checkpoint]'))
})

test('successful write only acknowledges activity captured at start, not later milestone activity', () => {
  const cp = createSaveCheckpoints()
  cp.onActivity('a', 1)
  cp.onTodo('a', [{ content: 'fixture', status: 'in_progress' }])
  cp.onBrainStart('a', 'write-a', 'symbiosis-brain_brain_write', 1, 'alpha/lesson.md')
  cp.onActivity('a', 1)
  cp.onTodo('a', [{ content: 'fixture', status: 'completed' }])
  cp.onActivity('a', 1)
  cp.onBrainResult('a', 'write-a', 'symbiosis-brain_brain_write', { content: [{ type: 'text', text: 'Saved: alpha/lesson.md' }] })
  const result = { title: 'Read', output: 'ORIGINAL', metadata: {} }
  cp.appendPending('a', 'read', result)
  assert.match(result.output, /\[SB save checkpoint\]/)
})

test('successful write before todo completion suppresses the same-revision milestone', () => {
  const cp = createSaveCheckpoints()
  cp.onActivity('a', 1)
  cp.onTodo('a', [{ content: 'fixture', status: 'in_progress' }])
  cp.onActivity('a', 1)
  cp.onBrainStart('a', 'write-a', 'symbiosis-brain_brain_write', 1, 'alpha/lesson.md')
  cp.onBrainResult('a', 'write-a', 'symbiosis-brain_brain_write', { content: [{ type: 'text', text: 'Saved: alpha/lesson.md' }] })
  cp.onTodo('a', [{ content: 'fixture', status: 'completed' }])
  const result = { title: 'Read', output: 'ORIGINAL', metadata: {} }
  cp.appendPending('a', 'read', result)
  assert.equal(result.output, 'ORIGINAL')
})

test('successful current write suppresses reminder, stale generation and duplicate results do not', () => {
  const cp = createSaveCheckpoints()
  cp.onActivity('a', 2)
  cp.onTodo('a', [{ content: 'fixture', status: 'in_progress' }])
  cp.onBrainStart('a', 'write-a', 'symbiosis-brain_brain_write', 2, 'alpha/lesson.md')
  cp.onTodo('a', [{ content: 'fixture', status: 'completed' }])
  cp.onBrainResult('a', 'write-a', 'symbiosis-brain_brain_write', { content: [{ type: 'text', text: 'Saved: alpha/lesson.md' }] })
  cp.onBrainResult('a', 'write-a', 'symbiosis-brain_brain_write', { content: [{ type: 'text', text: 'Saved: alpha/lesson.md' }] })
  const result = { title: 'Read', output: 'ORIGINAL', metadata: {} }
  cp.appendPending('a', 'read', result)
  assert.equal(result.output, 'ORIGINAL')
  cp.onActivity('a', 3)
  cp.onTodo('a', [{ content: 'next', status: 'in_progress' }])
  cp.onTodo('a', [{ content: 'next', status: 'completed' }])
  const next = { title: 'Read', output: 'ORIGINAL', metadata: {} }
  cp.appendPending('a', 'read', next)
  assert.match(next.output, /\[SB save checkpoint\]/)
})

test('late successful writes from an earlier generation cannot acknowledge the current milestone', () => {
  const cp = createSaveCheckpoints()
  cp.onActivity('a', 4)
  cp.onTodo('a', [{ content: 'old task', status: 'in_progress' }])
  cp.onBrainStart('a', 'old-write', 'symbiosis_brain_brain_write', 4, 'demo/old.md')
  cp.onActivity('a', 5)
  cp.onTodo('a', [{ content: 'new task', status: 'in_progress' }])
  cp.onTodo('a', [{ content: 'new task', status: 'completed' }])
  cp.onBrainResult('a', 'old-write', 'symbiosis_brain_brain_write', { content: [{ type: 'text', text: 'Saved: demo/old.md' }] })
  const result = { title: 'Read', output: 'ORIGINAL', metadata: {} }
  cp.appendPending('a', 'read', result)
  assert.match(result.output, /\[SB save checkpoint\]/)
})

test('confirmed Brain write success is specific to the expected path and operation contract', () => {
  const cp = createSaveCheckpoints()
  cp.onActivity('a', 1)
  cp.onTodo('a', [{ content: 'fixture', status: 'in_progress' }])
  cp.onBrainStart('a', 'write-a', 'symbiosis_brain_brain_write', 1, 'demo/expected.md')
  cp.onTodo('a', [{ content: 'fixture', status: 'completed' }])
  cp.onBrainResult('a', 'write-a', 'symbiosis_brain_brain_write', { content: [{ type: 'text', text: 'Saved: demo/other.md' }] })
  const result = { title: 'Read', output: 'ORIGINAL', metadata: {} }
  cp.appendPending('a', 'read', result)
  assert.match(result.output, /\[SB save checkpoint\]/)
})

test('write, append, and patch confirm only their returned success prefix and expected path', () => {
  for (const [tool, response] of [
    ['symbiosis-brain_brain_write', 'Saved: demo/lesson.md'],
    ['symbiosis-brain_brain_append', "Appended to 'Next' in demo/lesson.md"],
    ['symbiosis-brain_brain_patch', 'Patched demo/lesson.md'],
  ]) {
    const cp = createSaveCheckpoints()
    cp.onActivity('a', 1)
    cp.onTodo('a', [{ status: 'in_progress' }])
    cp.onBrainStart('a', 'call', tool, 1, 'demo/lesson.md')
    cp.onTodo('a', [{ status: 'completed' }])
    cp.onBrainResult('a', 'call', tool, { content: [{ type: 'text', text: response }] })
    const result = { title: 'Read', output: 'ORIGINAL', metadata: {} }
    cp.appendPending('a', 'read', result)
    assert.equal(result.output, 'ORIGINAL', `${tool} success should acknowledge its own milestone`)
  }
})

test('MCP text errors and isError results do not consume the reminder', () => {
  const cp = createSaveCheckpoints()
  cp.onActivity('a', 1)
  cp.onTodo('a', [{ status: 'in_progress' }])
  cp.onTodo('a', [{ status: 'completed' }])
  cp.appendPending('a', 'read', { isError: true, content: [{ type: 'text', text: 'Error: synthetic failure' }] })
  cp.appendPending('a', 'read', { content: [{ type: 'text', text: 'Error: synthetic failure' }] })
  const result = { content: [{ type: 'text', text: 'Synthetic success' }] }
  cp.appendPending('a', 'read', result)
  assert.match(result.content[0]!.text, /\[SB save checkpoint\]/)
})

test('own Brain operations do not consume a reminder; next working result receives it', () => {
  const cp = createSaveCheckpoints()
  cp.onActivity('a', 1)
  cp.onTodo('a', [{ status: 'in_progress' }])
  cp.onTodo('a', [{ status: 'completed' }])
  for (const tool of ['symbiosis-brain_brain_search', 'symbiosis-brain_brain_read', 'symbiosis-brain_brain_sync']) {
    const result = { content: [{ type: 'text', text: 'Synthetic result' }] }
    cp.appendPending('a', tool, result)
    assert.equal(result.content[0]!.text, 'Synthetic result')
  }
  const working = { title: 'Read', output: 'ORIGINAL', metadata: {} }
  cp.appendPending('a', 'read', working)
  assert.match(working.output, /\[SB save checkpoint\]/)
})

test('todo control results do not consume the milestone they report', () => {
  const cp = createSaveCheckpoints()
  cp.onActivity('a', 1)
  cp.onTodo('a', [{ status: 'in_progress' }])
  const completedTodos = [{ status: 'completed' }]
  cp.onTodo('a', completedTodos)
  const control = { title: 'Todos updated', output: 'Synthetic todo result', metadata: {} }
  cp.appendPending('a', 'todowrite', control)
  assert.equal(control.output, 'Synthetic todo result')
  const working = { title: 'Read', output: 'Synthetic work result', metadata: {} }
  cp.appendPending('a', 'read', working)
  assert.match(working.output, /\[SB save checkpoint\]/)
})

test('native nonzero exits retain the reminder for the next successful tool result', () => {
  const cp = createSaveCheckpoints()
  cp.onActivity('a', 1)
  cp.onTodo('a', [{ status: 'in_progress' }])
  cp.onTodo('a', [{ status: 'completed' }])
  const failed = { title: 'bash', output: 'synthetic command failure', metadata: { exit: 1 } }
  cp.appendPending('a', 'bash', failed)
  assert.equal(failed.output, 'synthetic command failure')
  const succeeded = { title: 'read', output: 'Synthetic success', metadata: { exit: 0 } }
  cp.appendPending('a', 'read', succeeded)
  assert.match(succeeded.output, /\[SB save checkpoint\]/)
})

test('Brain result with any contradictory error text cannot advance the saved revision', () => {
  const cp = createSaveCheckpoints()
  cp.onActivity('a', 1)
  cp.onTodo('a', [{ status: 'in_progress' }])
  cp.onBrainStart('a', 'write', 'symbiosis-brain_brain_write', 1, 'demo/note.md')
  cp.onTodo('a', [{ status: 'completed' }])
  cp.onBrainResult('a', 'write', 'symbiosis-brain_brain_write', {
    structuredContent: { success: true, path: 'demo/note.md' },
    content: [
      { type: 'text', text: 'Saved: demo/note.md' },
      { type: 'text', text: 'Error: synthetic failure' },
    ],
  })
  const result = { title: 'Read', output: 'ORIGINAL', metadata: {} }
  cp.appendPending('a', 'read', result)
  assert.match(result.output, /\[SB save checkpoint\]/)
})

test('MCP reminder appends only text and leaves binary content and metadata untouched', () => {
  const cp = createSaveCheckpoints()
  cp.onActivity('a', 1)
  cp.onTodo('a', [{ content: 'fixture', status: 'in_progress' }])
  cp.onTodo('a', [{ content: 'fixture', status: 'completed' }])
  const image = { type: 'image', data: 'synthetic-binary', mimeType: 'image/png' }
  const result = { content: [{ type: 'text', text: 'Completed' }, image], _meta: { keep: true }, structuredContent: { keep: true } }
  cp.appendPending('a', 'read', result)
  assert.match((result.content[0] as { type: string; text: string }).text, /\[SB save checkpoint\]/)
  assert.equal(result.content[1], image)
  assert.deepEqual(result._meta, { keep: true })
  assert.deepEqual(result.structuredContent, { keep: true })
})

test('empty and all-cancelled todo lists do not create reminders; repeated event adds one advisory', () => {
  const cp = createSaveCheckpoints()
  cp.onTodo('a', [{ content: 'fixture', status: 'in_progress' }])
  cp.onTodo('a', [])
  cp.onTodo('a', [{ content: 'fixture', status: 'cancelled' }])
  const noMilestone = { title: 'Read', output: 'ORIGINAL', metadata: {} }
  cp.appendPending('a', 'read', noMilestone)
  assert.equal(noMilestone.output, 'ORIGINAL')
  cp.onTodo('a', [{ content: 'fixture', status: 'in_progress' }])
  cp.onTodo('a', [{ content: 'fixture', status: 'completed' }])
  const result = { title: 'Read', output: 'ORIGINAL', metadata: {} }
  cp.appendPending('a', 'read', result)
  cp.appendPending('a', 'read', result)
  assert.equal((result.output.match(/\[SB save checkpoint\]/g) ?? []).length, 1)
})

test('session disposal drops pending checkpoint state', () => {
  const cp = createSaveCheckpoints()
  cp.onActivity('a', 1)
  cp.onTodo('a', [{ status: 'in_progress' }])
  cp.onTodo('a', [{ status: 'completed' }])
  cp.drop('a')
  const result = { title: 'Read', output: 'ORIGINAL', metadata: {} }
  cp.appendPending('a', 'read', result)
  assert.equal(result.output, 'ORIGINAL')
})
