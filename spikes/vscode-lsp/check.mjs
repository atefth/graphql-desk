import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { dirname, resolve } from 'node:path';

const dir = dirname(fileURLToPath(import.meta.url));
const cli = resolve(dir, 'node_modules/graphql-language-service-cli/bin/graphql.js');
const rootUri = pathToFileURL(`${dir}/`).href;
const documentUri = pathToFileURL(resolve(dir, 'operation.graphql')).href;
// VS Code's language client uses the same Content-Length framed stdio transport.
const server = spawn(process.execPath, [cli, 'server', '--method', 'stream', '--configDir', dir], {
  cwd: dir,
  stdio: ['pipe', 'pipe', 'pipe'],
});
const pending = new Map();
const notifications = [];
let input = Buffer.alloc(0);
let nextId = 1;
let serverError = '';

const send = message => {
  const body = Buffer.from(JSON.stringify({ jsonrpc: '2.0', ...message }));
  server.stdin.write(`Content-Length: ${body.length}\r\n\r\n`);
  server.stdin.write(body);
};
const accept = message => {
  if (message.method === 'workspace/configuration') {
    send({ id: message.id, result: message.params.items.map(() => ({})) });
  } else if (pending.has(message.id)) {
    pending.get(message.id)(message);
    pending.delete(message.id);
  } else if (message.method) {
    notifications.push(message);
  }
};

server.stdout.on('data', chunk => {
  input = Buffer.concat([input, chunk]);
  while (true) {
    const boundary = input.indexOf('\r\n\r\n');
    if (boundary < 0) break;
    const header = input.subarray(0, boundary).toString();
    const length = Number(header.match(/Content-Length: (\d+)/i)?.[1]);
    if (!Number.isFinite(length) || input.length < boundary + 4 + length) break;
    const body = input.subarray(boundary + 4, boundary + 4 + length).toString();
    input = input.subarray(boundary + 4 + length);
    accept(JSON.parse(body));
  }
});
server.stderr.on('data', chunk => { serverError += chunk; });

const request = (method, params) => new Promise((resolve, reject) => {
  const id = nextId++;
  const timeout = setTimeout(() => {
    pending.delete(id);
    reject(new Error(`Timed out waiting for ${method}: ${serverError}`));
  }, 5000);
  pending.set(id, response => {
    clearTimeout(timeout);
    if (response.error) reject(new Error(response.error.message));
    else resolve(response.result);
  });
  send({ id, method, params });
});
const notify = (method, params) => send({ method, params });
const waitForNotification = async (method, predicate = () => true) => {
  const deadline = Date.now() + 5000;
  while (Date.now() < deadline) {
    const index = notifications.findIndex(item => item.method === method && predicate(item.params));
    if (index !== -1) return notifications.splice(index, 1)[0].params;
    await new Promise(resolve => setTimeout(resolve, 10));
  }
  throw new Error(`Timed out waiting for ${method}: ${serverError}`);
};

try {
  await request('initialize', {
    processId: process.pid,
    rootUri,
    workspaceFolders: [{ uri: rootUri, name: 'vscode-lsp-spike' }],
    capabilities: { workspace: { configuration: true } },
  });
  notify('initialized', {});

  const invalid = 'query UserCard { user(id: "1") { missingField } }';
  notify('textDocument/didOpen', {
    textDocument: { uri: documentUri, languageId: 'graphql', version: 1, text: invalid },
  });
  const diagnosticParams = await waitForNotification(
    'textDocument/publishDiagnostics',
    params => params.uri === documentUri,
  );
  const missingField = diagnosticParams.diagnostics.find(({ message }) => message.includes('missingField'));
  const start = invalid.indexOf('missingField');
  assert.deepEqual(missingField.range, {
    start: { line: 0, character: start },
    end: { line: 0, character: start + 'missingField'.length + 1 },
  });

  const editing = 'query UserCard { user(id: "1") { na } }';
  notify('textDocument/didChange', {
    textDocument: { uri: documentUri, version: 2 },
    contentChanges: [{ text: editing }],
  });
  const changed = await waitForNotification(
    'textDocument/publishDiagnostics',
    params => params.uri === documentUri,
  );
  assert.ok(changed.diagnostics.some(({ message }) => message.includes('na')));

  const completion = await request('textDocument/completion', {
    textDocument: { uri: documentUri },
    position: { line: 0, character: editing.indexOf('na }') + 2 },
  });
  const items = Array.isArray(completion) ? completion : completion.items;
  assert.ok(items.some(({ label }) => label === 'name'));

  const hoverText = 'query UserCard { user(id: "1") { name } }';
  notify('textDocument/didChange', {
    textDocument: { uri: documentUri, version: 3 },
    contentChanges: [{ text: hoverText }],
  });
  await waitForNotification('textDocument/publishDiagnostics', params => params.uri === documentUri);
  const position = { line: 0, character: hoverText.indexOf('name') + 1 };
  const hover = await request('textDocument/hover', { textDocument: { uri: documentUri }, position });
  assert.match(JSON.stringify(hover?.contents), /String!/);
  const definition = await request('textDocument/definition', { textDocument: { uri: documentUri }, position });
  assert.ok((Array.isArray(definition) ? definition : [definition]).some(({ uri }) => uri.toLowerCase().endsWith('/schema.graphql')));
  console.log('GraphQL LSP stdio config discovery, diagnostics, completion, hover, and definition passed.');
} finally {
  server.kill();
}
