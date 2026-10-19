import test from 'node:test'
import assert from 'node:assert/strict'
import { renderCommands } from '../install/commands.ts'

const commandBodies = {
  'brain-init': 'Invoke the skill named brain-init via the skill tool, then follow its instructions exactly. Additional user instructions: $ARGUMENTS',
  'brain-save': 'Invoke the skill named brain-save via the skill tool, then follow its instructions exactly. Additional user instructions: $ARGUMENTS',
  'brain-sync': 'Manual vault Git sync is deferred to stage 2 of the OpenCode adapter. Explain this; do not run Git commands or claim synchronization. If the user separately requests an index refresh, invoke the brain_sync MCP tool.',
} as const

function bodyOf(command: string): string {
  const separator = command.indexOf('\n---\n')
  assert.notEqual(separator, -1, 'command must start with YAML frontmatter')
  return command.slice(separator + '\n---\n'.length).trim()
}

test('renders all commands with English descriptions and canonical prompt bodies', () => {
  const commands = renderCommands()
  assert.deepEqual(Object.keys(commands).sort(), ['brain-init', 'brain-save', 'brain-sync'])

  for (const name of Object.keys(commandBodies) as Array<keyof typeof commandBodies>) {
    assert.match(commands[name], /^---\ndescription: [A-Za-z][^\n]*\n---\n/)
    assert.equal(bodyOf(commands[name]), commandBodies[name])
  }
})

test('command body delegates to canonical skill without duplicating it', () => {
  const commands = renderCommands();
  assert.ok(commands['brain-save'].includes('Invoke the skill named brain-save'))
  assert.ok(commands['brain-save'].includes('$ARGUMENTS'))
  assert.ok(!commands['brain-save'].includes('Step 0'))
  assert.ok(commands['brain-sync'].includes('deferred to stage 2'))
});

test('sync placeholder never runs Git or substitutes an automatic index refresh', () => {
  const sync = bodyOf(renderCommands()['brain-sync'])
  assert.equal(sync, commandBodies['brain-sync'])
  assert.match(sync, /do not run Git commands or claim synchronization/)
  assert.match(sync, /If the user separately requests an index refresh, invoke the brain_sync MCP tool\./)
})
