import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { readFile } from 'node:fs/promises';
import { join } from 'node:path';
import { spawnSync } from 'node:child_process';
import test from 'node:test';

test('built installer manifest hashes every shipped artifact file', async () => {
  const root = join(process.cwd(), 'opencode', 'dist');
  const manifest = JSON.parse(await readFile(join(root, 'manifest.json'), 'utf8')) as {
    entrypoint: string;
    files: { path: string; sha256: string; size: number }[];
    sources: { path: string; sha256: string; size: number }[];
  };
  assert.equal(manifest.entrypoint, 'install/main.mjs');
  assert.ok(manifest.files.length >= 2);
  assert.ok(manifest.sources.length >= 10);
  for (const item of manifest.files) {
    const bytes = await readFile(join(root, item.path));
    assert.equal(bytes.length, item.size, item.path);
    assert.equal(createHash('sha256').update(bytes).digest('hex'), item.sha256, item.path);
  }
  for (const item of manifest.sources) {
    const bytes = await readFile(join(process.cwd(), 'opencode', item.path));
    assert.equal(bytes.length, item.size, `source ${item.path}`);
    assert.equal(createHash('sha256').update(bytes).digest('hex'), item.sha256, `source ${item.path}`);
  }
  for (const path of ['server.ts', 'tui.tsx', 'install/install_lock.py', 'install/main.mjs', 'node_modules/jsonc-parser/package.json'])
    assert.ok(manifest.files.some((item) => item.path === path), `manifest includes runtime file ${path}`);
  assert.ok(manifest.files.some((item) => item.path === 'install/ownership.ts'), 'RPC lock helper source is packaged beside the compiled ownership module');
  const ownership = await readFile(join(root, 'install', 'ownership.mjs'), 'utf8');
  assert.match(ownership, /install_lock\.py/);
  const helper = await readFile(join(root, 'install', 'install_lock.py'), 'utf8');
  assert.match(helper, /json\.loads|json\.load/);
  const malformed = spawnSync(process.execPath, [join(root, 'install/main.mjs')], {
    input: '{"command":"invalid"}', encoding: 'utf8', timeout: 10_000, windowsHide: true,
  });
  assert.equal(malformed.status, 1, malformed.stderr);
  assert.deepEqual(JSON.parse(malformed.stderr), { ok: false, error: 'invalid-request' });
});
