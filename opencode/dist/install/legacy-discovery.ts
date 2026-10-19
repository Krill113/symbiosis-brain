import { inspectProjectLegacy } from './migration.ts';

export async function detectActiveLegacyAdapter(projectRoot: string): Promise<{ active: boolean; diagnostics: string[] }> {
  try {
    const result = await inspectProjectLegacy(projectRoot);
    return { active: result.active, diagnostics: result.active ? ['legacy-adapter-active-in-scope'] : [] };
  } catch {
    return { active: true, diagnostics: ['legacy-adapter-scan-failed'] };
  }
}
