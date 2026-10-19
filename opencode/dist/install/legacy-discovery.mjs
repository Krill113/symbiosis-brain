import { inspectProjectLegacy } from './migration.mjs';
export async function detectActiveLegacyAdapter(projectRoot) {
    try {
        const result = await inspectProjectLegacy(projectRoot);
        return { active: result.active, diagnostics: result.active ? ['legacy-adapter-active-in-scope'] : [] };
    }
    catch {
        return { active: true, diagnostics: ['legacy-adapter-scan-failed'] };
    }
}
