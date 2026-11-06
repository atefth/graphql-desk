"use strict";

const assert = require("node:assert/strict");
const { EventEmitter } = require("node:events");
const Module = require("node:module");
const test = require("node:test");
const { createRenameLanguageClient, stopLanguageClient } = require("../src/renameLanguageClient");

function fixture(t, { trusted = false, middleware, language = {}, ...bridgeOptions } = {}) {
  const grants = new Set();
  const state = { starts: 0, disposed: 0, outputDisposed: 0, requests: [], handlers: new Map(), validatedEdits: [], launches: [] };
  class LanguageClient {
    constructor(_id, _name, server, options) { this.options = options; this.server = server; }
    onRequest(type, handler) { state.handlers.set(type.method, handler); }
    async start() {
      if (!this.running) {
        this.running = true;
        if (state.cwdBarrier) await state.cwdBarrier;
        const node = state.debug ? this.server.debug : this.server.run;
        // The installed NodeModule path reads module again immediately before fork.
        const module = node.module;
        state.starts += 1;
        state.launches.push({ module, transport: node.transport, options: node.options });
      }
      await state.startBarrier;
    }
    async sendRequest(type, params, token) {
      // The installed client also routes automatic request startup through start().
      await this.start();
      const next = async () => { state.requests.push(type.method); state.onRequest?.(type); return state.reply ?? true; };
      return this.options.middleware?.sendRequest
        ? this.options.middleware.sendRequest(type, params, token, next) : next();
    }
    async dispose() { state.disposed += 1; this.running = false; }
    validateWorkspaceEdit(edit) { state.validatedEdits.push(edit); return state.validEdit !== false; }
  }
  const originalLoad = Module._load;
  const load = t.mock.method(Module, "_load", function(request, ...args) {
    return request === "vscode-languageclient/node" ? { LanguageClient, TransportKind: { ipc: 1 } }
      : originalLoad.call(this, request, ...args);
  });
  const vscode = {
    workspace: { isTrusted: trusted, onDidGrantWorkspaceTrust(handler) {
      grants.add(handler); return { dispose() { grants.delete(handler); } };
    } },
    window: { createOutputChannel() { return { appendLine() {}, dispose() { state.outputDisposed += 1; } }; } },
  };
  const client = createRenameLanguageClient(vscode, language, { middleware, ...bridgeOptions });
  load.mock.restore();
  t.after(() => client.stop());
  return { client, state, vscode, async grant() {
    vscode.workspace.isTrusted = true;
    await Promise.all([...grants].map(handler => handler()));
  }, grants };
}

test("untrusted activation and requests never start the rename process", async t => {
  const f = fixture(t, { isTrusted: () => true });
  await f.client.start();
  assert.equal(f.state.starts, 0);
  assert.equal(await f.client.extract({}), false);
  await assert.rejects(f.client.client.sendRequest({ method: "textDocument/rename" }, {}), /trusted|Trust/);
  assert.equal(f.state.starts, 0);
  assert.deepEqual(f.state.requests, []);
});

test("a trust grant starts the existing provider after an untrusted no-op", async t => {
  const f = fixture(t);
  await f.client.start();
  await f.grant();
  assert.equal(f.state.starts, 1);
  await f.client.start();
  assert.equal(await f.client.extract({}), true);
  assert.equal(f.state.starts, 1);
  assert.deepEqual(f.state.requests, ["graphqlDesk/extractFragment"]);
});

test("a trusted extract request can lazily start without relying on the grant event", async t => {
  const f = fixture(t);
  f.vscode.workspace.isTrusted = true;
  assert.equal(await f.client.extract({}), true);
  assert.equal(f.state.starts, 1);
});

test("disposal removes the grant listener and prevents subsequent automatic startup", async t => {
  const f = fixture(t);
  await f.client.stop();
  assert.equal(f.grants.size, 0);
  await f.grant();
  await f.client.start();
  assert.equal(await f.client.extract({}), false);
  await assert.rejects(f.client.client.sendRequest({ method: "textDocument/rename" }, {}), /trusted/);
  assert.equal(f.state.starts, 0);
  assert.equal(f.state.disposed, 1);
  assert.equal(f.state.outputDisposed, 1);
});

test("observed trust loss blocks requests and terminates the existing client", async t => {
  const f = fixture(t, { trusted: true });
  await f.client.start();
  f.vscode.workspace.isTrusted = false;
  await assert.rejects(f.client.client.sendRequest({ method: "textDocument/rename" }, {}), /trusted/);
  await f.client.stop();
  assert.equal(f.state.starts, 1);
  assert.equal(f.state.disposed, 1);
  assert.equal(f.grants.size, 0);
  assert.deepEqual(f.state.requests, []);
});

test("trust loss during pending startup is rejected and the child is disposed", async t => {
  const f = fixture(t, { trusted: true });
  let release;
  f.state.startBarrier = new Promise(resolve => { release = resolve; });
  const starting = f.client.start();
  assert.equal(f.state.starts, 1);
  f.vscode.workspace.isTrusted = false;
  release();
  await assert.rejects(starting, /trust changed/);
  await f.client.stop();
  assert.equal(f.state.disposed, 1);
  assert.deepEqual(f.state.requests, []);
});

for (const dispose of [false, true]) {
  test(`${dispose ? "disposal" : "trust loss"} during cwd lookup prevents the initial child spawn`, async t => {
    const f = fixture(t, { trusted: true });
    let release;
    f.state.cwdBarrier = new Promise(resolve => { release = resolve; });
    const starting = f.client.start();
    assert.equal(f.state.starts, 0);
    if (dispose) await f.client.stop();
    else f.vscode.workspace.isTrusted = false;
    release();
    await assert.rejects(starting, /trust/);
    await f.client.stop();
    assert.equal(f.state.starts, 0);
    assert.deepEqual(f.state.launches, []);
    assert.equal(f.state.disposed, 1);
  });
}

test("trusted startup retains library IPC options and the existing debug arguments", async t => {
  const f = fixture(t, { trusted: true });
  await f.client.start();
  const d = fixture(t, { trusted: true });
  d.state.debug = true;
  await d.client.start();
  assert.match(f.state.launches[0].module.replaceAll("\\", "/"), /\/src\/renameServer\.js$/);
  assert.equal(f.state.launches[0].transport, 1);
  assert.equal(f.state.launches[0].options, undefined);
  assert.equal(d.state.launches[0].module, f.state.launches[0].module);
  assert.equal(d.state.launches[0].transport, 1);
  assert.deepEqual(d.state.launches[0].options.execArgv, ["--nolazy", "--inspect=6009"]);
});

test("the final request and apply boundaries recheck trust after middleware waits", async t => {
  let release;
  let entered;
  const waiting = new Promise(resolve => { entered = resolve; });
  const held = new Promise(resolve => { release = resolve; });
  const f = fixture(t, { trusted: true, middleware: {
    async sendRequest(type, params, token, next) { entered(); await held; return next(type, params, token); },
    workspace: { async handleApplyEdit(params, next) { await held; return next(params); } },
  } });
  await f.client.start();
  const request = f.client.client.sendRequest({ method: "textDocument/rename" }, {});
  let applied = false;
  const applying = f.client.client.options.middleware.workspace.handleApplyEdit({}, () => { applied = true; return { applied: true }; });
  await waiting;
  f.vscode.workspace.isTrusted = false;
  release();
  await assert.rejects(request, /trusted/);
  assert.deepEqual(await applying, { applied: false });
  assert.equal(applied, false);
  assert.deepEqual(f.state.requests, []);
});

test("trusted apply delegates the original versioned edit without rewriting it", async t => {
  const f = fixture(t, { trusted: true });
  const params = { edit: { documentChanges: [{ textDocument: { uri: "file:///a.py", version: 7 }, edits: [] }] } };
  const result = await f.client.client.options.middleware.workspace.handleApplyEdit(params, value => {
    assert.equal(value, params);
    assert.equal(value.edit.documentChanges[0].textDocument.version, 7);
    return { applied: true };
  });
  assert.deepEqual(result, { applied: true });
});

test("trust loss during the delegated client's conversion wait blocks the final edit", async t => {
  const f = fixture(t, { trusted: true });
  await f.client.start();
  let release;
  let entered;
  const waiting = new Promise(resolve => { entered = resolve; });
  const converting = new Promise(resolve => { release = resolve; });
  let applied = false;
  const params = { edit: { documentChanges: [{ textDocument: { uri: "file:///a.py", version: 7 }, edits: [] }] } };
  const applying = f.client.client.options.middleware.workspace.handleApplyEdit(params, async value => {
    entered();
    // v10.1.2 waits for its conversion lock before this public final validator.
    await converting;
    if (!f.client.client.validateWorkspaceEdit(value.edit)) return { applied: false };
    applied = true;
    return { applied: true };
  });
  await waiting;
  f.vscode.workspace.isTrusted = false;
  release();
  assert.deepEqual(await applying, { applied: false });
  assert.equal(applied, false);
  assert.deepEqual(f.state.validatedEdits, []);
  assert.equal(f.state.disposed, 1);
});

test("trusted final validation preserves both accepted and rejected base version checks", async t => {
  const f = fixture(t, { trusted: true });
  const edit = { documentChanges: [{ textDocument: { uri: "file:///a.py", version: 7 }, edits: [] }] };
  assert.equal(f.client.client.validateWorkspaceEdit(edit), true);
  f.state.validEdit = false;
  assert.equal(f.client.client.validateWorkspaceEdit(edit), false);
  assert.deepEqual(f.state.validatedEdits, [edit, edit]);
  assert.equal(f.state.validatedEdits[0], edit);
  assert.equal(f.state.validatedEdits[1], edit);
});

test("a prepare response produced after trust loss cannot escape to the server", async t => {
  let release;
  let entered;
  const waiting = new Promise(resolve => { entered = resolve; });
  const result = { placeholder: "Card", range: { start: { line: 0, character: 0 }, end: { line: 0, character: 4 } } };
  const f = fixture(t, { trusted: true, language: { prepareRename() {
    entered(); return new Promise(resolve => { release = () => resolve(result); });
  } } });
  const uri = "file:///a.py";
  f.vscode.Uri = { parse: () => ({ scheme: "file" }) };
  f.vscode.Position = class { constructor(line, character) { this.line = line; this.character = character; } };
  f.vscode.workspace.openTextDocument = async () => ({ uri: { scheme: "file", toString: () => uri }, languageId: "python" });
  await f.client.start();
  const preparing = f.state.handlers.get("graphqlDesk/prepareFragmentRename")({ textDocument: { uri }, position: result.range.start });
  await waiting;
  f.vscode.workspace.isTrusted = false;
  release();
  assert.equal(await preparing, null);
  assert.equal(f.state.disposed, 1);
});

test("rename plans retain exact source versions and are withheld when trust changes during sync", async t => {
  const uri = "file:///a.py";
  const source = "fragment Card on User { id }";
  const f = fixture(t, { trusted: true, language: { rename: async () => [{
    module: { uri, version: 7 }, sourceText: source, range: { start: 9, end: 13 },
    positionRange: { start: { line: 0, character: 9 }, end: { line: 0, character: 13 } }, replacement: "UserCard",
  }] } });
  f.vscode.Uri = { parse: () => ({ scheme: "file" }) };
  f.vscode.Position = class { constructor(line, character) { this.line = line; this.character = character; } };
  f.vscode.workspace.openTextDocument = async () => ({ uri: { scheme: "file", toString: () => uri },
    languageId: "python", version: 7, getText: () => source });
  const params = { textDocument: { uri }, position: { line: 0, character: 10 }, newName: "UserCard" };
  const plan = f.state.handlers.get("graphqlDesk/fragmentRenamePlan");
  const result = await plan(params);
  assert.equal(result.targets[0].version, 7);
  assert.equal(result.targets[0].text, source);
  assert.equal(result.targets[0].edits[0].newText, "UserCard");
  let release;
  let entered;
  const waiting = new Promise(resolve => { entered = resolve; });
  f.state.onRequest = entered;
  f.state.reply = new Promise(resolve => { release = resolve; });
  const pending = plan(params);
  await waiting;
  f.vscode.workspace.isTrusted = false;
  release(true);
  assert.equal(await pending, null);
  assert.equal(f.state.disposed, 1);
});

test("failed startup shuts down its child and disposes the output channel", async () => {
  const server = new EventEmitter();
  server.exitCode = null;
  server.signalCode = null;
  server.kill = () => {
    queueMicrotask(() => {
      server.exitCode = 1;
      server.emit("exit", 1, null);
      server.emit("close", 1, null);
    });
    return true;
  };
  let outputDisposed = false;
  let currentServer = server;

  await stopLanguageClient({
    get serverProcess() { return currentServer; },
    async dispose() {
      currentServer = undefined;
      throw new Error("Client is not running");
    },
  }, { dispose() { outputDisposed = true; } });

  assert.equal(server.exitCode, 1);
  assert.equal(outputDisposed, true);
});

test("shutdown keeps its output channel until late child exit logging and stdio close", async () => {
  const server = new EventEmitter();
  server.exitCode = null;
  server.signalCode = null;
  server.stdout = { closed: false };
  server.stderr = { closed: false };
  let disposed = false;
  let logged = false;
  server.on("exit", () => { assert.equal(disposed, false); logged = true; });
  const stopping = stopLanguageClient({ serverProcess: server, async dispose() {} },
    { dispose() { disposed = true; } });
  await Promise.resolve();
  assert.equal(disposed, false);
  server.exitCode = 0;
  server.emit("exit", 0, null);
  assert.equal(logged, true);
  assert.equal(disposed, false);
  server.stdout.closed = true;
  server.stderr.closed = true;
  server.emit("close", 0, null);
  await stopping;
  assert.equal(disposed, true);
  assert.equal(server.listenerCount("close"), 0);
});

test("an already exited child with closed streams does not wait for a past close event", async () => {
  const server = new EventEmitter();
  server.exitCode = 0;
  server.signalCode = null;
  server.stdout = { closed: true };
  server.stderr = { closed: true };
  let disposed = false;
  await stopLanguageClient({ serverProcess: server, async dispose() {} }, { dispose() { disposed = true; } });
  assert.equal(disposed, true);
  assert.equal(server.listenerCount("close"), 0);
});

for (const throws of [false, true]) {
  test(`failed child kill ${throws ? "throw" : "return"} reports pending cleanup until real close`, async () => {
    const server = new EventEmitter();
    server.exitCode = null;
    server.signalCode = null;
    server.kill = () => { if (throws) throw new Error("PRIVATE_NATIVE_FAILURE"); return false; };
    let disposed = 0;
    const messages = [];
    const stopping = stopLanguageClient({ serverProcess: server, async dispose() { throw new Error("PRIVATE_DISPOSE_FAILURE"); } },
      { error(message) { messages.push(message); }, dispose() { disposed += 1; } });
    await assert.rejects(stopping, /cleanup is pending/);
    assert.equal(disposed, 0);
    assert.deepEqual(messages, ["GraphQL fragment edit process could not be stopped; cleanup is pending"]);
    assert.equal(server.listenerCount("close"), 1);
    server.exitCode = 1;
    server.emit("close", 1, null);
    await Promise.resolve();
    assert.equal(disposed, 1);
    assert.equal(server.listenerCount("close"), 0);
    server.emit("close", 1, null);
    assert.equal(disposed, 1);
  });
}

test("successful shutdown still disposes the owned output channel", async () => {
  let clientDisposed = false;
  let outputDisposed = false;

  await stopLanguageClient({ async dispose() { clientDisposed = true; } },
    { dispose() { outputDisposed = true; } });

  assert.equal(clientDisposed, true);
  assert.equal(outputDisposed, true);
});
