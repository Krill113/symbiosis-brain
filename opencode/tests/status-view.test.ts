import assert from 'node:assert/strict';
import test from 'node:test';
import { createStatusView } from '../src/host-ui.ts';
import { projectStatus, readStatusInput, type StatusInput } from '../src/status-view.ts';

const base = (overrides: Partial<StatusInput> = {}): StatusInput => ({
  currentSelection: null,
  lastUsed: null,
  usage: null,
  limit: null,
  compactionInvalidated: false,
  mcp: null,
  width: 120,
  ...overrides,
});

test('no assistant usage is unknown rather than zero', () => {
  const view = projectStatus(base());
  assert.match(view.text, /Context: \?/);
  assert.equal(view.unknown.context, true);
  assert.doesNotMatch(view.text, /Context: 0/);
});

test('normalizes usage exactly like the pinned native sidebar without double counting', () => {
  const view = projectStatus(base({
    lastUsed: { model: 'fixture/sol', variant: 'medium' },
    usage: { input: 10, output: 5, reasoning: 2, cacheRead: 3, cacheWrite: 4 },
    limit: 100,
  }));
  assert.equal(view.contextUsed, 24);
  assert.equal(view.contextPercent, 24);
  assert.match(view.text, /Used: fixture\/sol \/ medium/);
  assert.match(view.text, /Context: 24 \/ 100 · 24%/);
});

test('compaction response from another model does not replace working model or limit', () => {
  const messages = [
    { id: 'assistant-work', role: 'assistant', providerID: 'fixture', modelID: 'work', summary: false, tokens: { input: 20, output: 3, reasoning: 0, cache: { read: 2, write: 0 } } },
    { id: 'compact-user', role: 'user' },
    { id: 'assistant-compact', role: 'assistant', providerID: 'fixture', modelID: 'compact', summary: true, tokens: { input: 4, output: 2, reasoning: 0, cache: { read: 0, write: 0 } } },
  ];
  const api = fakeApi(messages, { 'compact-user': [{ type: 'compaction' }] });
  const input = readStatusInput(api, 'session-a');
  assert.deepEqual(input.lastUsed, { model: 'fixture/work', variant: null });
  assert.equal(input.compactionInvalidated, true);
  assert.equal(input.usage, null);
  assert.equal(input.limit, null);
  assert.match(projectStatus(input).text, /Context: stale/);
});

test('new working response clears compaction invalidation', () => {
  const messages = [
    { id: 'compact-user', role: 'user' },
    { id: 'assistant-work', role: 'assistant', providerID: 'fixture', modelID: 'work', summary: false, variant: 'high', tokens: { input: 8, output: 2, reasoning: 1, cache: { read: 4, write: 1 } } },
  ];
  const input = readStatusInput(fakeApi(messages, { 'compact-user': [{ type: 'compaction' }] }), 'session-a');
  assert.equal(input.compactionInvalidated, false);
  assert.deepEqual(input.usage, { input: 8, output: 2, reasoning: 1, cacheRead: 4, cacheWrite: 1 });
  assert.equal(input.limit, 64);
});

test('reader excludes disabled MCP entries from the enabled denominator', () => {
  const api = fakeApi([], {}, [{ name: 'connected', status: 'connected' }, { name: 'disabled', status: 'disabled' }, { name: 'failed', status: 'failed' }]);
  const input = readStatusInput(api, 'session-a');
  const view = projectStatus(input);
  assert.deepEqual(input.mcp, { connected: 1, enabled: 2 });
  assert.match(view.text, /MCP: 1\/2/);
  const empty = projectStatus(base({ mcp: { connected: 0, enabled: 0 } }));
  assert.match(empty.text, /MCP: 0\/0/);
  assert.equal(empty.unknown.mcp, false);
});

test('supported current selection before submit is distinct from last-used fallback', () => {
  const selected = projectStatus(base({
    currentSelection: { model: 'fixture/next', variant: 'high' },
    lastUsed: { model: 'fixture/previous', variant: 'medium' },
  }));
  const fallback = projectStatus(base({ lastUsed: { model: 'fixture/previous', variant: 'medium' } }));
  assert.match(selected.text, /fixture\/next \/ high/);
  assert.doesNotMatch(selected.text, /Used:/);
  assert.match(fallback.text, /Used: fixture\/previous \/ medium/);
});

test('last-used data is labeled and no quota is fabricated', () => {
  const view = projectStatus(base({
    lastUsed: { model: 'fixture/sol', variant: 'medium' },
    mcp: { connected: 1, enabled: 2 },
    width: 80,
  }));
  assert.ok(view.text.includes('Used:'));
  assert.ok(view.text.includes('Context: ?'));
  assert.ok(view.text.includes('MCP: 1/2'));
  assert.ok(!view.text.includes('$'));
});

test('narrow width shortens labels without losing MCP status', () => {
  const view = projectStatus(base({
    currentSelection: { model: 'provider/very-long-model-name', variant: 'high' },
    mcp: { connected: 2, enabled: 3 },
    width: 34,
  }));
  assert.ok(view.text.length <= 34);
  assert.match(view.text, /2\/3$/);
});

test('populated narrow status progressively compacts counts and retains model, variant, context and MCP', () => {
  for (const width of [40, 48]) {
    const view = projectStatus(base({
      lastUsed: { model: 'provider/very-long-model-name', variant: 'medium' },
      usage: { input: 265000, output: 500, reasoning: 200, cacheRead: 1000, cacheWrite: 0 },
      limit: 922000,
      mcp: { connected: 2, enabled: 3 },
      width,
    }));
    assert.equal(displayWidth(view.text), width);
    assert.match(view.text, /Used: .+ \/ medium/);
    assert.doesNotMatch(view.text, /Used:  \/ medium/);
    assert.match(view.text, /266\.7k\/922k/);
    assert.match(view.text, /2\/3$/);
    assert.doesNotMatch(view.text, /M…/);
  }
  assert.match(projectStatus(base({
    lastUsed: { model: 'provider/very-long-model-name', variant: 'medium' },
    usage: { input: 265000, output: 500, reasoning: 200, cacheRead: 1000, cacheWrite: 0 },
    limit: 922000,
    mcp: { connected: 2, enabled: 3 },
    width: 40,
  })).text, /Used: … \/ medium/);
});

test('MCP state remains unknown on not-ready and reader failure; message failure does not hide observed MCP', () => {
  const notReady = fakeApi([], {}, [{ status: 'connected' }]);
  notReady.state.ready = false;
  const beforeReady = readStatusInput(notReady);
  assert.equal(beforeReady.mcp, null);
  assert.equal(projectStatus(beforeReady).unknown.mcp, true);

  const failedMcp = fakeApi([]);
  failedMcp.state.mcp = () => { throw new Error('synthetic mcp unavailable'); };
  const unavailable = projectStatus(readStatusInput(failedMcp));
  assert.match(unavailable.text, /MCP: \?\/\?/);
  assert.equal(unavailable.unknown.mcp, true);

  const failedMessages = fakeApi([], {}, [{ status: 'connected' }]);
  failedMessages.state.session.messages = () => { throw new Error('synthetic messages unavailable'); };
  const partial = projectStatus(readStatusInput(failedMessages, 'session-a'));
  assert.match(partial.text, /MCP: 1\/1/);
  assert.equal(partial.unknown.mcp, false);
});

test('home route still reads available global MCP state', () => {
  const api = fakeApi([], {}, [{ status: 'connected' }, { status: 'disabled' }]);
  const view = projectStatus(readStatusInput(api));
  assert.match(view.text, /MCP: 1\/1/);
  assert.equal(view.unknown.model, true);
});

test('status host reader re-reads width on resize-only updates', () => {
  const api = fakeApi([]);
  let width = 100;
  let reads = 0;
  const status = createStatusView(api, () => {
    reads++;
    return width;
  });
  assert.ok(status().text.length <= 100);
  width = 22;
  assert.ok(displayWidth(status().text) <= 22);
  assert.equal(reads, 2);
});

function fakeApi(messages: any[], parts: Record<string, any[]> = {}, mcpItems: any[] = [{ name: 'on', status: 'connected' }, { name: 'off', status: 'disabled' }, { name: 'failed', status: 'failed' }]) {
  return {
    state: {
      ready: true,
      session: { messages: () => messages },
      part: (messageID: string) => parts[messageID] ?? [],
      provider: [{ id: 'fixture', models: { work: { limit: { context: 64 } } } }],
      mcp: () => mcpItems,
    },
    renderer: { width: 100 },
    route: { current: { name: 'home' } },
  } as any;
}

function displayWidth(value: string): number {
  return [...value].reduce((width, char) => {
    const cp = char.codePointAt(0) ?? 0;
    return width + (cp >= 0x1100 && (cp <= 0x115f || cp >= 0x2e80) ? 2 : 1);
  }, 0);
}
