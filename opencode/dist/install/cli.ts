import { createHash, randomUUID } from 'node:crypto';
import { lstat, readFile, readdir, realpath, stat } from 'node:fs/promises';
import { spawnSync } from 'node:child_process';
import { dirname, isAbsolute, join, parse, relative, resolve, sep } from 'node:path';
import { homedir } from 'node:os';
import { renderCommands } from './commands.ts';
import { applyJsonChange, changeFromCurrent, hasJsonChangeCommentDrift, isJsonChangeAtBefore, validateHostConfig, type JsonChange } from './config-edit.ts';
import { defaultLegacyRoots, inspectProjectLegacy, planMigration } from './migration.ts';
import { acquireInstallLock, hashText, readOptional, type InstallJournal, type InstallLockRelease, type InstallOperation, type InstallReceipt } from './ownership.ts';

export interface InstallInput {
  configDir: string;
  artifactDir: string;
  pythonExe: string;
  vault: string;
  packageDir: string;
  skillsDir: string;
  mcpCommand: string[];
  tuiConfigDir?: string;
  projectRoot?: string;
  migration?: { roots: string[]; decisions: Record<string, 'backup-and-remove' | 'keep'> };
}
export interface ArtifactHashes { serverPath: string; serverSha256: string; tuiPath: string; tuiSha256: string }
export interface InstallPlan { root: string; journalPath: string; pythonExe: string; resourceInput: InstallInput; artifacts: ArtifactHashes; operations: InstallOperation[]; priorOperations: InstallOperation[]; baseReceiptTransactionId: string | null; transactionId: string; diagnostics: string[] }
export interface DoctorReport { status: 'installed' | 'absent' | 'incomplete' | 'conflict'; conflicts: string[]; diagnostics: string[]; journal?: string }
export interface RemoveResult { status: 'removed' | 'conflict'; conflicts: string[] }
export interface RecoveryResult { status: 'clean' | 'recovered' | 'conflict'; conflicts: string[] }
export interface ResourceReply { pythonExe: string; packageDir: string; skillsDir: string; mcpCommand: string[] }

const publicServerCode = 'from symbiosis_brain.install_cli import main; main()';
export function portableMcpCommand(pythonExe: string, vault: string): string[] {
  return [pythonExe, '-c', publicServerCode, 'serve', '--vault', vault];
}

export function installInputFromResources(input: Omit<InstallInput, 'pythonExe' | 'packageDir' | 'skillsDir' | 'mcpCommand'>, resources: ResourceReply): InstallInput {
  return { ...input, pythonExe: resources.pythonExe, packageDir: resources.packageDir, skillsDir: resources.skillsDir, mcpCommand: [...resources.mcpCommand] };
}

async function validateRuntimeResources(input: InstallInput): Promise<ArtifactHashes> {
  for (const required of [input.configDir, input.artifactDir, input.packageDir, input.skillsDir, input.vault]) {
    await validatePhysicalPath(required);
    const info = await stat(required).catch(() => undefined);
    if (!info?.isDirectory()) throw new Error(`resource-directory-missing:${required}`);
  }
  const serverPath = resolve(input.artifactDir, 'server.ts');
  const tuiPath = resolve(input.artifactDir, 'tui.tsx');
  await Promise.all([validatePhysicalPath(serverPath), validatePhysicalPath(tuiPath)]);
  const moduleContents: string[] = [];
  for (const modulePath of [serverPath, tuiPath]) {
    const info = await stat(modulePath).catch(() => undefined);
    if (!info?.isFile() || info.size === 0) throw new Error(`adapter-module-missing:${modulePath}`);
    moduleContents.push(await readFile(modulePath, 'utf8'));
  }
  if (!moduleContents[0]!.includes('createServerPlugin') || !moduleContents[0]!.includes('export default plugin')
    || !moduleContents[1]!.includes('TuiPluginModule') || !moduleContents[1]!.includes('export default plugin')) throw new Error('adapter-module-interface-invalid');
  for (const name of ['brain-init', 'brain-save']) {
    const skill = await stat(join(input.skillsDir, name, 'SKILL.md')).catch(() => undefined);
    if (!skill?.isFile() || skill.size === 0) throw new Error(`canonical-skill-missing:${name}`);
  }
  const code = 'import ast,importlib.util,json,sys; from pathlib import Path; from importlib.resources import files; p=Path(str(files("symbiosis_brain"))).resolve(); s=importlib.util.find_spec("symbiosis_brain.install_cli"); ok=bool(s and s.origin and any(isinstance(n,ast.FunctionDef) and n.name=="main" for n in ast.parse(Path(s.origin).read_text(encoding="utf-8")).body)); print(json.dumps({"python":str(Path(sys.executable).resolve()),"package":str(p),"skills":str((p/"skills" if (p/"skills").is_dir() else p.parent.parent/"skills").resolve()),"entry":ok}))';
  const probe = spawnSync(input.pythonExe, ['-c', code], { encoding: 'utf8', windowsHide: true, timeout: 10_000, maxBuffer: 64 * 1024 });
  if (probe.error || probe.status !== 0) throw new Error('python-package-resource-probe-failed');
  let actual: { python?: string; package?: string; skills?: string; entry?: boolean };
  try { actual = JSON.parse(probe.stdout) as typeof actual; } catch { throw new Error('python-package-resource-probe-invalid'); }
  const samePath = async (left: string | undefined, right: string) => {
    if (!left) return false;
    const [actualPath, expectedPath] = await Promise.all([realpath(left), realpath(right)]);
    return actualPath.toLocaleLowerCase() === expectedPath.toLocaleLowerCase();
  };
  const mismatches = [!(await samePath(actual.python, input.pythonExe)) && 'python', !(await samePath(actual.package, input.packageDir)) && 'package',
    !(await samePath(actual.skills, input.skillsDir)) && 'skills', actual.entry !== true && 'entry'].filter(Boolean);
  if (mismatches.length) throw new Error(`python-package-resources-mismatch:${mismatches.join(',')}`);
  return { serverPath, serverSha256: createHash('sha256').update(moduleContents[0]!).digest('hex'),
    tuiPath, tuiSha256: createHash('sha256').update(moduleContents[1]!).digest('hex') };
}

const ownDir = '.symbiosis-brain-install';
const ADAPTER_OWNER = 'symbiosis-brain-opencode-v1';
const SERVER_OWNER = `${ADAPTER_OWNER}:server`;
const TUI_OWNER = `${ADAPTER_OWNER}:tui`;
const SKILLS_OWNER = `${ADAPTER_OWNER}:skills`;
const MCP_OWNER = `${ADAPTER_OWNER}:mcp`;
const configNames = ['opencode.jsonc', 'opencode.json'];
const commandNames = ['brain-init', 'brain-save', 'brain-sync'] as const;
const brainSkillNames = new Set(['brain-init', 'brain-save']);

async function validatePhysicalPath(path: string): Promise<void> {
  const absolute = resolve(path); const root = parse(absolute).root;
  let current = root;
  const parts = absolute.slice(root.length).split(sep).filter(Boolean);
  for (const [index, part] of parts.entries()) {
    current = join(current, part);
    let info;
    try { info = await lstat(current); }
    catch (error) { if ((error as NodeJS.ErrnoException).code === 'ENOENT') break; throw error; }
    if (info.isSymbolicLink()) throw new Error(`installer-path-reparse:${current}`);
    if (index < parts.length - 1 && !info.isDirectory()) throw new Error(`installer-path-not-directory:${current}`);
  }
}

function validateOperation(value: unknown): InstallOperation {
  if (!value || typeof value !== 'object') throw new Error('ownership-operation-invalid');
  const operation = value as Partial<InstallOperation>;
  if (typeof operation.target !== 'string' || !isAbsolute(operation.target) || (operation.kind !== 'file' && operation.kind !== 'json')
    || (operation.before !== null && typeof operation.before !== 'string') || (operation.after !== null && typeof operation.after !== 'string')
    || !['pending', 'writing', 'done', 'reversing', 'reversed'].includes(operation.progress ?? '')
    || (operation.kind === 'json' && (!Array.isArray(operation.path) || operation.path.length === 0 || operation.path.some((part) => typeof part !== 'string' && typeof part !== 'number')))
    || (operation.backup !== undefined && typeof operation.backup !== 'string')
    || (operation.identity !== undefined && typeof operation.identity !== 'string')
    || (operation.beforeIdentity !== undefined && typeof operation.beforeIdentity !== 'string')
    || (operation.afterIdentity !== undefined && typeof operation.afterIdentity !== 'string')
    || (operation.ownershipId !== undefined && typeof operation.ownershipId !== 'string')) throw new Error('ownership-operation-invalid');
  return operation as InstallOperation;
}

function validateJournal(value: unknown): InstallJournal {
  if (!value || typeof value !== 'object') throw new Error('ownership-journal-invalid');
  const journal = value as Partial<InstallJournal>;
  if (journal.schema !== 1 || typeof journal.transactionId !== 'string' || typeof journal.root !== 'string'
    || !['prepared', 'applying', 'committed', 'conflict'].includes(journal.phase ?? '') || !Array.isArray(journal.operations)
    || typeof journal.artifacts?.serverPath !== 'string' || typeof journal.artifacts?.serverSha256 !== 'string'
    || typeof journal.artifacts?.tuiPath !== 'string' || typeof journal.artifacts?.tuiSha256 !== 'string'
    || !Array.isArray(journal.reversedTargets) || journal.reversedTargets.some((target) => typeof target !== 'string')
    || !Array.isArray(journal.targetManifest) || journal.targetManifest.some((target) => typeof target !== 'string')
    || typeof journal.pythonExe !== 'string' || typeof journal.packageDir !== 'string' || !isAbsolute(journal.packageDir)
    || typeof journal.updatedAt !== 'string') throw new Error('ownership-journal-invalid');
  const operations = journal.operations.map(validateOperation);
  validateOperationPaths(journal.root, operations);
  const targets = [...new Set(operations.map((operation) => resolve(operation.target)))].sort();
  if (JSON.stringify(targets) !== JSON.stringify([...journal.targetManifest].map((target) => resolve(target)).sort())) throw new Error('ownership-target-manifest-invalid');
  return { ...journal, operations } as InstallJournal;
}

function validateReceipt(value: unknown): InstallReceipt {
  if (!value || typeof value !== 'object') throw new Error('ownership-receipt-invalid');
  const receipt = value as Partial<InstallReceipt>;
  if (receipt.schema !== 1 || typeof receipt.transactionId !== 'string' || typeof receipt.root !== 'string'
    || typeof receipt.pythonExe !== 'string' || typeof receipt.artifacts?.serverPath !== 'string' || typeof receipt.artifacts?.serverSha256 !== 'string'
    || typeof receipt.artifacts?.tuiPath !== 'string' || typeof receipt.artifacts?.tuiSha256 !== 'string'
    || typeof receipt.journal !== 'string' || typeof receipt.installedAt !== 'string' || !Array.isArray(receipt.operations)
    || typeof receipt.packageDir !== 'string' || !isAbsolute(receipt.packageDir)
    || !Array.isArray(receipt.targetManifest) || receipt.targetManifest.some((target) => typeof target !== 'string')) throw new Error('ownership-receipt-invalid');
  const operations = receipt.operations.map(validateOperation);
  validateOperationPaths(receipt.root, operations);
  if (resolve(receipt.journal).toLocaleLowerCase() !== resolve(receipt.root, ownDir, 'journal.json').toLocaleLowerCase()) throw new Error('ownership-receipt-location-invalid');
  const targets = [...new Set(operations.map((operation) => resolve(operation.target)))].sort();
  if (JSON.stringify(targets) !== JSON.stringify([...receipt.targetManifest].map((target) => resolve(target)).sort())) throw new Error('ownership-target-manifest-invalid');
  return { ...receipt, operations } as InstallReceipt;
}

function validateOperationPaths(root: string, operations: InstallOperation[]): void {
  const backups = resolve(root, ownDir, 'backups');
  for (const operation of operations) {
    if (!isAbsolute(operation.target)) throw new Error('ownership-target-path-invalid');
    if (operation.backup) {
      const path = resolve(operation.backup); const rel = relative(backups, path);
      if (!rel || rel === '..' || rel.startsWith(`..${sep}`) || isAbsolute(rel)) throw new Error('ownership-backup-path-invalid');
    }
  }
}

async function validateReceiptFilesystem(receipt: InstallReceipt): Promise<void> {
  await validateInstallState(receipt.root);
  await validatePhysicalPath(receipt.packageDir);
  for (const target of receipt.targetManifest) await validatePhysicalPath(target);
  for (const operation of receipt.operations) if (operation.backup) await validatePhysicalPath(operation.backup);
}

function validateJournalLocation(journalPath: string, journal: InstallJournal): void {
  const expected = resolve(journal.root, ownDir, 'journal.json');
  if (resolve(journalPath).toLocaleLowerCase() !== expected.toLocaleLowerCase()
    || resolve(journal.root) !== journal.root) throw new Error('ownership-journal-location-invalid');
}

async function validateInstallState(root: string): Promise<void> {
  const state = join(root, ownDir);
  for (const path of [root, state, join(state, 'lock'), join(state, 'journal.json'), join(state, 'receipt.json'), join(state, 'backups')])
    await validatePhysicalPath(path);
}

async function duplicateSkillDiagnostics(canonicalDir: string): Promise<string[]> {
  const diagnostics: string[] = [];
  let canonical: string[] = [];
  try { canonical = await readdir(canonicalDir); } catch { diagnostics.push('canonical-skills-directory-missing'); }
  for (const clientDir of [join(homedir(), '.claude', 'skills'), join(homedir(), '.agents', 'skills')]) {
    let entries: string[];
    try { entries = await readdir(clientDir); } catch { continue; }
    for (const name of entries) {
      if (!brainSkillNames.has(name) || !canonical.includes(name)) continue;
      const external = await realpath(join(clientDir, name)).catch(() => undefined);
      const packaged = await realpath(join(canonicalDir, name)).catch(() => undefined);
      const normalize = (path: string) => process.platform === 'win32' ? path.toLocaleLowerCase() : path;
      if (external && packaged && normalize(external) === normalize(packaged)) continue;
      diagnostics.push(`skill-precedence-unknown:${name}`);
    }
  }
  return [...new Set(diagnostics)];
}

function joinConfig(...parts: string[]): string { return join(...parts); }
function jsonOperation(target: string, text: string, change: JsonChange, ownershipId?: string): InstallOperation {
  return { target, kind: 'json', baseContent: text, before: hashText(text), after: null, path: change.path, identity: change.identity,
    beforeIdentity: change.beforeIdentity, afterIdentity: change.afterIdentity,
    ...(ownershipId ? { ownershipId } : {}), beforeValue: change.before, afterValue: change.after, progress: 'pending' };
}

function operationChange(operation: InstallOperation): JsonChange {
  return { path: operation.path!, before: operation.beforeValue, after: operation.afterValue,
    ...(operation.identity ? { identity: operation.identity } : {}),
    ...(operation.beforeIdentity ? { beforeIdentity: operation.beforeIdentity } : {}),
    ...(operation.afterIdentity ? { afterIdentity: operation.afterIdentity } : {}) };
}

function isOwnedEmptyJson(text: string | null): boolean {
  return text !== null && /^\s*\{\s*\}\s*$/.test(text);
}

function containsJsoncComment(text: string): boolean {
  for (let index = 0; index < text.length; index++) {
    if (text[index] === '"') {
      index++;
      while (index < text.length) {
        if (text[index] === '\\') { index += 2; continue; }
        if (text[index] === '"') break;
        index++;
      }
    } else if (text[index] === '/' && (text[index + 1] === '/' || text[index + 1] === '*')) return true;
  }
  return false;
}

function ensureChanges(text: string, target: string, desired: Array<{ path: (string | number)[]; value: unknown; identity?: string; afterIdentity?: string; ownershipId?: string }>, operations: InstallOperation[]): string {
  let current = text;
  for (const item of desired) {
    const change = changeFromCurrent(current, item.path, item.value, item.identity, item.afterIdentity);
    if (JSON.stringify(change.before) === JSON.stringify(change.after)) continue;
    const operation = jsonOperation(target, current, change, item.ownershipId);
    operations.push(operation);
    current = applyJsonChange(current, change, 'forward');
    operation.after = hashText(current); operation.afterContent = current;
  }
  return current;
}

function addJsonChanges(target: string, current: string, operations: InstallOperation[], desired: Array<{ path: (string | number)[]; value: unknown; identity?: string; afterIdentity?: string; ownershipId?: string }>, backupRoot: string): void {
  const previousCount = operations.length;
  ensureChanges(current, target, desired, operations);
  if (operations.length > previousCount) {
    const backup = join(backupRoot, `${randomUUID()}.bak`);
    for (const operation of operations.slice(previousCount)) { operation.backup = backup; operation.backupContent = current; operation.baseContent = current; }
  }
}

function opFile(target: string, before: string | null, after: string | null, backup?: string): InstallOperation {
  return { target, kind: 'file', before, after, ...(backup ? { backup } : {}), progress: 'pending' };
}

function ownershipKey(operation: InstallOperation): string {
  if (operation.kind === 'json' && operation.ownershipId) return `json:${resolve(operation.target)}:owner:${operation.ownershipId}`;
  const identity = operation.identity ?? JSON.stringify(operation.path ?? []);
  return `${operation.kind}:${resolve(operation.target)}:${identity}`;
}

function mergeOwnership(prior: InstallOperation[], current: InstallOperation[]): InstallOperation[] {
  const merged = new Map(prior.map((operation) => [ownershipKey(operation), structuredClone(operation)]));
  for (const operation of current) {
    const key = ownershipKey(operation); const old = merged.get(key);
    if (old) {
      if (operation.kind === 'file') {
        merged.set(key, { ...operation, before: old.before, backup: old.backup ?? operation.backup, backupContent: old.backupContent ?? operation.backupContent });
      } else {
        merged.set(key, { ...operation, baseContent: old.baseContent, afterContent: operation.afterContent ?? old.afterContent, before: old.before, beforeValue: old.beforeValue,
          beforeIdentity: old.beforeIdentity ?? old.identity, backup: old.backup ?? operation.backup,
          backupContent: old.backupContent ?? operation.backupContent });
      }
    } else merged.set(key, structuredClone(operation));
  }
  return [...merged.values()];
}

async function verifyOwnedState(operations: InstallOperation[]): Promise<void> {
  const groups = new Map<string, InstallOperation[]>();
  for (const operation of operations) groups.set(operation.target, [...(groups.get(operation.target) ?? []), operation]);
  for (const group of groups.values()) {
    if ((await reverseTarget(group, await readOptional(group[0]!.target))).status === 'conflict') {
      const kind = group.some((operation) => operation.kind === 'json') ? 'config' : 'file';
      throw new Error(`owned-${kind}-conflict:${group[0]!.target}`);
    }
  }
}

async function recoverConditionalTargets(operations: InstallOperation[], pythonExe: string, lease: InstallLockRelease): Promise<void> {
  for (const target of new Set(operations.map((operation) => operation.target))) {
    await lease.recover(target, pythonExe);
  }
}

async function saveLockedJournal(path: string, journal: InstallJournal, release: InstallLockRelease): Promise<void> {
  release.assertHeld(); journal.updatedAt = new Date().toISOString();
  await release.write(path, `${JSON.stringify(journal, null, 2)}\n`);
}

async function addTargetFile(operations: InstallOperation[], target: string, content: string, backupRoot: string, ownedTargets: Map<string, string | null>): Promise<void> {
  const before = await readOptional(target);
  if (before === content) return;
  const expectedOwned = ownedTargets.get(resolve(target));
  if (before !== null && expectedOwned === undefined) throw new Error(`owned-command-conflict:${target}`);
  if (expectedOwned !== undefined && before !== expectedOwned) throw new Error(`owned-command-conflict:${target}`);
  let backup: string | undefined;
  if (before !== null) {
    backup = join(backupRoot, `${randomUUID()}.bak`);
  }
  operations.push(opFile(target, before, content, backup));
}

export async function planInstall(input: InstallInput): Promise<InstallPlan> {
  if (!input.configDir || !input.artifactDir || !input.pythonExe || !input.vault || !input.packageDir || !input.skillsDir || !input.mcpCommand?.length) throw new Error('invalid-install-input');
  if (JSON.stringify(input.mcpCommand) !== JSON.stringify(portableMcpCommand(input.pythonExe, input.vault))) throw new Error('unverified-brain-mcp-command');
  const artifacts = await validateRuntimeResources(input);
  const root = resolve(input.configDir); const state = join(root, ownDir); const journalPath = join(state, 'journal.json');
  await validateInstallState(root);
  if (await readOptional(journalPath)) {
    const recovery = await recoverInstall(journalPath);
    if (recovery.status === 'conflict') throw new Error(`unfinished-install-conflict:${recovery.conflicts.join(',')}`);
  }
  const operations: InstallOperation[] = []; const diagnostics: string[] = [];
  const backupRoot = join(state, 'backups');
  const ownedTargets = new Map<string, string | null>();
  let priorOperations: InstallOperation[] = [];
  let baseReceiptTransactionId: string | null = null;
  const priorReceiptText = await readOptional(join(state, 'receipt.json'));
  if (priorReceiptText) {
    const priorReceipt = validateReceipt(JSON.parse(priorReceiptText));
    if (resolve(priorReceipt.root).toLocaleLowerCase() !== root.toLocaleLowerCase()
      || resolve(priorReceipt.journal).toLocaleLowerCase() !== journalPath.toLocaleLowerCase()) throw new Error('ownership-receipt-location-invalid');
    await validateReceiptFilesystem(priorReceipt);
    baseReceiptTransactionId = priorReceipt.transactionId;
    priorOperations = priorReceipt.operations;
    for (const operation of priorReceipt.operations) if (operation.kind === 'file') ownedTargets.set(resolve(operation.target), operation.after);
  }
  let configPath = join(root, configNames[0]!);
  let existingConfig: string | null = null;
  for (const name of configNames) {
    const candidate = join(root, name);
    const value = await readOptional(candidate);
    if (value !== null) { configPath = candidate; existingConfig = value; break; }
  }
  let configText = await readOptional(configPath);
  if (existingConfig !== null) configText = existingConfig;
  if (configText === null) { operations.push(opFile(configPath, null, '{}\n')); configText = '{}\n'; }
  let config = validateHostConfig(configText);
  const migrationPlan = await planMigration({
    roots: [...defaultLegacyRoots(root, input.projectRoot ?? process.cwd()), ...(input.migration?.roots ?? [])],
    backupDir: join(state, 'backups'), decisions: input.migration?.decisions ?? {},
  });
  const pluginModule = resolve(input.artifactDir, 'server.ts');
  const tuiModule = resolve(input.artifactDir, 'tui.tsx');
  const plugin = Array.isArray(config.plugin) ? [...config.plugin] : [];
  const previousPluginOperation = priorOperations.find((operation) => operation.kind === 'json' && operation.path?.[0] === 'plugin' && operation.ownershipId === SERVER_OWNER);
  const previousPluginSpec = previousPluginOperation?.afterIdentity ?? previousPluginOperation?.identity;
  const mcp = config.mcp && typeof config.mcp === 'object' ? config.mcp as Record<string, unknown> : {};
  const existingBrainRegistration = Object.entries(mcp).find(([name, value]) => {
    if (!['brain', 'symbiosis-brain'].includes(name) || !value || typeof value !== 'object') return false;
    const server = value as Record<string, unknown>;
    const command = server.command;
    if (server.enabled === false || !Array.isArray(command) || command.length === 0 || command.some((part) => typeof part !== 'string')) return false;
    const isBrainCommand = command.some((part) => /symbiosis_brain\.(?:server|install_cli)/.test(part))
      || /(?:^|[\\/])symbiosis-brain(?:\.exe)?$/i.test(command[0] as string);
    return isBrainCommand;
  });
  const priorMcpOperation = priorOperations.find((operation) => operation.kind === 'json' && operation.path?.[0] === 'mcp'
    && operation.path.length === 2 && operation.ownershipId === MCP_OWNER);
  if (existingBrainRegistration) {
    const [name, value] = existingBrainRegistration;
    const server = value as Record<string, unknown>;
    const command = server.command as string[];
    const normalized = (part: string) => resolve(part).replaceAll('\\', '/').toLocaleLowerCase();
    const expected = input.mcpCommand;
    const sameCommand = command.length === expected.length && command.every((part, index) => index === 0
      ? normalized(part) === normalized(expected[index]!) : index === expected.indexOf('--vault') + 1
        ? normalized(part) === normalized(expected[index]!) : part === expected[index]);
    const previouslyOwned = priorMcpOperation && priorMcpOperation.path?.[1] === name
      && JSON.stringify(server) === JSON.stringify(priorMcpOperation.afterValue);
    const environment = server.environment && typeof server.environment === 'object' ? server.environment as Record<string, unknown> : {};
    const envVault = environment.SYMBIOSIS_BRAIN_VAULT;
    if (!['brain', 'symbiosis-brain'].includes(name) || (!sameCommand && !previouslyOwned)
      || (!previouslyOwned && envVault !== undefined && (typeof envVault !== 'string' || normalized(envVault) !== normalized(input.vault))))
      throw new Error('brain-mcp-existing-binding-conflict');
  }
  const ownsPlugin = (entry: unknown) => Array.isArray(entry) && typeof entry[0] === 'string' && entry[1] && typeof entry[1] === 'object'
    && ((entry[1] as Record<string, unknown>).adapterId === ADAPTER_OWNER || entry[0] === previousPluginSpec);
  const existingOwnedIndex = plugin.findIndex(ownsPlugin);
  const existingOwned = existingOwnedIndex >= 0 ? plugin[existingOwnedIndex] : undefined;
  const conflictingNewModule = plugin.find((entry) => Array.isArray(entry) && entry[0] === pluginModule && !ownsPlugin(entry));
  if (conflictingNewModule) throw new Error('server-plugin-module-conflict');
  const pluginTuple = [pluginModule, { adapterId: ADAPTER_OWNER, enabled: true, python: input.pythonExe, tools: input.packageDir, vault: input.vault,
    shell: process.platform === 'win32' ? 'powershell' : 'bash', mcpServer: existingBrainRegistration?.[0] ?? 'symbiosis-brain' }];
  const pluginBeforeIdentity = existingOwned && Array.isArray(existingOwned) && typeof existingOwned[0] === 'string' ? existingOwned[0] : undefined;
  if (existingOwnedIndex >= 0) plugin[existingOwnedIndex] = pluginTuple;
  else plugin.push(pluginTuple);
  const desired: Array<{ path: (string | number)[]; value: unknown; identity?: string; afterIdentity?: string; ownershipId?: string }> = [];
  for (let index = 0; index < plugin.length; index++) {
    const entry = plugin[index];
    const spec = typeof entry === 'string' ? entry : Array.isArray(entry) && typeof entry[0] === 'string' ? entry[0] : '';
    if (!/brain-recall|symbiosis-brain-recall/i.test(spec) || spec === pluginModule || spec === previousPluginSpec) continue;
    const candidate = resolve(root, spec);
    const legacy = migrationPlan.items.find((item) => resolve(item.path) === candidate);
    if (!legacy || legacy.action === 'decision-required' || legacy.action === 'keep') throw new Error(`active-legacy-plugin-conflict:${spec}`);
    desired.push({ path: ['plugin', index], value: undefined, identity: spec });
  }
  if (config.plugin === undefined || !Array.isArray(config.plugin)) desired.push({ path: ['plugin'], value: plugin });
  else if (existingOwnedIndex >= 0) desired.push({ path: ['plugin', existingOwnedIndex], value: pluginTuple,
    identity: pluginBeforeIdentity, afterIdentity: pluginModule, ownershipId: SERVER_OWNER });
  else desired.push({ path: ['plugin', config.plugin.length], value: pluginTuple, identity: pluginModule, ownershipId: SERVER_OWNER });

  if (config.mcp === undefined) desired.push({ path: ['mcp'], value: {} });
  if (existingBrainRegistration && !priorMcpOperation) diagnostics.push('existing-correct-brain-mcp-preserved');
  if (existingBrainRegistration && priorMcpOperation) {
    const [name, value] = existingBrainRegistration;
    const server = value as Record<string, unknown>;
    if (JSON.stringify(server) !== JSON.stringify(priorMcpOperation.afterValue)) throw new Error('brain-mcp-owned-binding-conflict');
    const next = { ...server, command: input.mcpCommand };
    desired.push({ path: ['mcp', name], value: next, ownershipId: MCP_OWNER });
  }
  else {
    if (existingBrainRegistration) { /* verified exact external registration remains user-owned */ }
    else {
      if ('symbiosis-brain' in mcp) throw new Error('brain-mcp-name-conflict');
      desired.push({ path: ['mcp', 'symbiosis-brain'], value: { type: 'local', command: input.mcpCommand, enabled: true }, ownershipId: MCP_OWNER });
    }
  }
  const skills = config.skills && typeof config.skills === 'object' ? config.skills as Record<string, unknown> : {};
  if (config.skills === undefined) desired.push({ path: ['skills'], value: {} });
  const paths = Array.isArray(skills.paths) ? skills.paths : [];
  if (skills.paths === undefined) desired.push({ path: ['skills', 'paths'], value: [] });
  const previousSkillsOperation = priorOperations.find((operation) => operation.kind === 'json' && operation.path?.[0] === 'skills' && operation.path?.[1] === 'paths' && operation.ownershipId === SKILLS_OWNER);
  const previousSkillsPath = previousSkillsOperation?.afterIdentity ?? previousSkillsOperation?.identity;
  if (!paths.includes(input.skillsDir)) {
    const oldPathIndex = previousSkillsPath ? paths.indexOf(previousSkillsPath) : -1;
    if (oldPathIndex >= 0) desired.push({ path: ['skills', 'paths', oldPathIndex], value: input.skillsDir,
      identity: previousSkillsPath, afterIdentity: input.skillsDir, ownershipId: SKILLS_OWNER });
    else desired.push({ path: ['skills', 'paths', paths.length], value: input.skillsDir, identity: input.skillsDir, ownershipId: SKILLS_OWNER });
  }
  addJsonChanges(configPath, configText, operations, desired, backupRoot);

  const projectRoot = resolve(input.projectRoot ?? process.cwd());
  for (const projectConfigDir of [projectRoot, join(projectRoot, '.opencode')]) {
    for (const name of configNames) {
      const target = join(projectConfigDir, name);
      const projectText = await readOptional(target);
      if (projectText === null || target === configPath) continue;
      const projectConfig = validateHostConfig(projectText);
      if (!Array.isArray(projectConfig.plugin)) continue;
      const projectPlugins = projectConfig.plugin;
      const removals: Array<{ path: (string | number)[]; value: unknown; identity?: string }> = [];
      for (let index = 0; index < projectPlugins.length; index++) {
        const entry = projectPlugins[index];
        const spec = typeof entry === 'string' ? entry : Array.isArray(entry) && typeof entry[0] === 'string' ? entry[0] : '';
        if (!/brain-recall|symbiosis-brain-recall/i.test(spec)) continue;
        const legacyPath = resolve(projectRoot, spec);
        const legacy = migrationPlan.items.find((item) => resolve(item.path) === legacyPath);
        if (!legacy || legacy.action !== 'backup-and-remove') throw new Error(`project-legacy-plugin-reference-conflict:${target}`);
        removals.push({ path: ['plugin', index], value: undefined, identity: spec });
      }
      if (removals.length) addJsonChanges(target, projectText, operations, removals, backupRoot);
    }
  }

  const tuiDir = resolve(input.tuiConfigDir ?? root);
  const tuiPath = join(tuiDir, 'tui.json');
  let tuiText = await readOptional(tuiPath);
  if (tuiText === null) { operations.push(opFile(tuiPath, null, '{}\n')); tuiText = '{}\n'; }
  const tuiBaseText = tuiText;
  const tuiConfig = validateHostConfig(tuiText);
  const tuiPlugins = Array.isArray(tuiConfig.plugin) ? [...tuiConfig.plugin] : [];
  const tuiSpec = tuiModule;
  const priorTuiOperation = priorOperations.find((operation) => operation.kind === 'json' && operation.target === tuiPath && operation.path?.[0] === 'plugin' && operation.ownershipId === TUI_OWNER);
  const priorTuiSpec = priorTuiOperation?.afterIdentity ?? priorTuiOperation?.identity;
  const ownsTui = (entry: unknown) => Array.isArray(entry) && typeof entry[0] === 'string' && entry[1] && typeof entry[1] === 'object'
    && ((entry[1] as Record<string, unknown>).adapterId === ADAPTER_OWNER || entry[0] === priorTuiSpec);
  const ownedTuiIndex = tuiPlugins.findIndex(ownsTui);
  const conflictingTuiModule = tuiPlugins.find((entry) => Array.isArray(entry) && entry[0] === tuiSpec && !ownsTui(entry));
  if (conflictingTuiModule) throw new Error('tui-plugin-module-conflict');
  const tuiTuple = [tuiSpec, { adapterId: ADAPTER_OWNER, enabled: true }];
  if (ownedTuiIndex >= 0) {
    const oldSpec = Array.isArray(tuiPlugins[ownedTuiIndex]) ? tuiPlugins[ownedTuiIndex]![0] as string : undefined;
    const change = changeFromCurrent(tuiText, ['plugin', ownedTuiIndex], tuiTuple, oldSpec, tuiSpec);
    const op = jsonOperation(tuiPath, tuiText, change, TUI_OWNER); operations.push(op); tuiText = applyJsonChange(tuiText, change, 'forward'); op.after = hashText(tuiText); op.afterContent = tuiText;
  } else {
    if (tuiPlugins.some((entry) => entry === tuiSpec)) throw new Error('tui-plugin-module-conflict');
    if (tuiConfig.plugin === undefined) {
      const change = changeFromCurrent(tuiText, ['plugin'], []);
      const operation = jsonOperation(tuiPath, tuiText, change); operations.push(operation); tuiText = applyJsonChange(tuiText, change, 'forward'); operation.after = hashText(tuiText); operation.afterContent = tuiText;
    }
    const change = changeFromCurrent(tuiText, ['plugin', tuiPlugins.length], tuiTuple, tuiSpec);
    const op = jsonOperation(tuiPath, tuiText, change, TUI_OWNER); operations.push(op); tuiText = applyJsonChange(tuiText, change, 'forward'); op.after = hashText(tuiText); op.afterContent = tuiText;
  }
  for (const operation of operations.filter((op) => op.target === tuiPath && op.kind === 'json' && !op.backup)) {
    operation.backup = join(backupRoot, `${randomUUID()}.bak`); operation.backupContent = tuiBaseText; operation.baseContent = tuiBaseText;
  }
  const commands = renderCommands();
  for (const name of commandNames) await addTargetFile(operations, join(root, 'command', `${name}.md`), commands[name], backupRoot, ownedTargets);
  const skillDiagnostics = await duplicateSkillDiagnostics(input.skillsDir);
  diagnostics.push(...skillDiagnostics);
  if (skillDiagnostics.some((item) => item.startsWith('skill-precedence-unknown:'))) throw new Error(`canonical-skill-precedence-conflict:${skillDiagnostics.join(',')}`);

  diagnostics.push(...migrationPlan.diagnostics);
  for (const item of migrationPlan.items) {
    if (item.action === 'decision-required') throw new Error(`migration-decision-required:${item.path}`);
    if (item.action === 'keep') throw new Error(`active-legacy-plugin-kept-conflict:${item.path}`);
    if (item.action !== 'backup-and-remove') continue;
    const backup = join(backupRoot, `${item.fingerprint}-${item.path.split(/[\\/]/).pop()}`);
    operations.push(opFile(item.path, await readOptional(item.path), null, backup));
  }

  for (const op of operations) {
    if (op.kind === 'file' && op.before !== null && op.backup) diagnostics.push(`backup:${op.backup}`);
  }
  return { root, journalPath, pythonExe: input.pythonExe, resourceInput: structuredClone(input), artifacts, operations, priorOperations, baseReceiptTransactionId, transactionId: randomUUID(), diagnostics };
}

async function writeOperation(operation: InstallOperation, pythonExe: string, release: InstallLockRelease): Promise<void> {
  if (operation.kind === 'file') {
    await release.recover(operation.target, pythonExe);
    const currentFile = await readOptional(operation.target);
    if (currentFile !== operation.before && currentFile !== operation.after) throw new Error(`install-conflict:${operation.target}`);
    if (currentFile === operation.after) return;
    if (operation.backup && operation.before !== null) {
      const existing = await readOptional(operation.backup);
      if (existing !== null && existing !== operation.before) throw new Error('backup-conflict');
      if (existing === null) await release.write(operation.backup, operation.before);
    }
    await release.conditional(operation.target, operation.after, hashText(currentFile), pythonExe);
    return;
  }
  if (operation.backup && operation.backupContent !== undefined) {
    const existing = await readOptional(operation.backup);
    if (existing !== null && existing !== operation.backupContent) throw new Error('backup-conflict');
    if (existing === null) await release.write(operation.backup, operation.backupContent);
  }
  await release.recover(operation.target, pythonExe);
  const current = await readOptional(operation.target);
  if (current === null) throw new Error(`missing-json-target:${operation.target}`);
  const updated = applyJsonChange(current, operationChange(operation), 'forward');
  validateHostConfig(updated);
  await release.conditional(operation.target, updated, hashText(current), pythonExe);
}

async function preflightOperations(operations: InstallOperation[]): Promise<void> {
  const targets = new Map<string, InstallOperation[]>();
  for (const operation of operations) targets.set(operation.target, [...(targets.get(operation.target) ?? []), operation]);
  for (const [target, group] of targets) {
    let current = await readOptional(target);
    for (const operation of group) {
      if (operation.kind === 'file') {
        if (current !== operation.before && current !== operation.after) throw new Error(`stale-install-plan:${target}`);
        current = operation.after;
      } else {
        if (current === null) throw new Error(`stale-install-plan:${target}`);
        try {
          current = applyJsonChange(current, operationChange(operation), 'forward');
          validateHostConfig(current);
        } catch { throw new Error(`stale-install-plan:${target}`); }
      }
    }
  }
}

export async function applyInstall(plan: InstallPlan, options: { failAfter?: number; crashAfterWrite?: number; crashDuringWrite?: number; crashAfterReceipt?: boolean } = {}): Promise<InstallReceipt> {
  if (resolve(plan.resourceInput.configDir).toLocaleLowerCase() !== resolve(plan.root).toLocaleLowerCase()
    || resolve(plan.resourceInput.pythonExe).toLocaleLowerCase() !== resolve(plan.pythonExe).toLocaleLowerCase()
    || JSON.stringify(portableMcpCommand(plan.resourceInput.pythonExe, plan.resourceInput.vault)) !== JSON.stringify(plan.resourceInput.mcpCommand))
    throw new Error('stale-install-plan-resource-binding');
  const revalidatedArtifacts = await validateRuntimeResources(plan.resourceInput);
  if (JSON.stringify(revalidatedArtifacts) !== JSON.stringify(plan.artifacts)) throw new Error('stale-install-plan-resource-changed');
  await validateInstallState(plan.root);
  for (const target of new Set(plan.operations.map((operation) => operation.target))) await validatePhysicalPath(target);
  const release = await acquireInstallLock(plan.root, plan.pythonExe, plan.resourceInput.packageDir);
  try {
    if (await readOptional(plan.journalPath)) {
      const recovery = await recoverJournalUnlocked(plan.journalPath, { lease: release });
      if (recovery.status === 'conflict') throw new Error(`unfinished-install-conflict:${recovery.conflicts.join(',')}`);
    }
    const receiptPath = join(dirname(plan.journalPath), 'receipt.json');
    const receiptText = await readOptional(receiptPath);
    const previousReceipt = receiptText ? validateReceipt(JSON.parse(receiptText)) : undefined;
    if ((previousReceipt?.transactionId ?? null) !== plan.baseReceiptTransactionId) throw new Error('stale-install-plan-receipt-changed');
    if (previousReceipt) {
      await validateReceiptFilesystem(previousReceipt);
      await recoverConditionalTargets(previousReceipt.operations, plan.pythonExe, release);
      await verifyOwnedState(previousReceipt.operations);
    }
    await recoverConditionalTargets(plan.operations, plan.pythonExe, release);
    await preflightOperations(plan.operations);
    const serverBytes = await readFile(plan.artifacts.serverPath);
    const tuiBytes = await readFile(plan.artifacts.tuiPath);
    if (createHash('sha256').update(serverBytes).digest('hex') !== plan.artifacts.serverSha256
      || createHash('sha256').update(tuiBytes).digest('hex') !== plan.artifacts.tuiSha256) throw new Error('stale-install-plan-artifact-changed');
    const state = dirname(plan.journalPath); await release.mkdir(state);
    const journal: InstallJournal = { schema: 1, transactionId: plan.transactionId, phase: 'prepared', root: plan.root, pythonExe: plan.pythonExe, packageDir: plan.resourceInput.packageDir, artifacts: plan.artifacts, operations: structuredClone(plan.operations), targetManifest: [...new Set(plan.operations.map((operation) => resolve(operation.target)))], reversedTargets: [], updatedAt: new Date().toISOString() };
    await saveLockedJournal(plan.journalPath, journal, release);
    journal.phase = 'applying'; await saveLockedJournal(plan.journalPath, journal, release);
    let writes = 0;
    for (const op of journal.operations) {
      op.progress = 'writing'; await saveLockedJournal(plan.journalPath, journal, release);
      if (options.crashDuringWrite === writes + 1) process.kill(process.pid, 'SIGKILL');
      await release.mkdir(dirname(op.target));
      if (op.backup) await release.mkdir(dirname(op.backup));
      await writeOperation(op, plan.pythonExe, release);
      op.progress = 'done'; writes++; await saveLockedJournal(plan.journalPath, journal, release);
      if (options.crashAfterWrite === writes) process.kill(process.pid, 'SIGKILL');
      if (options.failAfter === writes) throw new Error('injected-install-failure');
    }
    const receipt: InstallReceipt = { schema: 1, transactionId: journal.transactionId, root: plan.root, pythonExe: plan.pythonExe, packageDir: plan.resourceInput.packageDir, artifacts: plan.artifacts,
      journal: plan.journalPath, operations: mergeOwnership(plan.priorOperations, journal.operations),
      targetManifest: [...new Set(mergeOwnership(plan.priorOperations, journal.operations).map((operation) => resolve(operation.target)))],
      installedAt: new Date().toISOString() };
    await release.write(join(state, 'receipt.json'), `${JSON.stringify(receipt, null, 2)}\n`);
    if (options.crashAfterReceipt) process.kill(process.pid, 'SIGKILL');
    journal.phase = 'committed'; await saveLockedJournal(plan.journalPath, journal, release);
    await release.remove(plan.journalPath);
    return receipt;
  } finally { await release(); }
}

export async function reverseTarget(operations: InstallOperation[], observed: string | null): Promise<{ status: 'ok' | 'conflict'; target: string; content: string | null; reason?: string }> {
  const target = operations[0]!.target;
  let current = observed;
  const baseline = operations[0]!.baseContent ?? (operations[0]!.kind === 'file' ? operations[0]!.before : undefined);
  if (baseline !== undefined && current === baseline) return { status: 'ok', target, content: current };
  const original = operations.find((operation) => operation.baseContent !== undefined)?.baseContent;
  const finalOwnedHash = [...operations].reverse().find((operation) => operation.after !== null)?.after;
  const createdFile = operations.find((operation) => operation.kind === 'file' && operation.before === null);
  if (createdFile && current !== null && (finalOwnedHash === undefined || hashText(current) !== finalOwnedHash) && containsJsoncComment(current))
    return { status: 'conflict', target, content: current, reason: 'user-comment-in-created-config' };
  if (original !== undefined && current !== null && finalOwnedHash !== undefined && hashText(current) === finalOwnedHash) {
    return { status: 'ok', target, content: createdFile ? null : original };
  }
  const alreadyBefore = operations.every((operation) => operation.kind === 'file'
    ? current === operation.before
    : (current === null && operation.beforeValue === undefined) || (current !== null && isJsonChangeAtBefore(current, operationChange(operation))));
  if (alreadyBefore) {
    const fileOperation = operations.find((operation) => operation.kind === 'file');
    return { status: 'ok', target, content: fileOperation ? fileOperation.before : current };
  }
  for (const operation of [...operations].reverse()) {
    if (operation.kind === 'file') {
      if (current === operation.before) continue;
      if (current !== operation.after && !(operation.before === null && isOwnedEmptyJson(current)))
        if (operation.before === null && current !== null) {
          try { validateHostConfig(current); }
          catch { return { status: 'conflict', target, content: current, reason: 'file-value-mismatch' }; }
        } else return { status: 'conflict', target, content: current, reason: 'file-value-mismatch' };
      if (operation.before === null && current !== null && !isOwnedEmptyJson(current)) return { status: 'ok', target, content: current };
      current = operation.before;
    } else {
      if (current === null) return { status: 'conflict', target, content: null, reason: 'json-target-missing' };
      try {
        if (operation.afterContent && hasJsonChangeCommentDrift(operation.afterContent, current, operationChange(operation)))
          return { status: 'conflict', target, content: current, reason: 'comment-changed-on-owned-json-node' };
        current = applyJsonChange(current, operationChange(operation), 'reverse');
        validateHostConfig(current);
      }
      catch (error) { return { status: 'conflict', target, content: current, reason: error instanceof Error ? error.message : 'json-value-mismatch' }; }
    }
  }
  return { status: 'ok', target, content: current };
}

async function recoverJournalUnlocked(journalPath: string, options: { crashAfterReverseWrite?: number; crashDuringReverseWrite?: number; lease: InstallLockRelease }): Promise<RecoveryResult> {
  const text = await readOptional(journalPath); if (text === null) return { status: 'clean', conflicts: [] };
  const journal = validateJournal(JSON.parse(text));
  validateJournalLocation(journalPath, journal);
  await validatePhysicalPath(journal.packageDir);
  for (const target of journal.targetManifest) await validatePhysicalPath(target);
  for (const operation of journal.operations) if (operation.backup) await validatePhysicalPath(operation.backup);
  const receiptText = await readOptional(join(dirname(journalPath), 'receipt.json'));
  if (receiptText) {
    try {
      const receipt = validateReceipt(JSON.parse(receiptText));
      if (receipt.schema === 1 && receipt.transactionId === journal.transactionId) {
        await options.lease.remove(journalPath);
        return { status: 'recovered', conflicts: [] };
      }
    } catch { /* An invalid receipt does not certify transaction commit. */ }
  }
  if (journal.phase === 'committed') {
    await options.lease.remove(journalPath);
    return { status: 'recovered', conflicts: [] };
  }
  const groups = new Map<string, InstallOperation[]>();
  for (const operation of journal.operations) if (operation.progress !== 'pending' && operation.progress !== 'reversed') {
    groups.set(operation.target, [...(groups.get(operation.target) ?? []), operation]);
  }
  const conflicts: string[] = [];
  let reverseWrites = 0;
  for (const group of [...groups.values()].reverse()) {
    const target = group[0]!.target;
    if (journal.reversedTargets.includes(target)) continue;
    await options.lease.recover(target, journal.pythonExe);
    const observed = await readOptional(target);
    const plan = await reverseTarget(group, observed);
    if (plan.status === 'conflict') { conflicts.push(`${target}:${plan.reason ?? 'ownership-conflict'}`); continue; }
    for (const operation of group) operation.progress = 'reversing';
    await saveLockedJournal(journalPath, journal, options.lease);
    if (options.crashDuringReverseWrite === reverseWrites + 1) process.kill(process.pid, 'SIGKILL');
    // Absent-to-absent recovery under a missing parent has no filesystem mutation;
    // a later user-created path is not owned by this transaction.
    const missingParent = await stat(dirname(target)).then(() => false).catch((error: NodeJS.ErrnoException) => {
      if (error.code === 'ENOENT') return true;
      throw error;
    });
    if (observed !== null || plan.content !== null || !missingParent) {
      try { await options.lease.conditional(target, plan.content, hashText(observed), journal.pythonExe); }
      catch (error) { conflicts.push(`${target}:${error instanceof Error ? error.message : 'conditional-commit-failed'}`); continue; }
    }
    reverseWrites++;
    if (options.crashAfterReverseWrite === reverseWrites) process.kill(process.pid, 'SIGKILL');
    for (const operation of group) operation.progress = 'reversed';
    journal.reversedTargets.push(target);
    await saveLockedJournal(journalPath, journal, options.lease);
  }
  journal.phase = conflicts.length ? 'conflict' : 'committed';
  await saveLockedJournal(journalPath, journal, options.lease);
  if (!conflicts.length) await options.lease.remove(journalPath);
  return { status: conflicts.length ? 'conflict' : 'recovered', conflicts };
}

export async function recoverInstall(journalPath: string, options: { crashAfterReverseWrite?: number; crashDuringReverseWrite?: number } = {}): Promise<RecoveryResult> {
  await validatePhysicalPath(journalPath);
  const text = await readOptional(journalPath); if (text === null) return { status: 'clean', conflicts: [] };
  const journal = validateJournal(JSON.parse(text));
  validateJournalLocation(journalPath, journal);
  await validateInstallState(journal.root);
  await validatePhysicalPath(journal.packageDir);
  const release = await acquireInstallLock(journal.root, journal.pythonExe, journal.packageDir);
  try { return await recoverJournalUnlocked(journalPath, { ...options, lease: release }); }
  finally { await release(); }
}

export async function inspectInstall(root: string): Promise<DoctorReport> {
  await validateInstallState(resolve(root));
  const journal = join(resolve(root), ownDir, 'journal.json');
  const pending = await readOptional(journal);
  if (pending) {
    const pendingJournal = validateJournal(JSON.parse(pending));
    if (resolve(pendingJournal.root).toLocaleLowerCase() !== resolve(root).toLocaleLowerCase())
      return { status: 'conflict', conflicts: ['journal-root-mismatch'], diagnostics: ['journal-root-mismatch'], journal };
    let recovery: RecoveryResult;
    try { recovery = await recoverInstall(journal); }
    catch (error) {
      if (error instanceof Error && error.message === 'installation-locked') return { status: 'incomplete', conflicts: [], diagnostics: ['installation-currently-active', `project-local-scan:${resolve(root)}`], journal };
      throw error;
    }
    if (recovery.status === 'conflict') return { status: 'conflict', conflicts: recovery.conflicts, diagnostics: ['unfinished-transaction-conflict', `project-local-scan:${resolve(root)}`], journal };
  }
  const receiptText = await readOptional(join(resolve(root), ownDir, 'receipt.json'));
  if (!receiptText) {
    const migration = await planMigration({ roots: defaultLegacyRoots(resolve(root)), backupDir: join(resolve(root), ownDir, 'backups'), decisions: {} });
    const projectLegacy = await inspectProjectLegacy(process.cwd(), resolve(root));
    const legacyConflicts = [...migration.items.map((item) => item.path), ...projectLegacy.paths];
    return { status: legacyConflicts.length ? 'conflict' : 'absent', conflicts: [...new Set(legacyConflicts)], diagnostics: [...migration.diagnostics, ...projectLegacy.diagnostics] };
  }
  const receipt = validateReceipt(JSON.parse(receiptText)); const conflicts: string[] = [];
  if (resolve(receipt.root).toLocaleLowerCase() !== resolve(root).toLocaleLowerCase()
    || resolve(receipt.journal).toLocaleLowerCase() !== resolve(root, ownDir, 'journal.json').toLocaleLowerCase())
    return { status: 'conflict', conflicts: ['receipt-root-mismatch'], diagnostics: ['receipt-root-mismatch'] };
  await validateReceiptFilesystem(receipt);
  for (const [path, expectedHash] of [[receipt.artifacts.serverPath, receipt.artifacts.serverSha256], [receipt.artifacts.tuiPath, receipt.artifacts.tuiSha256]] as const) {
    const bytes = await readFile(path).catch(() => undefined);
    if (!bytes || createHash('sha256').update(bytes).digest('hex') !== expectedHash) conflicts.push(path);
  }
  const byTarget = new Map<string, InstallOperation[]>();
  for (const op of receipt.operations) byTarget.set(op.target, [...(byTarget.get(op.target) ?? []), op]);
  for (const group of byTarget.values()) {
    if ((await reverseTarget(group, await readOptional(group[0]!.target))).status === 'conflict') conflicts.push(group[0]!.target);
  }
  const migration = await planMigration({ roots: defaultLegacyRoots(resolve(root)), backupDir: join(resolve(root), ownDir, 'backups'), decisions: {} });
  const legacyConflicts = migration.items.map((item) => item.path);
  const projectLegacy = await inspectProjectLegacy(process.cwd(), resolve(root));
  legacyConflicts.push(...projectLegacy.paths);
  const installedSkillsPath = receipt.operations.find((operation) => operation.kind === 'json' && operation.path?.[0] === 'skills' && operation.path?.[1] === 'paths' && typeof operation.afterValue === 'string')?.afterValue;
  const skillDiagnostics = typeof installedSkillsPath === 'string' ? await duplicateSkillDiagnostics(installedSkillsPath) : ['canonical-skills-path-not-owned'];
  return { status: conflicts.length || legacyConflicts.length || skillDiagnostics.length > 0 ? 'conflict' : 'installed', conflicts: [...new Set([...conflicts, ...legacyConflicts, ...skillDiagnostics])], diagnostics: [...migration.diagnostics, ...projectLegacy.diagnostics, ...skillDiagnostics] };
}

export async function removeOwned(receipt: InstallReceipt, expectedRoot: string): Promise<RemoveResult> {
  receipt = validateReceipt(receipt);
  if (resolve(expectedRoot).toLocaleLowerCase() !== resolve(receipt.root).toLocaleLowerCase()) return { status: 'conflict', conflicts: ['receipt-root-mismatch'] };
  await validateInstallState(resolve(expectedRoot));
  const release = await acquireInstallLock(expectedRoot, receipt.pythonExe, receipt.packageDir);
  try {
    const currentReceiptText = await readOptional(join(receipt.root, ownDir, 'receipt.json'));
    if (currentReceiptText === null) return { status: 'conflict', conflicts: ['current-receipt-missing'] };
    let currentReceipt: InstallReceipt;
    try { currentReceipt = validateReceipt(JSON.parse(currentReceiptText)); }
    catch { return { status: 'conflict', conflicts: ['current-receipt-invalid'] }; }
    if (currentReceipt.transactionId !== receipt.transactionId) return { status: 'conflict', conflicts: ['stale-remove-receipt'] };
    const recovery = await recoverJournalUnlocked(receipt.journal, { lease: release });
    if (recovery.status === 'conflict') return { status: 'conflict', conflicts: recovery.conflicts };
    if (resolve(receipt.journal).toLocaleLowerCase() !== resolve(receipt.root, ownDir, 'journal.json').toLocaleLowerCase()
      || resolve(receipt.root) !== receipt.root) return { status: 'conflict', conflicts: ['receipt-location-invalid'] };
    await validateReceiptFilesystem(receipt);
    const journal: InstallJournal = { schema: 1, transactionId: randomUUID(), phase: 'applying', root: receipt.root,
      pythonExe: receipt.pythonExe, packageDir: receipt.packageDir, artifacts: receipt.artifacts, operations: structuredClone(receipt.operations.map((operation) => ({ ...operation, progress: 'done' as const }))),
      targetManifest: receipt.targetManifest, reversedTargets: [], updatedAt: new Date().toISOString() };
    await saveLockedJournal(receipt.journal, journal, release);
    const removal = await recoverJournalUnlocked(receipt.journal, { lease: release });
    const conflicts = removal.conflicts;
    if (!conflicts.length) await release.remove(join(receipt.root, ownDir, 'receipt.json'));
    return { status: conflicts.length ? 'conflict' : 'removed', conflicts };
  } finally { await release(); }
}
