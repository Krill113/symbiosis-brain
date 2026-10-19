export interface HostContext {
  shell: 'bash' | 'powershell' | null
  cwd: string
  boundary: string | null
}

export interface NormalizedAction {
  tool: string
  query: string
  shell?: 'bash' | 'powershell'
  command?: string
}

const MAX_QUERY = 4_000
const MAX_SOURCE = 12_000
const secretMarker = /\b(?:authorization|bearer)\b|\b(?:[A-Za-z0-9]+[_-])*(?:api[_-]?key|token|password|passwd|secret|credential|client[_-]?secret)\b|\bsetx\s+\S+\s+|:\/\/[^:/\s]+:[^@/\s]+@/i
const privateKey = /-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----/i
const privateKeyBlock = /-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----[\s\S]*?(?:-----END (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----|$)/gi
const shellAllow: Record<'bash' | 'powershell', RegExp> = {
  bash: /^(git\s+(status|diff|show|log|commit|push|merge|rebase|reset|cherry-pick|revert|tag)|(?:npm|pnpm|yarn|pip|uv)\s+(install|add|remove|uninstall)|docker\s+(push|deploy|run)|winget\s+(install|uninstall|upgrade)|claude\s+mcp\s+(add|remove)|gh\s+pr\s+(create|merge|close|edit)|\.?\/.+\.(sh|ps1|py|bat|cmd))/i,
  powershell: /^(git\s+(status|diff|show|log|commit|push|merge|rebase|reset|cherry-pick|revert|tag)|(?:npm|pnpm|yarn|pip|uv)\s+(install|add|remove|uninstall)|docker\s+(push|deploy|run)|winget\s+(install|uninstall|upgrade)|claude\s+mcp\s+(add|remove)|gh\s+pr\s+(create|merge|close|edit)|\.?\/.+\.(sh|ps1|py|bat|cmd))/i,
}

function text(value: unknown): string { return typeof value === 'string' ? value : '' }
function basename(value: unknown): string { return text(value).replaceAll('\\', '/').replace(/\/+$/, '').split('/').at(-1)?.slice(0, 160) ?? '' }

function redactLine(value: string, patch = false): string {
  if (privateKey.test(value)) return patch ? '[REDACTED PATCH LINE]' : '[REDACTED PRIVATE KEY]'
  if (secretMarker.test(value)) return patch ? '[REDACTED PATCH LINE]' : '[REDACTED]'
  return value
}

function redact(value: string, patch = false): string {
  return value.replace(privateKeyBlock, '[REDACTED PRIVATE KEY]').split(/\r?\n/).map((line) => {
    if (patch && line.startsWith('*** ')) return line
    const added = patch && line.startsWith('+') && !line.startsWith('+++')
    const safe = redactLine(added ? line.slice(1) : line, added)
    return added ? `+${safe}` : safe
  }).join('\n')
}

function limited(source: string, label: string, patch = false): string {
  const safe = redact(source, patch)
  if (source.length > MAX_SOURCE || safe.length > MAX_QUERY * 3) return `${label} [oversize: source omitted]`.trim()
  return safe.slice(0, MAX_QUERY)
}

function patchParts(source: string): string {
  const files: string[] = []
  let name = 'patch'
  let added: string[] = []
  const save = () => { if (added.length) files.push(`${name}:\n${added.slice(0, 80).join('\n')}`) }
  for (const line of source.split(/\r?\n/)) {
    if (/^\*\*\* (Add|Update|Delete) File:/.test(line)) {
      save()
      name = line.slice(line.indexOf(':') + 1).trim()
      added = []
    } else if (line.startsWith('+') && !line.startsWith('+++')) added.push(line)
  }
  save()
  return files.slice(0, 6).join('\n')
}

export function normalizeAction(tool: string, args: unknown, host: HostContext): NormalizedAction | null {
  if (typeof args !== 'object' || args === null || Array.isArray(args)) return null
  const input = args as Record<string, unknown>
  const name = tool.toLowerCase()
  const file = input.filePath ?? input.file_path ?? input.path
  const base = basename(file)
  if (['edit', 'write', 'multiedit', 'notebookedit'].includes(name)) {
    let content: string
    if (name === 'multiedit') {
      content = Array.isArray(input.edits) ? input.edits.map((entry) => {
        if (typeof entry !== 'object' || entry === null) return ''
        const e = entry as Record<string, unknown>
        return text(e.newString ?? e.new_string)
      }).join('\n') : ''
    } else content = text(input.newString ?? input.new_string ?? input.content ?? input.new_source)
    return { tool: name, query: `${base} ${limited(content, base)}`.trim() }
  }
  if (name === 'apply_patch' || name === 'applypatch') {
    const original = text(input.patchText ?? input.patch ?? input.input)
    const scanned = redact(original, true)
    if (original.length > MAX_SOURCE) return { tool: 'apply_patch', query: 'patch [oversize: source omitted]' }
    const projected = patchParts(scanned)
    return { tool: 'apply_patch', query: projected.length > MAX_QUERY * 3 ? 'patch [oversize: source omitted]' : projected.slice(0, MAX_QUERY) }
  }
  if (name === 'task' || name === 'agent') {
    return { tool: name, query: limited(text(input.prompt ?? input.description), 'task') }
  }
  if (['grep', 'ripgrep', 'search', 'glob'].includes(name)) {
    const query = [text(input.pattern ?? input.query), text(input.include), base].filter(Boolean).join(' ')
    return query ? { tool: name, query: limited(query, base) } : null
  }
  if (['bash', 'shell', 'powershell', 'terminal'].includes(name)) {
    if (host.shell === null || !shellAllow[host.shell]) return null
    const command = text(input.command)
    if (!command || !shellAllow[host.shell].test(command.trim())) return null
    if (command.length > MAX_SOURCE) return { tool: 'shell', query: 'command [oversize: source omitted]', shell: host.shell, command: '[OVERSIZE: command omitted]' }
    const safe = redact(command).slice(0, MAX_QUERY)
    return { tool: 'shell', query: safe, shell: host.shell, command: safe }
  }
  return null
}
