export type BrainCommand = 'brain-init' | 'brain-save' | 'brain-sync'

const commandDefinitions: Record<BrainCommand, { description: string; prompt: string }> = {
  'brain-init': {
    description: 'Initialize Symbiosis Brain context for the current work.',
    prompt: 'Invoke the skill named brain-init via the skill tool, then follow its instructions exactly. Additional user instructions: $ARGUMENTS',
  },
  'brain-save': {
    description: 'Review and preserve useful knowledge with Symbiosis Brain.',
    prompt: 'Invoke the skill named brain-save via the skill tool, then follow its instructions exactly. Additional user instructions: $ARGUMENTS',
  },
  'brain-sync': {
    description: 'Explain the deferred manual vault Git sync and handle separately requested index refreshes.',
    prompt: 'Manual vault Git sync is deferred to stage 2 of the OpenCode adapter. Explain this; do not run Git commands or claim synchronization. If the user separately requests an index refresh, invoke the brain_sync MCP tool.',
  },
}

export function renderCommands(): Record<BrainCommand, string> {
  return Object.fromEntries(
    Object.entries(commandDefinitions).map(([name, command]) => [
      name,
      `---\ndescription: ${command.description}\n---\n${command.prompt}\n`,
    ]),
  ) as Record<BrainCommand, string>
}
