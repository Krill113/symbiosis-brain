import { applyEdits, findNodeAtLocation, modify, parse, parseTree, printParseErrorCode, type ParseError } from 'jsonc-parser';

export interface JsonChange {
  path: (string | number)[];
  before: unknown;
  after: unknown;
  identity?: string;
  beforeIdentity?: string;
  afterIdentity?: string;
}

export class ConfigConflict extends Error {
  readonly path: (string | number)[];
  readonly reason: string;
  constructor(path: (string | number)[], reason: string) { super(`config-conflict:${path.join('.')}:${reason}`); this.path = path; this.reason = reason; }
}

export function parseConfig(text: string): Record<string, unknown> {
  const errors: ParseError[] = [];
  const value = parse(text, errors, { allowTrailingComma: true, disallowComments: false });
  if (errors.length || !value || typeof value !== 'object' || Array.isArray(value)) {
    throw new Error(`invalid-config:${errors.map((e) => printParseErrorCode(e.error)).join(',')}`);
  }
  return value as Record<string, unknown>;
}

function equal(a: unknown, b: unknown): boolean { return JSON.stringify(a) === JSON.stringify(b); }

function resolvePath(root: unknown, path: (string | number)[], identity?: string): { parent: Record<string | number, unknown> | unknown[]; key: string | number; value: unknown } {
  let node: any = root;
  for (const part of path.slice(0, -1)) {
    if (node === null || typeof node !== 'object' || !(part in node)) throw new ConfigConflict(path, 'missing-parent');
    node = node[part];
  }
  let key = path[path.length - 1]!;
  if (Array.isArray(node) && identity) {
    const index = node.findIndex((item) => item === identity || (Array.isArray(item) && item[0] === identity));
    if (index >= 0) key = index;
    else if (typeof key === 'number' && key > node.length) key = node.length;
  }
  return { parent: node, key, value: node?.[key] };
}

export function applyJsonChange(text: string, change: JsonChange, direction: 'forward' | 'reverse', strict = true): string {
  const config = parseConfig(text);
  const identity = direction === 'forward' ? (change.beforeIdentity ?? change.identity) : (change.afterIdentity ?? change.identity);
  const { parent, key, value } = resolvePath(config, change.path, identity);
  const expected = direction === 'forward' ? change.before : change.after;
  const desired = direction === 'forward' ? change.after : change.before;
  if (equal(value, desired)) return text;
  if (strict && !equal(value, expected)) throw new ConfigConflict(change.path, 'value-changed');
  if (Array.isArray(parent) && desired === undefined) {
    return removeArrayElement(text, change.path.slice(0, -1), Number(key));
  }
  const actualPath = [...change.path.slice(0, -1), key];
  const edits = modify(text, actualPath, desired, { formattingOptions: { insertSpaces: true, tabSize: 2, eol: text.includes('\r\n') ? '\r\n' : '\n' } });
  return applyEdits(text, edits);
}

function removeArrayElement(text: string, arrayPath: (string | number)[], index: number): string {
  const errors: ParseError[] = [];
  const tree = parseTree(text, errors, { allowTrailingComma: true, disallowComments: false });
  if (!tree || errors.length) throw new Error('invalid-config-during-array-remove');
  const array = findNodeAtLocation(tree, arrayPath);
  if (!array || array.type !== 'array' || !array.children || index < 0 || index >= array.children.length) throw new ConfigConflict(arrayPath, 'array-identity-missing');
  const item = array.children[index]!;
  let start = item.offset;
  let end = item.offset + item.length;
  if (array.children.length > 1 && index < array.children.length - 1) {
    while (end < text.length && /\s/.test(text[end]!)) end++;
    if (text[end] !== ',') throw new Error('invalid-array-separator');
    end++;
  } else if (array.children.length > 1) {
    while (start > array.offset && /\s/.test(text[start - 1]!)) start--;
    if (text[start - 1] !== ',') throw new Error('invalid-array-separator');
    start--;
  }
  return applyEdits(text, [{ offset: start, length: end - start, content: '' }]);
}

export function validateHostConfig(text: string): Record<string, unknown> {
  const config = parseConfig(text);
  if (config.plugin !== undefined) {
    const validPlugin = (entry: unknown) => typeof entry === 'string'
      || (Array.isArray(entry) && entry.length === 2 && typeof entry[0] === 'string'
        && !!entry[1] && typeof entry[1] === 'object' && !Array.isArray(entry[1]));
    if (!Array.isArray(config.plugin) || !config.plugin.every(validPlugin)) throw new Error('schema-invalid:plugin');
  }
  if (config.mcp !== undefined) {
    const validMcpEntry = (entry: unknown) => {
      if (!entry || typeof entry !== 'object' || Array.isArray(entry)) return false;
      const command = (entry as Record<string, unknown>).command;
      return command === undefined || (Array.isArray(command) && command.every((part) => typeof part === 'string'));
    };
    if (!config.mcp || typeof config.mcp !== 'object' || Array.isArray(config.mcp)
      || !Object.values(config.mcp).every(validMcpEntry)) throw new Error('schema-invalid:mcp');
  }
  if (config.skills !== undefined) {
    if (!config.skills || typeof config.skills !== 'object' || Array.isArray(config.skills)) throw new Error('schema-invalid:skills');
    const paths = (config.skills as Record<string, unknown>).paths;
    if (paths !== undefined && (!Array.isArray(paths) || paths.some((path) => typeof path !== 'string'))) throw new Error('schema-invalid:skills');
  }
  if (config.command !== undefined && (!config.command || typeof config.command !== 'object' || Array.isArray(config.command))) throw new Error('schema-invalid:command');
  return config;
}

export function changeFromCurrent(text: string, path: (string | number)[], after: unknown, identity?: string, afterIdentity?: string): JsonChange {
  const config = parseConfig(text);
  const { value } = resolvePath(config, path, identity);
  return { path, before: value, after, ...(identity ? { identity, beforeIdentity: identity } : {}), ...(afterIdentity ? { afterIdentity } : {}) };
}

export function isJsonChangeInstalled(text: string, change: JsonChange): boolean {
  try { return equal(resolvePath(parseConfig(text), change.path, change.identity).value, change.after); }
  catch { return false; }
}

export function isJsonChangeAtBefore(text: string, change: JsonChange): boolean {
  try { return equal(resolvePath(parseConfig(text), change.path, change.beforeIdentity ?? change.identity).value, change.before); }
  catch { return change.before === undefined && (change.path.length > 1); }
}

function nodeAtChange(text: string, change: JsonChange) {
  const errors: ParseError[] = [];
  const root = parseTree(text, errors, { allowTrailingComma: true, disallowComments: false });
  if (!root || errors.length) throw new ConfigConflict(change.path, 'invalid-config');
  const identity = change.afterIdentity ?? change.identity;
  if (identity) {
    const parent = findNodeAtLocation(root, change.path.slice(0, -1));
    if (parent?.type === 'array' && parent.children) {
      return parent.children.find((node) => node.value === identity || (node.type === 'array' && node.children?.[0]?.value === identity));
    }
  }
  return findNodeAtLocation(root, change.path);
}

function commentsInNode(text: string, node: NonNullable<ReturnType<typeof nodeAtChange>>): string[] {
  const source = text.slice(node.offset, node.offset + node.length);
  const comments: string[] = [];
  let index = 0;
  while (index < source.length) {
    if (source[index] === '"') {
      index++;
      while (index < source.length) {
        if (source[index] === '\\') { index += 2; continue; }
        if (source[index] === '"') break;
        index++;
      }
    } else if (source[index] === '/' && source[index + 1] === '/') {
      const end = source.indexOf('\n', index + 2);
      comments.push(source.slice(index, end < 0 ? source.length : end));
      index = end < 0 ? source.length : end;
    } else if (source[index] === '/' && source[index + 1] === '*') {
      const close = source.indexOf('*/', index + 2);
      const end = close < 0 ? source.length : close + 2;
      comments.push(source.slice(index, end)); index = end;
    }
    index++;
  }
  return comments;
}

export function hasJsonChangeCommentDrift(installedText: string, currentText: string, change: JsonChange): boolean {
  try {
    const installed = nodeAtChange(installedText, change);
    const current = nodeAtChange(currentText, change);
    if (!installed || !current) return false;
    return JSON.stringify(commentsInNode(installedText, installed)) !== JSON.stringify(commentsInNode(currentText, current));
  } catch { return false; }
}
