import assert from 'node:assert/strict';
import test from 'node:test';
import type { Hooks } from '@opencode-ai/plugin';
import type { TuiPluginApi, TuiPluginModule, TuiSlotPlugin } from '@opencode-ai/plugin/tui';
import type { JSX } from '@opentui/solid';

const hookContracts = {
  'chat.message': async (input, output) => {
    input.sessionID;
    output.message;
    output.parts;
  },
  'shell.env': async (input, output) => {
    input.sessionID;
    output.env;
  },
  'tool.execute.after': async (input, output) => {
    input.callID;
    output.title;
    output.output;
    output.metadata;
  },
  'experimental.chat.system.transform': async (input, output) => {
    input.sessionID;
    input.model;
    output.system;
    // @ts-expect-error The pinned public hook has no request-purpose field.
    input.requestPurpose;
  },
} satisfies Hooks;

const tuiModule = {
  id: 'symbiosis-brain.status',
  tui: async (api: TuiPluginApi, options, meta) => {
    api.state.ready;
    api.state.provider;
    api.state.mcp();
    api.state.session.count();
    api.client;
    api.event.on;
    api.route.current;
    const slotID: string = api.slots.register(statusSlot);
    void slotID;
    void options;
    void meta;
  },
} satisfies TuiPluginModule;

const statusSlot: TuiSlotPlugin<{ app_bottom: object }> = {
  slots: {
    app_bottom: () => null as unknown as JSX.Element,
  },
};

const activeSessionID = (api: TuiPluginApi): string | undefined => {
  const route = api.route.current;
  const sessionID = route.name === 'session' ? route.params?.sessionID : undefined;
  return typeof sessionID === 'string' ? sessionID : undefined;
};

void hookContracts;
void tuiModule;
void activeSessionID;

test('system transform exposes session and model, not request purpose', () => {
  const input = { sessionID: 'session-a', model: {} };

  assert.equal(Object.hasOwn(input, 'sessionID'), true);
  assert.equal(Object.hasOwn(input, 'model'), true);
  assert.equal(Object.hasOwn(input, 'requestPurpose'), false);
});

test('tool after output retains native result fields', () => {
  const output = {
    title: 'Read file',
    output: 'synthetic result',
    metadata: { source: 'fixture' },
    attachments: [],
  };

  assert.deepEqual(Object.keys(output), ['title', 'output', 'metadata', 'attachments']);
});

test('MCP after hook sees raw CallToolResult and preserves unknown/non-text fields on text mutation', () => {
  const output = {
    content: [
      { type: 'text' as const, text: 'synthetic original text' },
      { type: 'image' as const, data: 'c3ludGhldGlj', mimeType: 'image/png' },
    ],
    isError: false,
    structuredContent: { fixture: 'synthetic' },
    _meta: { fixture: 'synthetic metadata' },
    extensionField: { retained: true },
  };

  const text = output.content.find((item) => item.type === 'text');
  assert.ok(text);
  text.text += ' synthetic mutation';

  assert.equal(Object.hasOwn(output, 'title'), false);
  assert.equal(Object.hasOwn(output, 'output'), false);
  assert.equal(output.content[0]?.type, 'text');
  assert.equal(output.content[0]?.text, 'synthetic original text synthetic mutation');
  assert.deepEqual(output.content[1], {
    type: 'image',
    data: 'c3ludGhldGlj',
    mimeType: 'image/png',
  });
  assert.deepEqual(output.structuredContent, { fixture: 'synthetic' });
  assert.deepEqual(output._meta, { fixture: 'synthetic metadata' });
  assert.deepEqual(output.extensionField, { retained: true });
});

test('TUI plugin module is a target-exclusive default export shape', () => {
  const module = { id: 'symbiosis-brain.status', tui: async () => undefined };

  assert.equal(typeof module.id, 'string');
  assert.equal(typeof module.tui, 'function');
  assert.equal(Object.hasOwn(module, 'server'), false);
});
