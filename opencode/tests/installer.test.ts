import assert from 'node:assert/strict';
import { mkdtemp, readFile, readdir, rm, writeFile, mkdir, copyFile, symlink, stat } from 'node:fs/promises';
import { homedir, tmpdir } from 'node:os';
import { join, resolve } from 'node:path';
import { spawn, spawnSync } from 'node:child_process';
import { pathToFileURL } from 'node:url';
import test from 'node:test';
import { parseConfig } from '../install/config-edit.ts';
import { planInstall, applyInstall, inspectInstall, removeOwned, recoverInstall, portableMcpCommand, installInputFromResources, reverseTarget } from '../install/cli.ts';
import { acquireInstallLock, conditionalCommit, hashText } from '../install/ownership.ts';
import type { InstallReceipt } from '../install/ownership.ts';
import { createServerPlugin } from '../server.ts';

async function validInput(root: string, configDir: string, artifactDir = join(process.cwd(), 'opencode')) {
  const vault = join(root, 'synthetic-vault');
  await mkdir(vault, { recursive: true });
  await mkdir(artifactDir, { recursive: true });
  await copyFile(join(process.cwd(), 'opencode', 'server.ts'), join(artifactDir, 'server.ts'));
  await copyFile(join(process.cwd(), 'opencode', 'tui.tsx'), join(artifactDir, 'tui.tsx'));
  const pythonExe = join(process.env.VIRTUAL_ENV!, 'Scripts', 'python.exe');
  const packageDir = join(process.cwd(), 'src', 'symbiosis_brain');
  const skillsDir = join(process.cwd(), 'skills');
  return { configDir, artifactDir, pythonExe, vault, packageDir, skillsDir, mcpCommand: portableMcpCommand(pythonExe, vault) };
}

async function fixture<T>(fn: (root: string) => Promise<T>): Promise<T> {
  const root = await mkdtemp(join(tmpdir(), 'sb-install-'));
  try { return await fn(root); } finally { await rm(root, { recursive: true, force: true }); }
}

test('installation preserves JSONC host settings and is idempotent', async () => fixture(async (root) => {
  const configDir = join(root, 'config'); const artifactDir = join(root, 'artifact');
  await mkdir(configDir); await mkdir(artifactDir);
  const config = join(configDir, 'opencode.jsonc');
  const initial = `{// keep comment\n"$schema":"https://opencode.ai/config.json", "plugin":["orca", ["other", {"enabled":true}]], "mcp":{"brain":{"type":"local","command":["brain"]}}, "model":"vendor/model", "permission":{"edit":"ask"}, "skills":{"paths":["C:/shared/skills"]}, "command":{"commands":{"description":"palette"}}}`;
  await writeFile(config, initial);
  const tui = join(configDir, 'tui.json');
  const tuiInitial = `{// preserve TUI comment\n"plugin":["orca-tui", ["status", {"enabled":true}]]}`;
  await writeFile(tui, tuiInitial);
  const input = await validInput(root, configDir, artifactDir);
  const plan = await planInstall(input);
  await applyInstall(plan);
  const installed = await readFile(config, 'utf8');
  assert.match(installed, /keep comment/); assert.match(installed, /"orca"/); assert.match(installed, /"model"\s*:\s*"vendor\/model"/);
  const tuiInstalled = await readFile(tui, 'utf8');
  assert.match(tuiInstalled, /preserve TUI comment/); assert.match(tuiInstalled, /orca-tui/);
  await applyInstall(await planInstall(input));
  assert.equal(await readFile(config, 'utf8'), installed);
  assert.equal(await readFile(tui, 'utf8'), tuiInstalled);
  const doctor = await inspectInstall(configDir);
  assert.equal(doctor.status, 'installed');
  const ownedCommand = join(configDir, 'command', 'brain-save.md');
  const concurrentCommandEdit = `${await readFile(ownedCommand, 'utf8')}User-added line.\n`;
  await writeFile(ownedCommand, concurrentCommandEdit);
  await assert.rejects(() => planInstall(input), /owned-(?:file|command)-conflict/);
  assert.equal(await readFile(ownedCommand, 'utf8'), concurrentCommandEdit);
}));

test('actual worker resources reply feeds the installer portable MCP tuple', async () => fixture(async (root) => {
  const configDir = join(root, 'config'); const artifactDir = join(root, 'artifact');
  await mkdir(configDir); await mkdir(artifactDir);
  const direct = await validInput(root, configDir, artifactDir);
  const resourceProbe = 'import json,sys; from symbiosis_brain.adapters.opencode.protocol import ContextRequest; from symbiosis_brain.adapters.opencode.worker import _service_factory; r=ContextRequest("r","s",0,"resources",sys.argv[1],"resume",0,boundary=sys.argv[1]); print(json.dumps(_service_factory(sys.argv[2])(\'control\')(r).to_wire()))';
  const worker = spawnSync(direct.pythonExe, ['-c', resourceProbe, root, direct.vault], { cwd: process.cwd(), encoding: 'utf8', windowsHide: true, timeout: 15_000 });
  assert.equal(worker.status, 0, worker.stderr);
  const reply = JSON.parse(worker.stdout);
  assert.equal(reply.status, 'ok');
  const input = installInputFromResources({ configDir, artifactDir, vault: direct.vault, projectRoot: root }, reply.resources);
  assert.deepEqual(input.mcpCommand, portableMcpCommand(reply.resources.pythonExe, direct.vault));
  await applyInstall(await planInstall(input));
  const config = JSON.parse(await readFile(join(configDir, 'opencode.jsonc'), 'utf8'));
  assert.deepEqual(config.mcp['symbiosis-brain'].command, reply.resources.mcpCommand);
}));

test('installed flat plugin tuple starts the server runtime without inherited Brain environment', async () => fixture(async (root) => {
  const configDir = join(root, 'config'); const artifactDir = join(root, 'artifact');
  await mkdir(configDir); await mkdir(artifactDir);
  const input = await validInput(root, configDir, artifactDir);
  const receipt = await applyInstall(await planInstall(input));
  const config = parseConfig(await readFile(join(configDir, 'opencode.jsonc'), 'utf8'));
  const tuple = (config.plugin as unknown[][]).find((entry) => Array.isArray(entry) && (entry[1] as Record<string, unknown>)?.adapterId === 'symbiosis-brain-opencode-v1');
  assert.ok(tuple);
  const pluginOptions = tuple[1] as Record<string, unknown>;
  assert.equal(pluginOptions.python, input.pythonExe); assert.equal(pluginOptions.tools, input.packageDir); assert.equal(pluginOptions.vault, input.vault);
  assert.equal(typeof pluginOptions.shell, 'string');
  const inheritedBrainEnv = Object.fromEntries(Object.entries(process.env).filter(([key]) => key.startsWith('SYMBIOSIS_BRAIN_')));
  for (const key of Object.keys(inheritedBrainEnv)) delete process.env[key];
  let configuredRuntime: Record<string, unknown> | undefined;
  let hostConfigCalls = 0;
  const resources = { pythonExe: input.pythonExe, packageDir: input.packageDir, skillsDir: input.skillsDir, grepExe: null,
    actionRulesFile: join(input.vault, '.index', 'action-rules.tsv'), mcpCommand: input.mcpCommand };
  const plugin = createServerPlugin({
    workerPool(options) {
      configuredRuntime = options as unknown as Record<string, unknown>;
      return { async request(request) { return { version: 1, kind: 'result', requestId: request.requestId, sessionId: request.sessionId,
        generation: request.generation, capabilityRevision: request.capabilityRevision, status: 'ok', scope: null,
        blocks: [], diagnostics: [], ...(request.operation === 'resources' ? { resources } : {}) }; }, async dispose() {} };
    },
    actionMatcher: () => ({ async match() { return []; }, async matchDetailed() { return { status: 'ok', blocks: [] }; }, async dispose() {}, diagnostics: [] }),
  });
  try {
    const hooks = await plugin({ directory: root, worktree: root, client: { config: { async get() { hostConfigCalls++; throw new Error('unexpected config fallback'); } } } } as never, pluginOptions as never);
    assert.equal(hostConfigCalls, 0); assert.ok(configuredRuntime);
    assert.equal(configuredRuntime.pythonExe, input.pythonExe); assert.equal(configuredRuntime.packageDir, input.packageDir); assert.equal(configuredRuntime.vault, input.vault);
    await hooks.dispose?.();
  } finally { Object.assign(process.env, inheritedBrainEnv); }
  assert.ok(receipt.transactionId);
}));

test('uninstall reports conflict and preserves concurrent edits', async () => fixture(async (root) => {
  const configDir = join(root, 'config'); const artifactDir = join(root, 'artifact');
  await mkdir(configDir); await mkdir(artifactDir);
  const config = join(configDir, 'opencode.json');
  await writeFile(config, '{"model":"vendor/model","plugin":[]}');
  const input = await validInput(root, configDir, artifactDir);
  const receipt = await applyInstall(await planInstall(input));
  const parsed = JSON.parse(await readFile(config, 'utf8'));
  parsed.plugin[0][1].enabled = false;
  parsed.model = 'user/changed';
  const current = JSON.stringify(parsed, null, 2);
  await writeFile(config, current);
  const result = await removeOwned(receipt, configDir);
  assert.equal(result.status, 'conflict'); assert.equal(await readFile(config, 'utf8'), current);
}));

test('reverse ownership uses the first observed bytes across an ABA-style interleaving', () => fixture(async (root) => {
  const target = join(root, 'owned.txt');
  const userBytes = 'intervening user edit';
  await writeFile(target, 'owned bytes');
  const operation = { target, kind: 'file' as const, before: null, after: 'owned bytes', progress: 'done' as const };
  const decision = await reverseTarget([operation], userBytes);
  assert.equal(decision.status, 'conflict');
  await writeFile(target, userBytes);
  assert.equal(await readFile(target, 'utf8'), userBytes);
}));

test('reverse no-op still validates the exact absent snapshot before confirming ownership reversed', () => fixture(async (root) => {
  const target = join(root, 'owned.txt');
  await writeFile(target, 'owned bytes');
  const operation = { target, kind: 'file' as const, before: null, after: 'owned bytes', progress: 'done' as const };
  const decision = await reverseTarget([operation], null);
  assert.equal(decision.status, 'ok');
  assert.equal(decision.content, null);
  const pythonExe = join(process.env.VIRTUAL_ENV!, 'Scripts', 'python.exe');
  assert.throws(() => conditionalCommit(target, decision.content, hashText(null), pythonExe), /conditional-commit-conflict/);
  assert.equal(await readFile(target, 'utf8'), 'owned bytes');
}));

test('installer refuses to replace an unowned command with the same name', async () => fixture(async (root) => {
  const configDir = join(root, 'config'); const artifactDir = join(root, 'artifact');
  await mkdir(configDir); await mkdir(artifactDir); await mkdir(join(configDir, 'command'));
  const command = join(configDir, 'command', 'brain-save.md');
  const userCommand = '---\ndescription: user-owned\n---\nKeep my instructions.\n';
  await writeFile(command, userCommand);
  const input = await validInput(root, configDir, artifactDir);
  await assert.rejects(() => planInstall(input), /owned-command-conflict/);
  assert.equal(await readFile(command, 'utf8'), userCommand);
}));

test('a correct existing Brain MCP registration is preserved rather than duplicated', async () => fixture(async (root) => {
  const configDir = join(root, 'config'); const artifactDir = join(root, 'artifact');
  await mkdir(configDir); await mkdir(artifactDir);
  const input = await validInput(root, configDir, artifactDir);
  const existing = { type: 'local', enabled: true, command: input.mcpCommand };
  const configPath = join(configDir, 'opencode.json');
  await writeFile(configPath, JSON.stringify({ mcp: { brain: existing }, model: 'vendor/model' }));
  const receipt = await applyInstall(await planInstall(input));
  const installed = parseConfig(await readFile(configPath, 'utf8'));
  assert.deepEqual(installed.mcp, { brain: existing });
  assert.equal(receipt.operations.some((operation) => operation.path?.[0] === 'mcp'), false);
}));

test('an existing Brain MCP registration bound to a different vault is a conflict', async () => fixture(async (root) => {
  const configDir = join(root, 'config'); const artifactDir = join(root, 'artifact');
  await mkdir(configDir); await mkdir(artifactDir);
  const input = await validInput(root, configDir, artifactDir);
  const wrongVault = join(root, 'other-vault'); await mkdir(wrongVault);
  const configPath = join(configDir, 'opencode.json');
  await writeFile(configPath, JSON.stringify({ mcp: { brain: { type: 'local', command: portableMcpCommand(input.pythonExe, wrongVault) } } }));
  const before = await readFile(configPath, 'utf8');
  await assert.rejects(() => planInstall(input), /brain-mcp-existing-binding-conflict/);
  assert.equal(await readFile(configPath, 'utf8'), before);
}));

test('adapter lock has one winner, refuses unpublished owners, and releases only its token', async () => fixture(async (root) => {
  const pythonExe = join(process.env.VIRTUAL_ENV!, 'Scripts', 'python.exe');
  const packageDir = join(process.cwd(), 'src', 'symbiosis_brain');
  const results = await Promise.allSettled([acquireInstallLock(root, pythonExe, packageDir), acquireInstallLock(root, pythonExe, packageDir)]);
  const winners = results.filter((result) => result.status === 'fulfilled');
  const losers = results.filter((result) => result.status === 'rejected');
  assert.equal(winners.length, 1); assert.equal(losers.length, 1);
  const release = (winners[0] as PromiseFulfilledResult<() => Promise<void>>).value;
  await release();
}));

test('a live stale Node owner cannot mutate after its exact OS lock holder dies and a new owner enters', () => fixture(async (root) => {
  const pythonExe = join(process.env.VIRTUAL_ENV!, 'Scripts', 'python.exe');
  const moduleUrl = pathToFileURL(join(process.cwd(), 'opencode/install/ownership.ts')).href;
  const target = join(root, '.symbiosis-brain-install', 'journal.json');
  const gate = `${target}.gate`;
  const script = `const m=await import(${JSON.stringify(moduleUrl)}); const lock=await m.acquireInstallLock(process.env.SB_LOCK_ROOT,process.env.SB_PYTHON,process.env.SB_PACKAGE); process.stdout.write(JSON.stringify({held:true,pid:lock.holderPid})+'\\n'); process.stdin.once('data',async()=>{process.stdout.write('WRITE_SENT\\n');try{await lock.write(process.env.SB_TARGET,'stale-owner',process.env.SB_TARGET+'.gate');process.stdout.write('UNEXPECTED_WRITE\\n')}catch(e){process.stdout.write('REJECTED:'+e.message+'\\n')} process.exit(0)});`;
  const holder = spawn(process.execPath, ['--experimental-strip-types', '--input-type=module', '-e', script], {
    cwd: process.cwd(), env: { ...process.env, SB_LOCK_ROOT: root, SB_TARGET: target, SB_PYTHON: pythonExe, SB_PACKAGE: join(process.cwd(), 'src', 'symbiosis_brain'), SB_TEST_INSTALL_LOCK_WRITE_GATE: gate }, stdio: ['pipe', 'pipe', 'pipe'], windowsHide: true,
  });
  let output = ''; let holderError = ''; let holderPid = 0;
  holder.stdout.setEncoding('utf8').on('data', (chunk: string) => {
    output += chunk;
    if (!holderPid) {
      try { holderPid = (JSON.parse(output.split(/\r?\n/)[0] ?? '') as { pid?: number }).pid ?? 0; } catch { /* incomplete frame */ }
    }
  });
  holder.stderr.setEncoding('utf8').on('data', (chunk: string) => { holderError += chunk; });
  const readUntil = async (predicate: () => boolean) => {
    const deadline = Date.now() + 10_000;
    while (!predicate() && Date.now() < deadline) await new Promise((resolveWait) => setTimeout(resolveWait, 10));
    assert.ok(predicate(), `lock-holder-ready-timeout:${output}:${holderError}`);
  };
  try {
    await readUntil(() => holderPid > 0);
    holder.stdin.write('start-write\n');
    await readUntil(() => output.includes('WRITE_SENT'));
    const gateEntered = gate;
    const deadline = Date.now() + 10_000;
    while (!await readFile(gateEntered, 'utf8').then(() => true).catch(() => false) && Date.now() < deadline) await new Promise((resolveWait) => setTimeout(resolveWait, 10));
    assert.ok(await readFile(gateEntered, 'utf8').catch(() => ''), `holder RPC must pause before mutation: ${output}:${holderError}`);
    assert.ok(!output.includes('UNEXPECTED_WRITE'), `test write must still be in flight before killing its holder: ${output}`);
    process.kill(holderPid);
    const nextOwner = await acquireInstallLock(root, pythonExe, join(process.cwd(), 'src', 'symbiosis_brain'));
    try {
      await nextOwner.write(target, 'new-owner');
      await readUntil(() => output.includes('REJECTED:installation-lock-lost'));
      assert.match(output, /REJECTED:installation-lock-lost/);
      assert.equal(await readFile(target, 'utf8'), 'new-owner');
    } finally { await nextOwner(); }
  } finally {
    if (holder.exitCode === null) { holder.kill(); await new Promise<void>((resolveExit) => holder.once('exit', () => resolveExit())); }
  }
}));

test('lock release waits for helper exit even when the release acknowledgement is emitted first', () => fixture(async (root) => {
  const pythonExe = join(process.env.VIRTUAL_ENV!, 'Scripts', 'python.exe');
  const gate = join(root, 'release-gate');
  process.env.SB_TEST_INSTALL_LOCK_RELEASE_GATE = gate;
  const release = await acquireInstallLock(root, pythonExe, join(process.cwd(), 'src', 'symbiosis_brain'));
  delete process.env.SB_TEST_INSTALL_LOCK_RELEASE_GATE;
  const releasing = release();
  const entered = join(root, 'release-gate.entered');
  const deadline = Date.now() + 10_000;
  while (!await readFile(entered, 'utf8').then(() => true).catch(() => false) && Date.now() < deadline) await new Promise((resolveWait) => setTimeout(resolveWait, 10));
  assert.ok(await readFile(entered, 'utf8').catch(() => ''), 'helper must acknowledge release before exit');
  await writeFile(join(root, 'release-gate.proceed'), 'continue');
  await releasing;
}));

test('identical reinstall preserves the original TUI baseline for uninstall', async () => fixture(async (root) => {
  const configDir = join(root, 'config'); const artifactDir = join(root, 'artifact');
  await mkdir(configDir); await mkdir(artifactDir);
  const tui = join(configDir, 'tui.json');
  const original = '{"plugin":["user-tui"]}\n';
  await writeFile(tui, original);
  const input = await validInput(root, configDir, artifactDir);
  await applyInstall(await planInstall(input));
  const latest = await applyInstall(await planInstall(input));
  const result = await removeOwned(latest, configDir);
  assert.equal(result.status, 'removed', result.conflicts.join(','));
  assert.equal(await readFile(tui, 'utf8'), original);
}));

for (const upgraded of [false, true]) test(`uninstall preserves a user comment added inside the owned TUI tuple${upgraded ? ' after artifact upgrade' : ''}`, async () => fixture(async (root) => {
  const configDir = join(root, 'config'); const artifactDir = join(root, 'artifact');
  await mkdir(configDir); await mkdir(artifactDir);
  const tui = join(configDir, 'tui.json');
  await writeFile(tui, '{"plugin":["user-tui"]}\n');
  let input = await validInput(root, configDir, artifactDir);
  let receipt = await applyInstall(await planInstall(input));
  if (upgraded) {
    input = await validInput(root, configDir, join(root, 'artifact-upgraded'));
    receipt = await applyInstall(await planInstall(input));
  }
  const installed = await readFile(tui, 'utf8');
  const moduleSpec = JSON.stringify(join(input.artifactDir, 'tui.tsx'));
  const tupleStart = installed.indexOf(moduleSpec);
  assert.notEqual(tupleStart, -1);
  const optionsStart = installed.indexOf('{', tupleStart + moduleSpec.length);
  assert.notEqual(optionsStart, -1);
  const edited = `${installed.slice(0, optionsStart + 1)}/* user TUI note */${installed.slice(optionsStart + 1)}`;
  await writeFile(tui, edited);
  const result = await removeOwned(receipt, configDir);
  assert.equal(result.status, 'conflict');
  assert.equal(await readFile(tui, 'utf8'), edited);
}));

test('removing with an obsolete receipt cannot remove the current transaction', async () => fixture(async (root) => {
  const configDir = join(root, 'config'); const artifactDir = join(root, 'artifact');
  await mkdir(configDir); await mkdir(artifactDir);
  const input = await validInput(root, configDir, artifactDir);
  const oldReceipt = await applyInstall(await planInstall(input));
  const currentReceipt = await applyInstall(await planInstall(input));
  const before = await readFile(join(configDir, 'opencode.jsonc'), 'utf8');
  const result = await removeOwned(oldReceipt, configDir);
  assert.deepEqual(result, { status: 'conflict', conflicts: ['stale-remove-receipt'] });
  assert.equal(await readFile(join(configDir, 'opencode.jsonc'), 'utf8'), before);
  assert.equal(JSON.parse(await readFile(join(configDir, '.symbiosis-brain-install', 'receipt.json'), 'utf8')).transactionId, currentReceipt.transactionId);
}));

test('a copied receipt cannot redirect removal to its recorded root', async () => fixture(async (root) => {
  const configA = join(root, 'config-a'); const configB = join(root, 'config-b'); const artifactDir = join(root, 'artifact');
  await mkdir(configA); await mkdir(configB); await mkdir(artifactDir);
  const target = join(configA, 'sentinel'); await writeFile(target, 'synthetic protected bytes');
  const pythonExe = join(process.env.VIRTUAL_ENV!, 'Scripts', 'python.exe');
  const receipt: InstallReceipt = { schema: 1, transactionId: 'synthetic-transaction', root: configA, pythonExe, packageDir: join(process.cwd(), 'src', 'symbiosis_brain'),
    artifacts: { serverPath: join(artifactDir, 'server.ts'), serverSha256: 'a'.repeat(64), tuiPath: join(artifactDir, 'tui.tsx'), tuiSha256: 'b'.repeat(64) },
    journal: join(configA, '.symbiosis-brain-install', 'journal.json'),
    operations: [{ target, kind: 'file', before: 'original bytes', after: 'synthetic protected bytes', progress: 'done' }],
    targetManifest: [target], installedAt: 'synthetic' };
  const result = await removeOwned(receipt, configB);
  assert.deepEqual(result, { status: 'conflict', conflicts: ['receipt-root-mismatch'] });
  assert.equal(await readFile(target, 'utf8'), 'synthetic protected bytes');
}));

test('doctor rejects a copied receipt root before following any recorded target', async () => fixture(async (root) => {
  const configA = join(root, 'config-a'); const configB = join(root, 'config-b'); const artifactDir = join(root, 'artifact');
  await mkdir(configA); await mkdir(configB); await mkdir(artifactDir);
  const input = await validInput(root, configA, artifactDir);
  const receipt = await applyInstall(await planInstall(input));
  const stateB = join(configB, '.symbiosis-brain-install'); await mkdir(stateB);
  const redirected = join(root, 'redirected'); await mkdir(redirected);
  const junction = join(configB, 'redirect'); await symlink(redirected, junction, 'junction');
  const copied = structuredClone(receipt);
  copied.operations[0]!.target = join(junction, 'trap.json');
  copied.targetManifest = [...new Set(copied.operations.map((operation) => resolve(operation.target)))];
  await writeFile(join(stateB, 'receipt.json'), JSON.stringify(copied));
  const doctor = await inspectInstall(configB);
  assert.equal(doctor.status, 'conflict');
  assert.deepEqual(doctor.conflicts, ['receipt-root-mismatch']);
  assert.deepEqual(await readdir(redirected), []);
}));

test('apply rejects resource paths changed after planning before creating the journal', async () => fixture(async (root) => {
  const configDir = join(root, 'config'); const artifactDir = join(root, 'artifact');
  await mkdir(configDir); await mkdir(artifactDir);
  const input = await validInput(root, configDir, artifactDir);
  const plan = await planInstall(input);
  const wrongPackage = join(root, 'wrong-package'); await mkdir(wrongPackage);
  plan.resourceInput.packageDir = wrongPackage;
  await assert.rejects(() => applyInstall(plan), /python-package-resources-mismatch:package/);
  assert.equal(await readFile(join(configDir, '.symbiosis-brain-install', 'journal.json'), 'utf8').catch(() => null), null);
}));

test('installer state directory reparse point is rejected before planning reads state', async () => fixture(async (root) => {
  const configDir = join(root, 'config'); const artifactDir = join(root, 'artifact'); const redirected = join(root, 'redirected-state');
  await mkdir(configDir); await mkdir(artifactDir); await mkdir(redirected);
  const input = await validInput(root, configDir, artifactDir);
  await symlink(redirected, join(configDir, '.symbiosis-brain-install'), 'junction');
  await assert.rejects(() => planInstall(input), /installer-path-reparse/);
  assert.deepEqual(await readdir(redirected), []);
}));

test('uninstall does not delete a newly created config after a user adds a comment', async () => fixture(async (root) => {
  const configDir = join(root, 'config'); const artifactDir = join(root, 'artifact');
  await mkdir(configDir); await mkdir(artifactDir);
  const input = await validInput(root, configDir, artifactDir);
  const receipt = await applyInstall(await planInstall(input));
  const config = join(configDir, 'opencode.jsonc');
  const withComment = (await readFile(config, 'utf8')).replace('{', '{/* user note */');
  await writeFile(config, withComment);
  const result = await removeOwned(receipt, configDir);
  assert.equal(result.status, 'conflict');
  assert.match(await readFile(config, 'utf8'), /user note/);
}));

test('reinstall updates an exactly owned Brain MCP command when its vault changes', async () => fixture(async (root) => {
  const configDir = join(root, 'config'); const artifactDir = join(root, 'artifact');
  await mkdir(configDir); await mkdir(artifactDir);
  const input = await validInput(root, configDir, artifactDir);
  await applyInstall(await planInstall(input));
  const nextVault = join(root, 'next-vault'); await mkdir(nextVault);
  const upgradedInput = { ...input, vault: nextVault, mcpCommand: portableMcpCommand(input.pythonExe, nextVault) };
  const plan = await planInstall(upgradedInput);
  assert.ok(plan.operations.some((operation) => operation.ownershipId === 'symbiosis-brain-opencode-v1:mcp'));
  await applyInstall(plan);
  const config = parseConfig(await readFile(join(configDir, 'opencode.jsonc'), 'utf8'));
  assert.deepEqual((config.mcp as Record<string, { command: string[] }>)['symbiosis-brain']?.command, upgradedInput.mcpCommand);
}));

test('artifact upgrade replaces owned server and TUI modules without duplicate skills paths', async () => fixture(async (root) => {
  const configDir = join(root, 'config'); await mkdir(configDir);
  const config = join(configDir, 'opencode.json'); await writeFile(config, '{"model":"vendor/model"}');
  const priorPythonPath = process.env.PYTHONPATH;
  const wheelDir = join(root, 'wheel-dist'); await mkdir(wheelDir);
  let wheelPath = process.env.SB_TEST_WHEEL_FILE;
  const uv = process.env.SB_TEST_UV_EXE ?? 'uv';
  if (!wheelPath) {
    const build = spawnSync(uv, ['build', '--wheel', '--out-dir', wheelDir], { cwd: process.cwd(), encoding: 'utf8', timeout: 60_000 });
    assert.equal(build.status, 0, build.stderr);
    const wheelName = (await readdir(wheelDir)).find((name) => name.endsWith('.whl'));
    assert.ok(wheelName, 'synthetic acceptance wheel exists');
    wheelPath = join(wheelDir, wheelName);
  }
  assert.ok((await stat(wheelPath)).isFile(), `prepared acceptance wheel exists: ${wheelPath}`);
  const wheelVenv = join(root, 'wheel-venv');
  const sourcePython = join(process.env.VIRTUAL_ENV!, 'Scripts', 'python.exe');
  const makeVenv = spawnSync(uv, ['--offline', 'venv', wheelVenv, '--python', sourcePython], { cwd: process.cwd(), encoding: 'utf8', timeout: 30_000 });
  assert.equal(makeVenv.status, 0, makeVenv.stderr);
  const v2Python = join(wheelVenv, 'Scripts', 'python.exe');
  const installWheel = spawnSync(uv, ['--offline', 'pip', 'install', '--python', v2Python, '--no-deps', wheelPath], { cwd: process.cwd(), encoding: 'utf8', timeout: 30_000 });
  assert.equal(installWheel.status, 0, installWheel.stderr);
  const v2Package = join(wheelVenv, 'Lib', 'site-packages', 'symbiosis_brain');
  const v2Skills = join(v2Package, 'skills');
  try {
  const v1 = await validInput(root, configDir, join(root, 'artifact-v1'));
  Object.assign(v1, { packageDir: join(process.cwd(), 'src', 'symbiosis_brain'), skillsDir: join(process.cwd(), 'skills') });
  v1.mcpCommand = portableMcpCommand(v1.pythonExe, v1.vault);
  await applyInstall(await planInstall(v1));
  const v2 = await validInput(root, configDir, join(root, 'artifact-v2'));
  Object.assign(v2, { pythonExe: v2Python, packageDir: v2Package, skillsDir: v2Skills });
  v2.mcpCommand = portableMcpCommand(v2.pythonExe, v2.vault);
  delete process.env.PYTHONPATH;
  const receipt = await applyInstall(await planInstall(v2));
  const currentConfig = JSON.parse(await readFile(config, 'utf8'));
  const serverEntries = currentConfig.plugin.filter((entry: unknown) => Array.isArray(entry) && (entry[1] as Record<string, unknown>)?.adapterId === 'symbiosis-brain-opencode-v1');
  assert.equal(serverEntries.length, 1);
  assert.equal(serverEntries[0][0], join(v2.artifactDir, 'server.ts'));
  const tuiConfig = JSON.parse(await readFile(join(configDir, 'tui.json'), 'utf8'));
  const tuiEntries = tuiConfig.plugin.filter((entry: unknown) => Array.isArray(entry) && (entry[1] as Record<string, unknown>)?.adapterId === 'symbiosis-brain-opencode-v1');
  assert.equal(tuiEntries.length, 1);
  assert.equal(tuiEntries[0][0], join(v2.artifactDir, 'tui.tsx'));
  assert.equal(currentConfig.skills.paths.filter((path: string) => path === v2Skills).length, 1);
  assert.equal(currentConfig.skills.paths.filter((path: string) => path === v1.skillsDir).length, 0);
  const doctor = await inspectInstall(configDir);
  assert.equal(doctor.status, 'installed', JSON.stringify(doctor));
  assert.ok(receipt.operations.some((operation) => operation.afterIdentity === join(v2.artifactDir, 'server.ts')));
  assert.ok(receipt.operations.some((operation) => operation.afterIdentity === v2Skills));
  const packagedConfigDir = join(root, 'wheel-config'); await mkdir(packagedConfigDir);
  const packagedProbe = `from symbiosis_brain.adapters.opencode.install import main; import sys,json; c=sys.argv[1]; v=sys.argv[2]; n=sys.argv[3]; results=[main(['setup','--vault',v,'--config-dir',c,'--node',n]),main(['doctor','--config-dir',c,'--node',n]),main(['uninstall','--config-dir',c,'--node',n])]; print(json.dumps(results)); sys.exit(any(results))`;
  const cleanEnv = { ...process.env, PYTHONPATH: '' };
  const packaged = spawnSync(v2Python, ['-c', packagedProbe, packagedConfigDir, v2.vault, process.execPath], {
    cwd: root, env: cleanEnv, encoding: 'utf8', timeout: 90_000, windowsHide: true,
  });
  if (packaged.status !== 0) {
    const diagnosticProbe = `import json,sys,subprocess; from pathlib import Path; from importlib.resources import files; from symbiosis_brain.adapters.opencode.install import _resources; a,s=_resources(); p=Path(str(files('symbiosis_brain'))).resolve(); v=sys.argv[1]; py=sys.executable; i={'configDir':sys.argv[2],'artifactDir':str(a),'pythonExe':py,'vault':v,'packageDir':str(p),'skillsDir':str(s),'mcpCommand':[py,'-c','from symbiosis_brain.install_cli import main; main()','serve','--vault',v]}; r=subprocess.run([sys.argv[3],str(a/'install/main.mjs')],input=json.dumps({'command':'install','input':i}),text=True,capture_output=True); mod=(a/'install/cli.mjs').as_uri(); code='const m=await import('+json.dumps(mod)+'); console.log(JSON.stringify(await m.inspectInstall('+json.dumps(sys.argv[2])+')))'; d=subprocess.run([sys.argv[3],'--input-type=module','-e',code],text=True,capture_output=True); print(json.dumps({'stdout':r.stdout,'stderr':r.stderr,'code':r.returncode,'doctor':d.stdout,'doctorError':d.stderr}))`;
    const diagnostic = spawnSync(v2Python, ['-c', diagnosticProbe, v2.vault, packagedConfigDir, process.execPath], {
      cwd: root, env: cleanEnv, encoding: 'utf8', timeout: 30_000, windowsHide: true,
    });
    assert.fail(`packaged Python CLI failed\nstdout:\n${packaged.stdout}\nstderr:\n${packaged.stderr}\ndirect Node: ${diagnostic.stdout}\n${diagnostic.stderr}`);
  }
  assert.deepEqual(JSON.parse(packaged.stdout.split(/\r?\n/).at(-2)!), [0, 0, 0]);
  const packageOnlyProbe = 'from importlib.resources import files; from symbiosis_brain.adapters.opencode.install import _resources,_verify_manifest; import pathlib; p=pathlib.Path(str(files("symbiosis_brain"))).resolve(); a,s=_resources(); assert a==p/"opencode"; assert s==p/"skills"; _verify_manifest(a); print(p)';
  const packageOnly = spawnSync(v2Python, ['-c', packageOnlyProbe], { cwd: root, env: cleanEnv, encoding: 'utf8', timeout: 15_000, windowsHide: true });
  assert.equal(packageOnly.status, 0, `${packageOnly.stdout}\n${packageOnly.stderr}`);
  assert.match(packageOnly.stdout.trim(), /[\\/]Lib[\\/]site-packages[\\/]symbiosis_brain$/i);
  assert.doesNotMatch(packageOnly.stdout, /opencode-v071[\\/]src[\\/]symbiosis_brain/i);
  } finally {
    if (priorPythonPath === undefined) delete process.env.PYTHONPATH;
    else process.env.PYTHONPATH = priorPythonPath;
  }
}));

test('doctor reports canonical skill duplicates when host precedence is not known', async () => fixture(async (root) => {
  const configDir = join(root, 'config'); const artifactDir = join(root, 'artifact');
  await mkdir(configDir); await mkdir(artifactDir);
  const input = await validInput(root, configDir, artifactDir);
  await applyInstall(await planInstall(input));
  const duplicate = join(homedir(), '.claude', 'skills', 'brain-init');
  try {
    await mkdir(duplicate, { recursive: true });
    await writeFile(join(duplicate, 'SKILL.md'), '# Synthetic external duplicate\n');
    const doctor = await inspectInstall(configDir);
    assert.equal(doctor.status, 'conflict');
    assert.ok(doctor.diagnostics.includes('skill-precedence-unknown:brain-init'));
  } finally { await rm(duplicate, { recursive: true, force: true }); }
}));

test('uninstall removes only owned entries and leaves the vault and host settings intact', async () => fixture(async (root) => {
  const configDir = join(root, 'config'); const artifactDir = join(root, 'artifact');
  await mkdir(configDir); await mkdir(artifactDir);
  const config = join(configDir, 'opencode.json');
  await writeFile(config, '{"plugin":["orca", /* preserve user plugin comment */ "other-plugin"],"model":"vendor/model","mcp":{"brain":{"command":["brain"]}}}');
  const input = await validInput(root, configDir, artifactDir);
  await writeFile(join(input.vault, 'sentinel'), 'synthetic vault content');
  const receipt = await applyInstall(await planInstall(input));
  const withAdapter = await readFile(config, 'utf8');
  const userEntryRange = withAdapter.match(/"orca"([\s\S]*?\/\* preserve user plugin comment \*\/[\s\S]*?)"other-plugin"/);
  assert.ok(userEntryRange, 'fixture entries and their in-array comment are visible before reorder');
  await writeFile(config, withAdapter.replace(userEntryRange[0], `"other-plugin"${userEntryRange[1]}"orca"`));
  const result = await removeOwned(receipt, configDir);
  assert.equal(result.status, 'removed', result.conflicts.join(','));
  const restored = parseConfig(await readFile(config, 'utf8'));
  assert.deepEqual(restored.plugin, ['other-plugin', 'orca']); assert.equal(restored.model, 'vendor/model');
  assert.match(await readFile(config, 'utf8'), /preserve user plugin comment/);
  assert.deepEqual(restored.mcp, { brain: { command: ['brain'] } });
  assert.equal(await readFile(join(input.vault, 'sentinel'), 'utf8'), 'synthetic vault content');
}));

test('interrupted transaction recovery preserves unrelated concurrent changes', async () => fixture(async (root) => {
  const configDir = join(root, 'config'); const artifactDir = join(root, 'artifact');
  await mkdir(configDir); await mkdir(artifactDir);
  const config = join(configDir, 'opencode.jsonc');
  await writeFile(config, '{"model":"vendor/model"}');
  const input = await validInput(root, configDir, artifactDir);
  const plan = await planInstall(input);
  await assert.rejects(() => applyInstall(plan, { failAfter: 1 }));
  const interrupted = JSON.parse(await readFile(config, 'utf8'));
  interrupted.model = 'user/changed'; await writeFile(config, JSON.stringify(interrupted));
  const result = await recoverInstall(join(configDir, '.symbiosis-brain-install', 'journal.json'));
  assert.equal(result.status, 'recovered');
  assert.equal(JSON.parse(await readFile(config, 'utf8')).model, 'user/changed');
}));

test('apply lock recovers an earlier failed plan and rejects plans made against a replaced receipt', async () => fixture(async (root) => {
  const configDir = join(root, 'config'); const artifactDir = join(root, 'artifact');
  await mkdir(configDir); await mkdir(artifactDir);
  const config = join(configDir, 'opencode.json'); await writeFile(config, '{"model":"vendor/model"}');
  const input = await validInput(root, configDir, artifactDir);
  const planA = await planInstall(input);
  const stalePlanB = await planInstall(input);
  await assert.rejects(() => applyInstall(planA, { failAfter: 2 }), /injected-install-failure/);
  const receiptB = await applyInstall(stalePlanB);
  assert.equal((await inspectInstall(configDir)).status, 'installed');
  const stalePlanC = await planInstall(input);
  const upgradedInput = await validInput(root, configDir, join(root, 'artifact-next'));
  await applyInstall(await planInstall(upgradedInput));
  await assert.rejects(() => applyInstall(stalePlanC), /stale-install-plan-receipt-changed/);
  assert.equal((await inspectInstall(configDir)).status, 'installed');
  assert.ok(receiptB.transactionId);
}));

async function runWriterCrashMatrix(root: string, crashMode: 'during' | 'after'): Promise<void> {
  const countRoot = join(root, 'count'); const countConfig = join(countRoot, 'config');
  await mkdir(countConfig, { recursive: true });
  await writeFile(join(countConfig, 'opencode.json'), JSON.stringify({
    plugin: ['orca'], model: 'vendor/model', mcp: { brain: { type: 'local', command: ['brain'] } },
    skills: { paths: ['C:/shared/skills'] },
  }));
  await writeFile(join(countConfig, 'tui.json'), '{"plugin":["orca-tui"]}');
  const countProject = join(countRoot, 'project');
  const legacyPath = join(countProject, '.opencode', 'plugins', 'brain-recall.ts');
  await mkdir(join(countProject, '.opencode', 'plugins'), { recursive: true });
  await writeFile(legacyPath, '/* synthetic legacy adapter */\nexport default {}\n');
  const countInput = { ...(await validInput(countRoot, countConfig, join(countRoot, 'artifact'))), projectRoot: countProject };
  const operationCount = (await planInstall(countInput)).operations.length;
  assert.ok(operationCount > 0);

  for (let index = 1; index <= operationCount; index++) {
    const runRoot = join(root, `${crashMode}-${index}`); const configDir = join(runRoot, 'config');
    await mkdir(configDir, { recursive: true });
    await writeFile(join(configDir, 'opencode.json'), JSON.stringify({
      plugin: ['orca'], model: 'vendor/model', mcp: { brain: { type: 'local', command: ['brain'] } },
      skills: { paths: ['C:/shared/skills'] },
    }));
    await writeFile(join(configDir, 'tui.json'), '{"plugin":["orca-tui"]}');
    const project = join(runRoot, 'project'); const legacy = join(project, '.opencode', 'plugins', 'brain-recall.ts');
    await mkdir(join(project, '.opencode', 'plugins'), { recursive: true });
    await writeFile(legacy, '/* synthetic legacy adapter */\nexport default {}\n');
    const input = { ...(await validInput(runRoot, configDir, join(runRoot, 'artifact'))), projectRoot: project };
    const option = crashMode === 'during' ? 'crashDuringWrite' : 'crashAfterWrite';
    const script = `const m=await import(${JSON.stringify(pathToFileURL(join(process.cwd(), 'opencode/install/cli.ts')).href)}); const i=JSON.parse(process.env.SB_INSTALL_INPUT); const p=await m.planInstall(i); await m.applyInstall(p,{${option}:Number(process.env.SB_CRASH_AT)});`;
    const child = spawnSync(process.execPath, ['--experimental-strip-types', '--input-type=module', '-e', script], {
      cwd: process.cwd(), env: { ...process.env, SB_INSTALL_INPUT: JSON.stringify(input), SB_CRASH_AT: String(index) }, encoding: 'utf8',
    });
    assert.notEqual(child.status, 0, `writer should die ${crashMode} target ${index}: ${child.stderr}`);
    const configPath = join(configDir, 'opencode.json');
    const changed = parseConfig(await readFile(configPath, 'utf8'));
    changed.model = `interleaved-user-${crashMode}-${index}`;
    await writeFile(configPath, JSON.stringify(changed));
    const journalPath = join(configDir, '.symbiosis-brain-install', 'journal.json');
    const recoverScript = `const m=await import(${JSON.stringify(pathToFileURL(join(process.cwd(), 'opencode/install/cli.ts')).href)}); const r=await m.recoverInstall(process.env.SB_JOURNAL); process.stdout.write(JSON.stringify(r));`;
    const recovery = spawnSync(process.execPath, ['--experimental-strip-types', '--input-type=module', '-e', recoverScript], {
      cwd: process.cwd(), env: { ...process.env, SB_JOURNAL: journalPath }, encoding: 'utf8',
    });
    assert.equal(recovery.status, 0, recovery.stderr);
    assert.equal(JSON.parse(recovery.stdout).status, 'recovered', recovery.stdout);
    assert.equal(parseConfig(await readFile(configPath, 'utf8')).model, `interleaved-user-${crashMode}-${index}`);
    assert.equal(await readFile(legacy, 'utf8'), '/* synthetic legacy adapter */\nexport default {}\n');
  }
}

test('new recovery process preserves interleaved edits after SIGKILL in writing state for every target', () => fixture((root) => runWriterCrashMatrix(root, 'during')));
test('new recovery process preserves interleaved edits after each committed target and legacy migration', () => fixture((root) => runWriterCrashMatrix(root, 'after')));

test('recovery journals reversed targets and can restart after a crash during rollback', async () => fixture(async (root) => {
  const configDir = join(root, 'config'); const artifactDir = join(root, 'artifact');
  await mkdir(configDir); await mkdir(artifactDir);
  const config = join(configDir, 'opencode.json');
  await writeFile(config, '{"model":"vendor/model"}');
  const input = await validInput(root, configDir, artifactDir);
  const plan = await planInstall(input);
  await assert.rejects(() => applyInstall(plan, { failAfter: plan.operations.length }), /injected-install-failure/);
  const installed = await readFile(config, 'utf8');
  const changed = JSON.parse(installed); changed.plugin[0][1].enabled = false; await writeFile(config, JSON.stringify(changed));
  const journalPath = join(configDir, '.symbiosis-brain-install', 'journal.json');
  const recoverScript = `const m=await import(${JSON.stringify(pathToFileURL(join(process.cwd(), 'opencode/install/cli.ts')).href)}); const r=await m.recoverInstall(process.env.SB_JOURNAL,JSON.parse(process.env.SB_RECOVER_OPTIONS||"{}")); process.stdout.write(JSON.stringify(r));`;
  const first = spawnSync(process.execPath, ['--experimental-strip-types', '--input-type=module', '-e', recoverScript], {
    cwd: process.cwd(), env: { ...process.env, SB_JOURNAL: journalPath }, encoding: 'utf8',
  });
  assert.equal(first.status, 0, first.stderr); assert.equal(JSON.parse(first.stdout).status, 'conflict');
  await writeFile(config, installed);
  const crash = spawnSync(process.execPath, ['--experimental-strip-types', '--input-type=module', '-e', recoverScript], {
    cwd: process.cwd(), env: { ...process.env, SB_JOURNAL: journalPath, SB_RECOVER_OPTIONS: JSON.stringify({ crashAfterReverseWrite: 1 }) }, encoding: 'utf8',
  });
  assert.notEqual(crash.status, 0);
  const retry = spawnSync(process.execPath, ['--experimental-strip-types', '--input-type=module', '-e', recoverScript], {
    cwd: process.cwd(), env: { ...process.env, SB_JOURNAL: journalPath }, encoding: 'utf8',
  });
  assert.equal(retry.status, 0, retry.stderr); assert.equal(JSON.parse(retry.stdout).status, 'recovered', retry.stdout);
  assert.equal(JSON.parse(await readFile(config, 'utf8')).model, 'vendor/model');
  assert.equal(await readFile(join(configDir, 'tui.json'), 'utf8').catch(() => null), null);
}));

test('receipt committed before a crash wins over journal cleanup and keeps installation', async () => fixture(async (root) => {
  const configDir = join(root, 'config'); const artifactDir = join(root, 'artifact');
  await mkdir(configDir); await mkdir(artifactDir);
  const input = await validInput(root, configDir, artifactDir);
  const script = `const m=await import(${JSON.stringify(pathToFileURL(join(process.cwd(), 'opencode/install/cli.ts')).href)}); const i=JSON.parse(process.env.SB_INSTALL_INPUT); const p=await m.planInstall(i); await m.applyInstall(p,{crashAfterReceipt:true});`;
  const child = spawnSync(process.execPath, ['--experimental-strip-types', '--input-type=module', '-e', script], { cwd: process.cwd(), env: { ...process.env, SB_INSTALL_INPUT: JSON.stringify(input) }, encoding: 'utf8' });
  assert.notEqual(child.status, 0);
  const result = await recoverInstall(join(configDir, '.symbiosis-brain-install', 'journal.json'));
  assert.equal(result.status, 'recovered');
  assert.equal((await inspectInstall(configDir)).status, 'installed');
}));


test('the same physical canonical skill through a junction is not a competing copy', async () => fixture(async (root) => {
  const configDir = join(root, 'config'); const artifactDir = join(root, 'artifact');
  await mkdir(configDir); await mkdir(artifactDir);
  const input = await validInput(root, configDir, artifactDir);
  const sharedRoot = join(homedir(), '.claude', 'skills');
  const alias = join(sharedRoot, 'brain-init');
  await mkdir(sharedRoot, { recursive: true });
  await symlink(join(input.skillsDir, 'brain-init'), alias, process.platform === 'win32' ? 'junction' : 'dir');
  try {
    await applyInstall(await planInstall(input));
    const doctor = await inspectInstall(configDir);
    assert.equal(doctor.status, 'installed', JSON.stringify(doctor));
  } finally { await rm(alias, { recursive: true, force: true }); }
}));
