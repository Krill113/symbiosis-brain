import { createHash } from 'node:crypto';
import { mkdir, readFile, readdir, rm, writeFile } from 'node:fs/promises';
import { dirname, join, relative, resolve } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const out = join(root, 'dist');
const typescriptPath = join(root, 'node_modules', 'typescript', 'lib', 'typescript.js');
const parserRoot = join(root, 'node_modules', 'jsonc-parser');
const typescript = await import(pathToFileURL(typescriptPath).href).catch(() => {
  throw new Error('build dependency missing: local TypeScript compiler is unavailable; no network install attempted');
});
await readFile(join(parserRoot, 'package.json')).catch(() => {
  throw new Error('build dependency missing: local jsonc-parser package is unavailable');
});

await rm(out, { recursive: true, force: true });
await mkdir(out, { recursive: true });

async function copyTree(from, to, include = () => true) {
  await mkdir(to, { recursive: true });
  for (const entry of await readdir(from, { withFileTypes: true })) {
    const source = join(from, entry.name), destination = join(to, entry.name);
    if (entry.isDirectory()) await copyTree(source, destination, include);
    else if (include(source)) await writeFile(destination, await readFile(source));
  }
}

// Host-loaded TypeScript keeps its relative .ts imports and runtime closure intact.
await writeFile(join(out, 'server.ts'), await readFile(join(root, 'server.ts')));
await writeFile(join(out, 'tui.tsx'), await readFile(join(root, 'tui.tsx')));
await copyTree(join(root, 'src'), join(out, 'src'));
await copyTree(join(root, 'install'), join(out, 'install'), (path) => path.endsWith('.ts') || path.endsWith('.py'));

// Compile the standalone installer with the already-installed TypeScript API.
// Its small ESM closure remains local files; relative TS specifiers become MJS.
const installerFiles = ['main', 'cli', 'commands', 'config-edit', 'migration', 'ownership', 'legacy-discovery'];
for (const name of installerFiles) {
  const source = await readFile(join(root, 'install', `${name}.ts`), 'utf8');
  const js = typescript.transpileModule(source, {
    compilerOptions: { target: typescript.ScriptTarget.ES2022, module: typescript.ModuleKind.ESNext },
    fileName: `${name}.ts`,
  }).outputText.replace(/((?:from\s*|import\s*)['"]\.{1,2}\/[^'"]+)\.ts(['"])/g, '$1.mjs$2');
  const destination = join(out, 'install', `${name}.mjs`);
  await mkdir(dirname(destination), { recursive: true });
  await writeFile(destination, js);
}

await copyTree(parserRoot, join(out, 'node_modules', 'jsonc-parser'));
const files = [];
async function collect(directory) {
  for (const entry of await readdir(directory, { withFileTypes: true })) {
    const path = join(directory, entry.name);
    if (entry.isDirectory()) await collect(path);
    else if (relative(out, path) !== 'manifest.json') {
      const bytes = await readFile(path);
      files.push({ path: relative(out, path).replaceAll('\\', '/'), sha256: createHash('sha256').update(bytes).digest('hex'), size: bytes.length });
    }
  }
}
await collect(out);
files.sort((left, right) => left.path < right.path ? -1 : left.path > right.path ? 1 : 0);
const sourceFiles = ['server.ts', 'tui.tsx', 'install/main.ts', 'install/cli.ts', 'install/commands.ts',
  'install/config-edit.ts', 'install/migration.ts', 'install/ownership.ts', 'install/legacy-discovery.ts',
  'install/install_lock.py', 'package.json', 'package-lock.json', ...files
    .filter((item) => item.path.startsWith('src/'))
    .map((item) => item.path)];
const sources = [];
for (const path of [...new Set(sourceFiles)].sort((left, right) => left < right ? -1 : left > right ? 1 : 0)) {
  const bytes = await readFile(join(root, path));
  sources.push({ path, sha256: createHash('sha256').update(bytes).digest('hex'), size: bytes.length });
}
await writeFile(join(out, 'manifest.json'), `${JSON.stringify({ schema: 1, entrypoint: 'install/main.mjs', sources, files }, null, 2)}\n`);
console.log(`Built offline OpenCode installer artifact (${files.length} hashed files)`);
