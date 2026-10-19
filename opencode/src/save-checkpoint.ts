export type TodoItem = { status?: string; [key: string]: unknown }

export interface SaveCheckpoints {
  onTodo(sessionId: string, todos: TodoItem[]): void
  onActivity(sessionId: string, generation: number): void
  onBrainStart(sessionId: string, callId: string, tool: string, generation: number, expectedPath: string): void
  onBrainResult(sessionId: string, callId: string, tool: string, result: unknown): void
  appendPending(sessionId: string, tool: string, result: unknown): void
  drop(sessionId: string): void
  clear(): void
}

interface BrainWrite {
  tool: string
  generation: number
  revision: number
  expectedPath: string
}

interface SessionCheckpoint {
  activityRevision: number
  generation: number
  savedRevision?: number
  savedGeneration?: number
  sawUnfinished: boolean
  milestoneRevision?: number
  milestoneGeneration?: number
  delivered: boolean
  writes: Map<string, BrainWrite>
}

const SAVE_TOOLS = new Set([
  'symbiosis-brain_brain_write', 'symbiosis-brain_brain_append', 'symbiosis-brain_brain_patch',
  'symbiosis_brain_brain_write', 'symbiosis_brain_brain_append', 'symbiosis_brain_brain_patch',
])
const TODO_TOOLS = new Set(['todowrite', 'todo_write'])
const CANCELLED = new Set(['cancelled', 'canceled'])
const COMPLETED = new Set(['completed', 'complete', 'done'])
const ERROR_TEXT = /(?:^|\n)\s*(?:error|failed|failure)\s*[:\-]/i
const REMINDER = '\n\n[SB save checkpoint] This work reached a meaningful milestone. Follow the brain-save skill to decide whether a durable lesson, decision, or handoff should be saved; do not save routine or sensitive content.'
const MAX_SESSIONS = 256
const OWN_BRAIN_OPERATION = /_brain_[a-z0-9_]+$/i

function record(value: unknown): Record<string, unknown> | undefined {
  return typeof value === 'object' && value !== null && !Array.isArray(value) ? value as Record<string, unknown> : undefined
}

function normalizedPath(value: string): string {
  return value.replaceAll('\\', '/').replace(/\/+$/, '').toLocaleLowerCase()
}

function resultTexts(value: Record<string, unknown>): string[] {
  const native = typeof value.output === 'string' ? [value.output] : []
  const content = Array.isArray(value.content) ? value.content.flatMap((item) => {
    const part = record(item)
    return part?.type === 'text' && typeof part.text === 'string' ? [part.text] : []
  }) : []
  return [...native, ...content]
}

function hasResultFailure(value: Record<string, unknown>): boolean {
  if (value.isError === true || value.error !== undefined || value.success === false) return true
  if (typeof value.status === 'string' && /^(?:error|failed|failure)$/i.test(value.status)) return true
  const metadata = record(value.metadata)
  if (metadata) {
    if (metadata.isError === true || metadata.success === false || metadata.error !== undefined) return true
    if (typeof metadata.status === 'string' && /^(?:error|failed|failure)$/i.test(metadata.status)) return true
    for (const key of ['exit', 'exitCode', 'code'] as const) {
      const status = metadata[key]
      if (typeof status === 'number' && status !== 0) return true
      if (typeof status === 'string' && /^-?\d+$/.test(status) && Number(status) !== 0) return true
    }
  }
  const structured = record(value.structuredContent)
  if (structured && (structured.isError === true || structured.success === false
    || (typeof structured.status === 'string' && /^(?:error|failed|failure)$/i.test(structured.status)))) return true
  return resultTexts(value).some((text) => ERROR_TEXT.test(text))
}

function confirmedSuccess(result: unknown, expectedPath: string, tool: string): boolean {
  const value = record(result)
  if (!value || hasResultFailure(value)) return false
  const structured = record(value.structuredContent)
  if (structured?.success === true && typeof structured.path === 'string') {
    return normalizedPath(structured.path) === normalizedPath(expectedPath)
  }
  const escape = (part: string) => part.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
  const pathPattern = expectedPath.replaceAll('\\', '/').split('/').map(escape).join('[/\\\\]')
  const quoted = `['\"]?${pathPattern}['\"]?`
  const normalizedTool = tool.toLocaleLowerCase()
  return resultTexts(value).some((text) => {
    if (normalizedTool.endsWith('_brain_write') || normalizedTool.endsWith('-brain_write')) return new RegExp(`^\\s*Saved:\\s*${quoted}(?:\\s|$)`, 'i').test(text)
    if (normalizedTool.endsWith('_brain_append') || normalizedTool.endsWith('-brain_append')) return new RegExp(`^\\s*Appended to .+ in ${quoted}(?:\\s|$)`, 'i').test(text)
    if (normalizedTool.endsWith('_brain_patch') || normalizedTool.endsWith('-brain_patch')) return new RegExp(`^\\s*Patched ${quoted}(?:\\s|$)`, 'i').test(text)
    return false
  })
}

function isMcp(value: Record<string, unknown>): boolean {
  return Array.isArray(value.content)
}

function eligibleTool(tool: string): boolean {
  const value = tool.toLocaleLowerCase()
  return !SAVE_TOOLS.has(value) && !TODO_TOOLS.has(value) && !OWN_BRAIN_OPERATION.test(value)
    && !/(?:title|compact|compaction|summary)/i.test(value)
}

export function createSaveCheckpoints(): SaveCheckpoints {
  const sessions = new Map<string, SessionCheckpoint>()

  function stateFor(sessionId: string): SessionCheckpoint {
    let state = sessions.get(sessionId)
    if (!state) {
      state = { activityRevision: 0, generation: 0, sawUnfinished: false, delivered: false, writes: new Map() }
    }
    sessions.delete(sessionId)
    sessions.set(sessionId, state)
    while (sessions.size > MAX_SESSIONS) sessions.delete(sessions.keys().next().value!)
    return state
  }

  function bindGeneration(state: SessionCheckpoint, generation: number): void {
    if (generation > state.generation) {
      state.generation = generation
      state.sawUnfinished = false
      state.milestoneRevision = undefined
      state.milestoneGeneration = undefined
      state.savedRevision = undefined
      state.savedGeneration = undefined
      state.delivered = false
    }
  }

  return {
    onTodo(sessionId, todos) {
      const state = stateFor(sessionId)
      const nonCancelled = todos.filter((todo) => typeof todo.status !== 'string' || !CANCELLED.has(todo.status.toLocaleLowerCase()))
      const unfinished = nonCancelled.some((todo) => typeof todo.status !== 'string' || !COMPLETED.has(todo.status.toLocaleLowerCase()))
      const completeMilestone = nonCancelled.length > 0 && !unfinished
      if (state.sawUnfinished && completeMilestone) {
        if (state.savedGeneration === state.generation && state.savedRevision !== undefined && state.savedRevision >= state.activityRevision) {
          state.milestoneRevision = undefined
          state.milestoneGeneration = undefined
          state.delivered = false
        } else {
          state.milestoneRevision = state.activityRevision
          state.milestoneGeneration = state.generation
          state.delivered = false
        }
      }
      state.sawUnfinished = nonCancelled.length > 0 && unfinished
    },
    onActivity(sessionId, generation) {
      const state = stateFor(sessionId)
      bindGeneration(state, generation)
      state.activityRevision += 1
    },
    onBrainStart(sessionId, callId, tool, generation, expectedPath) {
      const state = stateFor(sessionId)
      bindGeneration(state, generation)
      if (!SAVE_TOOLS.has(tool.toLocaleLowerCase()) || !callId) return
      state.writes.set(callId, { tool: tool.toLocaleLowerCase(), generation, revision: state.activityRevision, expectedPath })
      while (state.writes.size > 128) state.writes.delete(state.writes.keys().next().value!)
    },
    onBrainResult(sessionId, callId, tool, result) {
      const state = sessions.get(sessionId)
      const write = state?.writes.get(callId)
      if (!state || !write || write.tool !== tool.toLocaleLowerCase()) return
      state.writes.delete(callId)
      if (write.generation !== state.generation || !confirmedSuccess(result, write.expectedPath, write.tool)) return
      if (state.savedGeneration !== write.generation || state.savedRevision === undefined || write.revision > state.savedRevision) {
        state.savedGeneration = write.generation
        state.savedRevision = write.revision
      }
      if (state.milestoneGeneration === write.generation && state.milestoneRevision !== undefined
        && state.milestoneRevision <= state.savedRevision) {
        state.milestoneRevision = undefined
        state.milestoneGeneration = undefined
        state.delivered = false
      }
    },
    appendPending(sessionId, tool, result) {
      const state = sessions.get(sessionId)
      const value = record(result)
      if (!state || !value || !eligibleTool(tool) || state.delivered || state.milestoneRevision === undefined
        || state.milestoneGeneration !== state.generation || hasResultFailure(value)) return
      if (isMcp(value)) {
        const parts = value.content as unknown[]
        const texts = parts.map(record).filter((part) => part?.type === 'text' && typeof part.text === 'string')
        const first = texts[0]
        if (first) first.text = `${first.text as string}${REMINDER}`
        else parts.push({ type: 'text', text: REMINDER.trimStart() })
      } else {
        if (typeof value.output !== 'string') return
        value.output = `${value.output}${REMINDER}`
      }
      state.delivered = true
    },
    drop(sessionId) { sessions.delete(sessionId) },
    clear() { sessions.clear() },
  }
}
