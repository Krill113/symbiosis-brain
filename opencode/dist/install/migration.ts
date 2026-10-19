import { createHash } from 'node:crypto';
import { readFile } from 'node:fs/promises';
import { join, resolve } from 'node:path';
import { readdir } from 'node:fs/promises';
import { homedir } from 'node:os';
import { parseConfig } from './config-edit.ts';

const LEGACY = new Map<string, string>([
  ['brain-recall.ts', '/* synthetic legacy adapter */\nexport default {}\n'],
  ['symbiosis-brain.ts', '/* synthetic legacy adapter */\nexport default {}\n'],
]);
const KNOWN_LEGACY_SHA256 = new Set(['eb96d5966006987162c143da4f156b228908531dc550deb10524f41dcda00c4d']);

export interface MigrationInput { roots: string[]; backupDir: string; decisions: Record<string, 'backup-and-remove' | 'keep'> }
export interface MigrationItem { path: string; kind: 'known' | 'unknown'; fingerprint: string; action: 'backup-and-remove' | 'keep' | 'decision-required'; backupPath?: string }
export interface MigrationPlan { items: MigrationItem[]; diagnostics: string[]; searchRoots: string[] }

const digest = (text: string) => createHash('sha256').update(text.replaceAll('\r\n', '\n')).digest('hex');

async function walk(root: string, dir: string, output: string[]): Promise<void> {
  let entries;
  try { entries = await readdir(dir, { withFileTypes: true }); } catch { return; }
  for (const entry of entries) {
    if (entry.name === 'node_modules' || entry.name === '.git' || entry.name === '.symbiosis-brain-install') continue;
    const path = join(dir, entry.name);
    if (entry.isDirectory()) await walk(root, path, output);
    else if (entry.isFile() && /brain-recall\.(?:ts|js|mjs)$|symbiosis-brain\.(?:ts|js|mjs)$/.test(entry.name)) output.push(path);
  }
}

export async function planMigration(input: MigrationInput): Promise<MigrationPlan> {
  const paths: string[] = [];
  for (const root of input.roots) await walk(root, root, paths);
  const items: MigrationItem[] = [];
  for (const path of paths) {
    const text = await readFile(path, 'utf8'); const fingerprint = digest(text);
    const known = LEGACY.get(path.split(/[\\/]/).pop()!) === text || KNOWN_LEGACY_SHA256.has(fingerprint);
    const decision = input.decisions[resolve(path)];
    items.push({ path, kind: known ? 'known' : 'unknown', fingerprint, action: decision ?? (known ? 'backup-and-remove' : 'decision-required') });
  }
  return { items, diagnostics: input.roots.map((root) => `project-local-scan-only:${resolve(root)}`), searchRoots: input.roots.map((root) => resolve(root)) };
}

export function defaultLegacyRoots(configDir: string, projectRoot = process.cwd(), home = homedir()): string[] {
  const xdg = process.env.XDG_CONFIG_HOME;
  const appData = process.env.APPDATA;
  const globalRoots = [join(configDir, 'plugin'), join(configDir, 'plugins')];
  if (process.env.OPENCODE_CONFIG) {
    const explicitConfigDir = resolve(process.env.OPENCODE_CONFIG, '..');
    globalRoots.push(join(explicitConfigDir, 'plugin'), join(explicitConfigDir, 'plugins'));
  }
  if (xdg) globalRoots.push(join(xdg, 'opencode', 'plugin'), join(xdg, 'opencode', 'plugins'));
  if (appData) globalRoots.push(join(appData, 'opencode', 'plugin'), join(appData, 'opencode', 'plugins'));
  globalRoots.push(join(home, '.config', 'opencode', 'plugin'), join(home, '.config', 'opencode', 'plugins'));
  return [...new Set([...globalRoots, join(projectRoot, '.opencode', 'plugin'), join(projectRoot, '.opencode', 'plugins'),
    join(projectRoot, 'opencode', 'plugin'), join(projectRoot, 'opencode', 'plugins')].map((path) => resolve(path)))];
}

export async function inspectProjectLegacy(projectRoot: string, globalConfigDir?: string): Promise<{ active: boolean; paths: string[]; diagnostics: string[] }> {
  const roots = defaultLegacyRoots(globalConfigDir ?? join(homedir(), '.config', 'opencode'), projectRoot);
  const plan = await planMigration({ roots, backupDir: '', decisions: {} });
  const references: string[] = [];
  const configDirs = new Set([projectRoot, join(projectRoot, '.opencode'), globalConfigDir ?? join(homedir(), '.config', 'opencode')]);
  if (process.env.OPENCODE_CONFIG) configDirs.add(resolve(process.env.OPENCODE_CONFIG, '..'));
  if (process.env.XDG_CONFIG_HOME) configDirs.add(join(process.env.XDG_CONFIG_HOME, 'opencode'));
  if (process.env.APPDATA) configDirs.add(join(process.env.APPDATA, 'opencode'));
  for (const dir of configDirs) for (const name of ['opencode.json', 'opencode.jsonc']) {
    const file = join(dir, name);
    let config: Record<string, unknown>;
    try { config = parseConfig(await readFile(file, 'utf8')); } catch { continue; }
    if (!Array.isArray(config.plugin)) continue;
    for (const entry of config.plugin) {
      const spec = typeof entry === 'string' ? entry : Array.isArray(entry) && typeof entry[0] === 'string' ? entry[0] : '';
      if (/brain-recall|symbiosis-brain-recall/i.test(spec)) references.push(spec);
    }
  }
  const paths = [...plan.items.map((item) => item.path), ...references];
  return { active: paths.length > 0, paths, diagnostics: [`project-local-scan-only:${resolve(projectRoot)}`] };
}

export { LEGACY as knownLegacyFiles };
