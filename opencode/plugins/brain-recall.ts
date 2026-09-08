import type { Plugin } from "@opencode-ai/plugin"
import { existsSync, readFileSync, writeFileSync } from "node:fs"
import { dirname, join, resolve } from "node:path"

type SearchResult = {
  memory_hits: Array<{ path: string; title: string; scope: string; gist: string }>
  route_hints: Array<{ id: string; class: string; hint: string }>
}

export const BrainRecallPlugin: Plugin = async ({ client, directory }) => {
  const env = process.env
  const disabled = env.SYMBIOSIS_BRAIN_PRE_ACTION_DISABLED === "1"
  const skipShort = Number(env.SYMBIOSIS_BRAIN_RECALL_SKIP_SHORT_CHARS) || 15
  const timeoutMs = Number(env.SYMBIOSIS_BRAIN_OC_RECALL_TIMEOUT_MS) || 30000

  const mode = env.SYMBIOSIS_BRAIN_OC_RECALL_MODE === "silent" || env.SYMBIOSIS_BRAIN_OC_RECALL_MODE === "off"
    ? env.SYMBIOSIS_BRAIN_OC_RECALL_MODE
    : "block"
  const maxBlocks = Number(env.SYMBIOSIS_BRAIN_OC_RECALL_MAX_BLOCKS) || 5
  const bashOn = env.SYMBIOSIS_BRAIN_OC_RECALL_BASH === "1"

  const state = {
    vault: null as string | null,
    tools: null as string | null,
    scopeEnv: (env.SYMBIOSIS_BRAIN_SCOPE || null) as string | null,
    scopeMemo: null as string | null | undefined,
    prewarmed: false,
    lastText: new Map<string, string>(),
    injectedBlock: new Map<string, { text: string; block: string }>(),
    pending: new Map<string, Promise<string | null>>(),
    mcpNames: [] as string[],
    writtenRosters: new Set<string>(),
    warnedFiles: new Map<string, Set<string>>(),
    blockCount: new Map<string, number>(),
  }

  const log = (msg: string) => {
    try {
      void client.app
        .log({ body: { service: "brain-recall", level: "info", message: msg } })
        .catch(() => {})
    } catch {}
  }

  log("brain-recall plugin loaded (layer2)" + (disabled ? " (DISABLED)" : ""))

  function partsToText(parts: unknown): string {
    if (!Array.isArray(parts)) return ""
    let out = ""
    for (const p of parts) {
      if (typeof p === "string") {
        out += p
      } else if (p && typeof p === "object") {
        const o = p as Record<string, unknown>
        if (o.type === "text" && typeof o.text === "string") out += o.text
        else if (typeof o.content === "string") out += o.content
      }
    }
    return out
  }

  function resolveScope(cwd: string): string | null {
    if (state.scopeEnv) return state.scopeEnv
    if (state.scopeMemo !== undefined) return state.scopeMemo
    let found: string | null = null
    let dir = resolve(cwd)
    for (let i = 0; i < 8; i++) {
      const p = join(dir, "CLAUDE.md")
      if (existsSync(p)) {
        const m = readFileSync(p, "utf8").match(
          /<!--\s*symbiosis-brain\s+v1:\s*scope=(\S+)\s*-->/,
        )
        if (m) {
          found = m[1]
          break
        }
      }
      const parent = dirname(dir)
      if (parent === dir) break
      dir = parent
    }
    state.scopeMemo = found
    return found
  }

  function ensureRoster(sid: string) {
    if (env.SYMBIOSIS_BRAIN_OC_ROSTER_OFF === "1") return
    if (!sid || state.writtenRosters.has(sid)) return
    state.writtenRosters.add(sid)
    if (state.mcpNames.length === 0) return
    const tmp = process.env.TMPDIR || process.env.TEMP || ""
    if (!tmp) return
    try {
      const lines = state.mcpNames.map((n) => n + ": local MCP - connected").join("\n")
      writeFileSync(join(tmp, "brain-mcp-roster-" + sid), lines, "utf8")
      log("roster written: " + state.mcpNames.join(","))
    } catch {}
  }

  async function runSearchGist(query: string, sid: string): Promise<SearchResult | null> {
    if (!state.tools || !state.vault) return null
    ensureRoster(sid)
    const scope = resolveScope(directory)
    const cmd = [
      "uv", "run", "--quiet", "--directory", state.tools,
      "python", "-m", "symbiosis_brain", "search-gist",
      "--vault", state.vault,
      "--prompt-from-stdin", "--envelope",
      "--limit", "5",
      "--session-id", sid,
    ]
    if (scope) cmd.push("--scope", scope)
    let proc: {
      stdin: { write(s: string): void; end(): void }
      stdout: ReadableStream<Uint8Array>
      exited: Promise<number>
      kill(): void
    }
    try {
      proc = Bun.spawn(cmd, { stdin: "pipe", stdout: "pipe", stderr: "ignore" })
    } catch {
      return null
    }
    const killer = setTimeout(() => {
      try { proc.kill() } catch {}
    }, timeoutMs)
    try {
      proc.stdin.write(JSON.stringify({ prompt: query }))
      proc.stdin.end()
    } catch {
      clearTimeout(killer)
      return null
    }
    let outText = ""
    try {
      outText = await new Response(proc.stdout).text()
    } catch {
      outText = ""
    }
    const code = await proc.exited
    clearTimeout(killer)
    if (code !== 0) return null
    try {
      const parsed = JSON.parse(outText.trim())
      if (parsed && typeof parsed === "object") return parsed as SearchResult
      return null
    } catch {
      return null
    }
  }

  function buildClaudePayload(tool: string, args: unknown): { tool_name: string; tool_input: Record<string, unknown> } | null {
    const a = (args && typeof args === "object" ? args : {}) as Record<string, unknown>
    const fp = typeof a.filePath === "string" ? a.filePath : ""
    if (tool === "edit") {
      if (!fp) return null
      return { tool_name: "Edit", tool_input: { file_path: fp, new_string: typeof a.newString === "string" ? a.newString : "" } }
    }
    if (tool === "write") {
      if (!fp) return null
      return { tool_name: "Write", tool_input: { file_path: fp, content: typeof a.content === "string" ? a.content : "" } }
    }
    if (tool === "bash" && bashOn) {
      const cmd = typeof a.command === "string" ? a.command : ""
      if (!cmd) return null
      return { tool_name: "Bash", tool_input: { command: cmd } }
    }
    return null
  }

  async function runPreActionRecall(payload: { tool_name: string; tool_input: Record<string, unknown> }, sid: string): Promise<string | null> {
    if (!state.tools || !state.vault) return null
    ensureRoster(sid)
    const scope = resolveScope(directory)
    const cmd = [
      "uv", "run", "--quiet", "--directory", state.tools,
      "python", "-m", "symbiosis_brain", "pre-action-recall",
      "--vault", state.vault,
    ]
    let proc: {
      stdin: { write(s: string): void; end(): void }
      stdout: ReadableStream<Uint8Array>
      exited: Promise<number>
      kill(): void
    }
    try {
      proc = Bun.spawn(cmd, {
        stdin: "pipe",
        stdout: "pipe",
        stderr: "ignore",
        env: { ...process.env, ...(scope ? { SYMBIOSIS_BRAIN_SCOPE: scope } : {}) },
      })
    } catch {
      return null
    }
    const killer = setTimeout(() => {
      try { proc.kill() } catch {}
    }, timeoutMs)
    try {
      proc.stdin.write(JSON.stringify({ tool_name: payload.tool_name, tool_input: payload.tool_input, session_id: sid }))
      proc.stdin.end()
    } catch {
      clearTimeout(killer)
      return null
    }
    let outText = ""
    try {
      outText = await new Response(proc.stdout).text()
    } catch {
      outText = ""
    }
    const code = await proc.exited
    clearTimeout(killer)
    if (code !== 0) return null
    try {
      const parsed = JSON.parse(outText.trim())
      const ctx = parsed?.hookSpecificOutput?.additionalContext
      if (typeof ctx === "string" && ctx.trim()) return ctx.trim()
      return null
    } catch {
      return null
    }
  }

  function formatBlock(res: SearchResult, query: string): string | null {
    const hits = Array.isArray(res.memory_hits) ? res.memory_hits : []
    const hints = Array.isArray(res.route_hints) ? res.route_hints : []
    if (hits.length === 0 && hints.length === 0) return null
    const q = query.length > 60 ? query.slice(0, 60) + "…" : query
    const lines: string[] = []
    if (hits.length > 0) {
      lines.push(`[memory: ${hits.length} hits for "${q}"]`)
      for (const h of hits) {
        const path = typeof h.path === "string" ? h.path : ""
        const gist = typeof h.gist === "string" ? h.gist : ""
        lines.push(`- ${path} — ${gist}`)
      }
    }
    if (hints.length > 0) {
      lines.push(`[routes: ${hints.length} hints]`)
      for (const r of hints) {
        const hint = typeof r.hint === "string" ? r.hint : ""
        lines.push(`- ${hint}`)
      }
    }
    return lines.join("\n")
  }

  async function recallBlock(sid: string, text: string): Promise<string | null> {
    if (!text || text.length < skipShort) return null
    const cached = state.injectedBlock.get(sid)
    if (cached && cached.text === text) return cached.block
    let p = state.pending.get(sid)
    if (!p) {
      p = runSearchGist(text, sid).then((res) => {
        state.pending.delete(sid)
        if (!res) return null
        const block = formatBlock(res, text)
        if (block) {
          state.injectedBlock.set(sid, { text, block })
          log("inject: " + block)
        }
        return block
      })
      state.pending.set(sid, p)
    }
    return p
  }

  return {
    config: async (cfg) => {
      const mcp = (cfg as any)?.mcp?.["symbiosis-brain"]
      if (!mcp) return
      state.vault = env.SYMBIOSIS_BRAIN_VAULT || mcp.environment?.SYMBIOSIS_BRAIN_VAULT || null
      state.tools = env.SYMBIOSIS_BRAIN_TOOLS || mcp.environment?.SYMBIOSIS_BRAIN_TOOLS || null
      log("config: vault=" + state.vault + " tools=" + state.tools)
      const mcpKeys = Object.keys((cfg as any)?.mcp || {})
      if (mcpKeys.length > 0) state.mcpNames = mcpKeys
      log("config: mcp=" + state.mcpNames.join(","))
    },
    event: async ({ event }) => {
      if (event.type !== "session.created") return
      if (disabled || state.prewarmed) return
      state.prewarmed = true
      if (!state.tools || !state.vault) return
      const cmd = [
        "uv", "run", "--quiet", "--directory", state.tools,
        "python", "-m", "symbiosis_brain", "prewarm", "--vault", state.vault,
      ]
      log("prewarm start")
      void (async () => {
        try {
          const proc = Bun.spawn(cmd, { stdin: "ignore", stdout: "ignore", stderr: "ignore" })
          await proc.exited
          log("prewarm done")
        } catch {
          log("prewarm failed")
        }
      })()
    },
    "chat.message": async (input, output) => {
      if (disabled) return
      const sid = input.sessionID || ""
      const text = partsToText((output as any)?.parts) || ""
      if (sid && text) state.lastText.set(sid, text)
    },
    "experimental.chat.system.transform": async (input, output) => {
      if (disabled) return
      try {
        const sid = (input as any)?.sessionID || ""
        const text = sid ? (state.lastText.get(sid) || "") : ""
        const block = await recallBlock(sid, text)
        if (block) output.system.push(block)
      } catch {}
    },
    "tool.execute.before": async (input, output) => {
      if (disabled || mode === "off") return
      const sid = input.sessionID || ""
      let ours: Error | null = null
      try {
        const mapped = buildClaudePayload(input.tool, output.args)
        if (!mapped) return
        const fileKey = String(mapped.tool_input.file_path || mapped.tool_input.command || "")
        if (!fileKey) return
        const warned = state.warnedFiles.get(sid)
        if (warned && warned.has(fileKey)) return
        if ((state.blockCount.get(sid) || 0) >= maxBlocks) return
        const ctx = await runPreActionRecall(mapped, sid)
        if (!ctx) return
        if (mode === "silent") {
          log("pre-action silent: " + ctx)
          return
        }
        const set = warned ?? new Set<string>()
        set.add(fileKey)
        state.warnedFiles.set(sid, set)
        state.blockCount.set(sid, (state.blockCount.get(sid) || 0) + 1)
        log("pre-action block: " + ctx)
        ours = new Error(ctx)
        throw ours
      } catch (e) {
        if (ours && e === ours) throw e
      }
    },
  }
}
