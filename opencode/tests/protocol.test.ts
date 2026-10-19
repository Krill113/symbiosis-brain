import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import { parseRequestFrame, parseWorkerFrame } from '../src/protocol.ts';

const request = {
  version: 1,
  requestId: 'req-1',
  sessionId: 'session-1',
  generation: 2,
  operation: 'bootstrap',
  cwd: '/work/alpha',
  boundary: '/work/alpha',
  origin: 'user',
  capabilityRevision: 3,
  capabilities: { tools: [], mcp: [], skills: [] },
};

test('request parser validates and preserves correlation fields', () => {
  assert.deepEqual(parseRequestFrame(JSON.stringify(request)), request);
  assert.throws(() => parseRequestFrame(JSON.stringify({ ...request, generation: true })));
  assert.throws(() => parseRequestFrame(JSON.stringify({ ...request, sessionId: 'other' }), 'session-1'));
  assert.throws(() => parseRequestFrame(JSON.stringify({ ...request, boundary: undefined, scope: 'alpha' })));
  assert.throws(() => parseRequestFrame('{'));
});

test('frame limit measures UTF-8 bytes before JSON parsing', () => {
  assert.throws(() => parseRequestFrame(JSON.stringify({ ...request, query: 'я'.repeat(40_000) })));
});

test('worker hello and result are discriminated, malformed results are not empty success', () => {
  assert.equal(parseWorkerFrame({ version: 1, kind: 'hello', ready: true, role: 'control' }).kind, 'hello');
  assert.equal(parseWorkerFrame({
    version: 1, kind: 'result', requestId: 'r', sessionId: 's', generation: 1,
    capabilityRevision: 1, status: 'ok', scope: null, blocks: [], diagnostics: [],
  }).kind, 'result');
  assert.throws(() => parseWorkerFrame({ version: 1, kind: 'result', blocks: [] }));
  const resourcesResult = {
    version: 1, kind: 'result', requestId: 'r', sessionId: 's', generation: 0,
    capabilityRevision: 0, status: 'ok', scope: null, blocks: [], diagnostics: [],
  };
  assert.throws(() => parseWorkerFrame(resourcesResult, 'resources'));
  assert.equal(parseWorkerFrame({ ...resourcesResult, status: 'degraded' }, 'resources').kind, 'result');
});

test('Python and TypeScript consume the same JSON protocol vectors', () => {
  const vectors = JSON.parse(readFileSync(new URL('../../tests/opencode/protocol-vectors.json', import.meta.url), 'utf8'));
  assert.deepEqual(parseRequestFrame(JSON.stringify(vectors.request)), vectors.request);
  assert.deepEqual(parseWorkerFrame(vectors.result), vectors.result);
  assert.deepEqual(parseWorkerFrame(vectors.richResult, 'resources'), vectors.richResult);
  for (const item of vectors.invalidRequests) {
    assert.throws(
      () => parseRequestFrame(JSON.stringify({ ...vectors.request, ...item.change })),
      (error: unknown) => error instanceof Error && 'reason' in error && error.reason === item.reason,
    );
  }
  for (const item of vectors.invalidResults) {
    assert.throws(
      () => parseWorkerFrame({ ...vectors.result, ...item.change }),
      (error: unknown) => error instanceof Error && 'reason' in error && error.reason === item.reason,
    );
  }
  for (const item of vectors.invalidHellos) {
    assert.throws(
      () => parseWorkerFrame(item.frame),
      (error: unknown) => error instanceof Error && 'reason' in error && error.reason === item.reason,
    );
  }
  const hugeDigits = '9'.repeat(5000);
  const hugeCases = [
    ['request', parseRequestFrame, 'invalid-generation'],
    ['result', parseWorkerFrame, 'invalid-generation'],
    ['hello', parseWorkerFrame, 'invalid-worker-frame'],
  ] as const;
  for (const [name, parser, reason] of hugeCases) {
    const raw = vectors.hugeIntegerFrameTemplates[name].replace('__DIGITS__', hugeDigits);
    assert.ok(new TextEncoder().encode(raw).byteLength < 64 * 1024);
    assert.throws(
      () => parser(raw),
      (error: unknown) => error instanceof Error && 'reason' in error && error.reason === reason,
    );
  }
  for (const item of vectors.grepExeCases) {
    const result = structuredClone(vectors.richResult);
    result.resources.grepExe = item.value;
    if (item.valid) {
      assert.equal(parseWorkerFrame(result, 'resources').kind, 'result');
    } else {
      assert.throws(
        () => parseWorkerFrame(result, 'resources'),
        (error: unknown) => error instanceof Error && 'reason' in error && error.reason === item.reason,
      );
    }
  }
});
