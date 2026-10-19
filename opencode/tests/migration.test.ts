import assert from 'node:assert/strict';
import { mkdtemp, mkdir, writeFile, rm, copyFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import test from 'node:test';
import { inspectProjectLegacy, planMigration } from '../install/migration.ts';
import { applyInstall, inspectInstall, planInstall, portableMcpCommand } from '../install/cli.ts';
import { readFile } from 'node:fs/promises';

async function validInput(root: string, configDir: string, artifactDir: string) {
  const vault = join(root, 'synthetic-vault'); await mkdir(vault, { recursive: true });
  await copyFile(join(process.cwd(), 'opencode', 'server.ts'), join(artifactDir, 'server.ts'));
  await copyFile(join(process.cwd(), 'opencode', 'tui.tsx'), join(artifactDir, 'tui.tsx'));
  const pythonExe = join(process.env.VIRTUAL_ENV!, 'Scripts', 'python.exe');
  const packageDir = join(process.cwd(), 'src', 'symbiosis_brain');
  const skillsDir = join(process.cwd(), 'skills');
  return { configDir, artifactDir, pythonExe, vault, packageDir, skillsDir, mcpCommand: portableMcpCommand(pythonExe, vault) };
}

test('known legacy copy is backed up while unknown modified copy requires a decision', async () => {
  const root = await mkdtemp(join(tmpdir(), 'sb-migration-'));
  try {
    const project = join(root, 'project'); await mkdir(project);
    const known = join(project, '.opencode', 'plugin', 'brain-recall.ts'); await mkdir(join(project, '.opencode', 'plugin'), { recursive: true });
    await writeFile(known, '/* synthetic legacy adapter */\nexport default {}\n');
    const plan = await planMigration({ roots: [project], backupDir: join(root, 'backup'), decisions: {} });
    assert.equal(plan.items[0]?.kind, 'known'); assert.equal(plan.items[0]?.action, 'backup-and-remove');
    await writeFile(known, '/* user edit */\nexport default {}\n');
    const unknown = await planMigration({ roots: [project], backupDir: join(root, 'backup'), decisions: {} });
    assert.equal(unknown.items[0]?.action, 'decision-required');
    const secondProject = join(root, 'second-project');
    const secondLegacy = join(secondProject, '.opencode', 'plugins', 'brain-recall.ts');
    await mkdir(join(secondProject, '.opencode', 'plugins'), { recursive: true });
    await writeFile(secondLegacy, '/* synthetic legacy adapter */\nexport default {}\n');
    await writeFile(join(secondProject, 'opencode.jsonc'), '{"plugin":["./.opencode/plugins/brain-recall.ts"]}');
    const entryCheck = await inspectProjectLegacy(secondProject, join(root, 'global-config'));
    assert.equal(entryCheck.active, true);
    assert.ok(entryCheck.paths.some((path) => path.includes('brain-recall.ts')));
    assert.ok(entryCheck.diagnostics.some((diagnostic) => diagnostic.includes('project-local-scan-only')));
  } finally { await rm(root, { recursive: true, force: true }); }
});

test('installer journals a known legacy migration, stores backup outside discovery, and scopes doctor scan', async () => {
  const root = await mkdtemp(join(tmpdir(), 'sb-migration-install-'));
  try {
    const configDir = join(root, 'config'); const artifactDir = join(root, 'artifact'); const project = join(root, 'project');
    await mkdir(configDir); await mkdir(artifactDir); await mkdir(join(project, '.opencode', 'plugin'), { recursive: true });
    const legacy = join(project, '.opencode', 'plugin', 'brain-recall.ts');
    const legacyText = '/* synthetic legacy adapter */\nexport default {}\n'; await writeFile(legacy, legacyText);
    const input = { ...(await validInput(root, configDir, artifactDir)), migration: { roots: [project], decisions: {} } };
    const receipt = await applyInstall(await planInstall(input));
    const doctor = await inspectInstall(configDir);
    assert.equal(doctor.status, 'installed', JSON.stringify(doctor));
    assert.equal(await readFile(legacy, 'utf8').catch(() => null), null);
    const migrated = receipt.operations.find((operation) => operation.target === legacy);
    assert.ok(migrated?.backup);
    assert.equal(await readFile(migrated.backup!, 'utf8'), legacyText);
    assert.ok(receipt.operations.every((operation) => !operation.backup?.includes('.opencode/plugin')));
  } finally { await rm(root, { recursive: true, force: true }); }
});
