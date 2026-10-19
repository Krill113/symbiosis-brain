/** Capabilities projected from this session's connected/allowed state, never config keys. */
export interface CapabilitySnapshot {
  tools: Array<string | { name: string; enabled: boolean }>
  mcp: Array<{ name: string; connected: boolean; enabled?: boolean }>
  skills: Array<string | { name: string; available: boolean; source: 'package' | 'external' }>
  revision: number
  platform?: string
}

export interface Capabilities {
  tools: string[]
  mcp: string[]
  skills: string[]
  revision: number
  platform?: string
}

export function capabilitiesFromSnapshot(snapshot: CapabilitySnapshot): Capabilities {
  const tools = snapshot.tools.flatMap((item) => typeof item === 'string' ? [item] : item.enabled ? [item.name] : [])
  const mcp = snapshot.mcp.flatMap((item) => item.connected && item.enabled !== false ? [item.name] : [])
  const skills = snapshot.skills.flatMap((item) => typeof item === 'string' ? [] : item.available && item.source === 'package' ? [item.name] : [])
  return {
    tools: [...new Set(tools)].sort(), mcp: [...new Set(mcp)].sort(),
    skills: [...new Set(skills)].sort(), revision: Number.isSafeInteger(snapshot.revision) && snapshot.revision >= 0 ? snapshot.revision : 0,
    ...(snapshot.platform ? { platform: snapshot.platform } : {}),
  }
}
