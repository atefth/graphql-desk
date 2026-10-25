"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs/promises");
const os = require("node:os");
const path = require("node:path");
const { spawn: spawnProcess } = require("node:child_process");
const { EventEmitter } = require("node:events");
const { PassThrough, Writable } = require("node:stream");
const { createAnalysisWorker, MAX_FRAME_BYTES } = require("../src/analysisWorker");

// Same caller-selected interpreter convention as the existing Python fixture helper.
const root = path.resolve(__dirname, "../..");
const python = process.env.GRAPHQL_DESK_PYTHON ?? path.join(root, ".venv", process.platform === "win32" ? "Scripts/python.exe" : "bin/python");
const message = workspace => ({ op: "prepare", workspace, candidates: [], buffers: [], documents: [], editor_fallback: null });
const reply = { ok: true, id: "job", local_schemas: [], remote_schemas: [], required_buffers: [], configuration_diagnostics: [] };
const generic = { message: "GraphQL Desk worker request failed" };

test("hung process times out and a fresh worker succeeds", async t => {
  const temporary = await fs.mkdtemp(path.join(os.tmpdir(), "graphql-desk-hung-worker-"));
  const workspace = path.join(temporary, "workspace");
  await fs.mkdir(workspace);
  let hung;
  let hungExit;
  const spawn = (command, args, options) => {
    if (command === python && !hung) {
      hung = spawnProcess(process.execPath, ["-e", "process.stdin.resume()"], options);
      hungExit = new Promise(resolve => hung.once("exit", (code, signal) => resolve({ code, signal })));
      return hung;
    }
    return spawnProcess(command, args, options);
  };
  const worker = createAnalysisWorker({ python, workspace, isTrusted: () => true, timeoutMs: 3000, spawn });
  t.after(async () => {
    worker.dispose();
    await fs.rm(temporary, { recursive: true, force: true });
  });

  await assert.rejects(worker.request(message(workspace)), generic);
  const exit = await hungExit;
  assert.ok(exit.code !== null || exit.signal !== null);
  const result = await worker.request(message(workspace));
  assert.equal(result.ok, true);
  assert.equal(typeof result.id, "string");
  assert.deepEqual(result.local_schemas, []);
  assert.deepEqual(result.remote_schemas, []);
  assert.deepEqual(result.required_buffers, []);
  assert.deepEqual(result.configuration_diagnostics, []);
});

test("prepare accepts only bounded file configuration diagnostics with UTF-16 positions", async t => {
  const diagnostic = { uri: "file:///workspace/pyproject.toml", version: 3, code: "graphql-desk.configuration",
    message: "Invalid GraphQL Desk project configuration", range: { start: { line: 1, character: 0 }, end: { line: 2, character: 5 } } };
  const fake = fakeSpawn(child => child.stdout.write(JSON.stringify({ ...reply, configuration_diagnostics: [diagnostic] }) + "\n"));
  const { worker, workspace } = setup(t, fake);
  const prepared = await worker.request(message(workspace));
  assert.deepEqual(prepared.configuration_diagnostics, [diagnostic]);

  const invalid = { ...diagnostic, code: "graphql-desk.configuration", range: { ...diagnostic.range, end: { line: 0, character: 0 } } };
  const malformed = fakeSpawn(child => child.stdout.write(JSON.stringify({ ...reply, configuration_diagnostics: [invalid] }) + "\n"));
  const fixture = setup(t, malformed);
  await assert.rejects(fixture.worker.request(message(fixture.workspace)), generic);
});

function fakeSpawn(onWrite = () => {}) {
  const children = [];
  const calls = [];
  const spawn = (...args) => {
    calls.push(args);
    const child = new EventEmitter();
    child.stdout = new PassThrough();
    child.stderr = new PassThrough();
    child.stdin = new Writable({ write(chunk, _encoding, done) { onWrite(child, chunk); done(); } });
    child.killed = false;
    child.kill = () => { child.killed = true; };
    children.push(child);
    return child;
  };
  return { spawn, children, calls };
}

function setup(t, fake, options = {}) {
  const workspace = path.join(os.tmpdir(), "graphql-desk-transport-fixture");
  const worker = createAnalysisWorker({ python, workspace, isTrusted: () => true, spawn: fake.spawn, ...options });
  t.after(() => worker.dispose());
  return { worker, workspace };
}

test("real isolated persistent worker uses Unicode dirty external schema and rejects old IDs after restart", async t => {
  const temporary = await fs.mkdtemp(path.join(os.tmpdir(), "graphql-desk-transport-"));
  t.after(() => fs.rm(temporary, { recursive: true, force: true }));
  const workspace = path.join(temporary, "workspace");
  await fs.mkdir(workspace);
  const schemaPath = path.join(temporary, "outside.graphql");
  const sourcePath = path.join(workspace, "main.py");
  const source = "raise RuntimeError('DO_NOT_EXECUTE')\nfrom graphql_desk import gql\nq=gql('{ value }')\n";
  await fs.writeFile(sourcePath, source, { flag: "wx" });
  await fs.writeFile(schemaPath, "type Query { disk: String }", { flag: "wx" });
  await fs.writeFile(path.join(workspace, "pyproject.toml"), '[tool.graphql-desk]\nschema="../outside.graphql"\nschema_output="graphql_desk_generated/default.pyi"\n', { flag: "wx" });
  const worker = createAnalysisWorker({ python, workspace, isTrusted: () => true });
  t.after(() => worker.dispose());
  const capture = { ...message(workspace), candidates: [sourcePath], documents: [{ id: "external", path: schemaPath, version: 2, is_dirty: true }] };
  const prepared = await worker.request(capture);
  assert.deepEqual(prepared.required_buffers, ["external"]);
  const schema = { path: schemaPath, version: 2, is_dirty: true, text: "# 😀 café\ntype Query { value: String }" };
  const request = { ...capture, id: prepared.id, schema_buffers: [schema] };
  const analyzed = await worker.request({ ...request, op: "analyze" });
  assert.equal(analyzed.request.schemas[0].text, schema.text);
  assert.equal(analyzed.request.schemas[0].version, 2);
  assert.equal(analyzed.response.calls.length, 1);
  assert.deepEqual(await worker.request({ ...request, op: "current" }), { ok: true, current: true });
  worker.invalidate();
  await assert.rejects(worker.request({ ...request, op: "current" }), generic);
  const refreshed = await worker.request(capture);
  assert.notEqual(refreshed.id, prepared.id);
  assert.equal(await fs.readFile(sourcePath, "utf8"), source);
  assert.equal(await fs.readFile(schemaPath, "utf8"), "type Query { disk: String }");
  worker.dispose();
});

test("exact launch flags, one active request and no queued concurrent work", async t => {
  const fake = fakeSpawn();
  const { worker, workspace } = setup(t, fake);
  const first = worker.request(message(workspace));
  await assert.rejects(worker.request(message(workspace)), generic);
  assert.equal(fake.calls.length, 1);
  assert.equal(fake.calls[0][0], python);
  assert.deepEqual(fake.calls[0][1], ["-I", "-X", "utf8", "-m", "graphql_desk.editor_worker"]);
  assert.equal(fake.calls[0][2].shell, false);
  assert.equal(fake.calls[0][2].windowsHide, true);
  assert.notEqual(fake.calls[0][2].cwd, workspace);
  fake.children[0].stdout.write(JSON.stringify(reply).slice(0, 8));
  fake.children[0].stdout.write(JSON.stringify(reply).slice(8) + "\n");
  assert.deepEqual(await first, reply);
  const unicode = worker.request(message(workspace));
  const bytes = Buffer.from(JSON.stringify({ ...reply, id: "😀" }) + "\n");
  const split = bytes.indexOf(Buffer.from("😀")) + 1;
  fake.children[0].stdout.write(bytes.subarray(0, split));
  fake.children[0].stdout.write(bytes.subarray(split));
  assert.equal((await unicode).id, "😀");
});

test("untrusted calls never spawn and disposal is final", async t => {
  const fake = fakeSpawn();
  const { worker, workspace } = setup(t, fake, { isTrusted: () => false });
  await assert.rejects(worker.request(message(workspace)), generic);
  assert.equal(fake.calls.length, 0);
  worker.close();
  await assert.rejects(worker.request(message(workspace)), generic);
  assert.equal(fake.calls.length, 0);
  assert.throws(() => createAnalysisWorker({ python: "relative", workspace, isTrusted: () => true }), generic);
});

test("abort and timeout kill active workers; subsequent request starts fresh", async t => {
  for (const kind of ["abort", "timeout"]) {
    const fake = fakeSpawn();
    const { worker, workspace } = setup(t, fake, { timeoutMs: 25 });
    const controller = new AbortController();
    const active = worker.request(message(workspace), { signal: controller.signal });
    const rejected = assert.rejects(active, generic);
    if (kind === "abort") controller.abort();
    await rejected;
    assert.equal(fake.children[0].killed, true);
    const next = worker.request(message(workspace));
    fake.children[1].stdout.write(JSON.stringify(reply) + "\n");
    assert.deepEqual(await next, reply);
  }
});

test("Windows teardown kills the interpreter process tree and falls back to the direct process", {
  skip: process.platform !== "win32",
}, async t => {
  for (const exitCode of [0, 1]) {
    const fake = fakeSpawn();
    const { worker, workspace } = setup(t, fake);
    const active = worker.request(message(workspace));
    fake.children[0].pid = 4321;
    worker.dispose();
    await assert.rejects(active, generic);
    assert.deepEqual(fake.calls[1].slice(0, 2), [path.join(process.env.SystemRoot, "System32", "taskkill.exe"), ["/PID", "4321", "/T", "/F"]]);
    fake.children[1].emit("exit", exitCode);
    assert.equal(fake.children[0].killed, exitCode !== 0, "failed tree termination falls back to killing the worker directly");
  }
});

test("final await rejects trust loss, disposal and abort after response arrival", async t => {
  for (const kind of ["trust", "dispose", "abort"]) {
    let trusted = true;
    const fake = fakeSpawn();
    const { worker, workspace } = setup(t, fake, { isTrusted: () => trusted });
    const controller = new AbortController();
    const active = worker.request(message(workspace), { signal: controller.signal });
    fake.children[0].stdout.write(JSON.stringify(reply) + "\n");
    if (kind === "trust") trusted = false;
    if (kind === "dispose") worker.dispose();
    if (kind === "abort") controller.abort();
    await assert.rejects(active, generic);
    assert.equal(fake.children[0].killed, true);
  }
});

test("malformed, duplicate, nonfinite, invalid UTF8 and oversized replies kill state without secrets", async t => {
  const faults = [
    "PRIVATE_MARKER\n", '{"ok":true,"ok":true,"id":"PRIVATE_MARKER","local_schemas":[],"required_buffers":[]}\n',
    '{"ok":true,"\\u006fk":true,"id":"PRIVATE_MARKER","local_schemas":[],"required_buffers":[]}\n',
    '{"ok":true,"id":"PRIVATE_MARKER","local_schemas":[],"required_buffers":[],"overflow":1e999}\n',
    Buffer.from([0xff, 10]), Buffer.alloc(MAX_FRAME_BYTES + 1, 32), JSON.stringify({ ...reply, extra: "PRIVATE_MARKER" }) + "\n",
    JSON.stringify(reply) + "\nPRIVATE_MARKER", JSON.stringify(reply) + "\n" + JSON.stringify(reply) + "\n",
  ];
  for (const fault of faults) {
    const fake = fakeSpawn();
    const { worker, workspace } = setup(t, fake);
    const active = worker.request(message(workspace));
    fake.children[0].stdout.write(fault);
    await assert.rejects(active, generic);
    assert.equal(fake.children[0].killed, true);
  }
});

test("stderr, process errors and unexpected exits are sanitized and force restart", async t => {
  for (const kind of ["stderr", "error", "exit", "stdin", "stdout"]) {
    const fake = fakeSpawn();
    const { worker, workspace } = setup(t, fake);
    const active = worker.request(message(workspace));
    if (kind === "stderr") fake.children[0].stderr.write("PRIVATE_MARKER");
    else if (kind === "exit") fake.children[0].emit("exit", 1);
    else (kind === "error" ? fake.children[0] : fake.children[0][kind]).emit("error", new Error("PRIVATE_MARKER"));
    await assert.rejects(active, generic);
    assert.equal(fake.children[0].killed, true);
    const next = worker.request(message(workspace));
    fake.children[1].stdout.write(JSON.stringify(reply) + "\n");
    assert.deepEqual(await next, reply);
  }
});

test("oversized request never spawns; mismatched analyze identity rejects", async t => {
  const fake = fakeSpawn();
  const { worker, workspace } = setup(t, fake);
  await assert.rejects(worker.request({ ...message(workspace), padding: "x".repeat(MAX_FRAME_BYTES) }), generic);
  assert.equal(fake.calls.length, 0);
  const active = worker.request({ ...message(workspace), op: "analyze", id: "expected", schema_buffers: [] });
  fake.children[0].stdout.write(JSON.stringify({ ok: true, id: "wrong", request: {}, response: {}, unavailable_schemas: [] }) + "\n");
  await assert.rejects(active, generic);
});

test("spawn failures are sanitized and late old-process events cannot invalidate a restart", async t => {
  const workspace = path.join(os.tmpdir(), "graphql-desk-transport-fixture");
  const broken = createAnalysisWorker({ python, workspace, isTrusted: () => true,
    spawn() { throw new Error("PRIVATE_MARKER"); } });
  t.after(() => broken.dispose());
  await assert.rejects(broken.request(message(workspace)), generic);
  const fake = fakeSpawn();
  const { worker } = setup(t, fake);
  const active = worker.request(message(workspace));
  const rejected = assert.rejects(active, generic);
  worker.invalidate();
  await rejected;
  const next = worker.request(message(workspace));
  fake.children[0].emit("exit", 1);
  fake.children[0].emit("error", new Error("PRIVATE_MARKER"));
  fake.children[1].stdout.write(JSON.stringify(reply) + "\n");
  assert.deepEqual(await next, reply);
});

test("temporary-directory workspace falls back to an outside filesystem-root cwd", async t => {
  const workspace = os.tmpdir();
  const fake = fakeSpawn();
  const worker = createAnalysisWorker({ python, workspace, isTrusted: () => true, spawn: fake.spawn });
  t.after(() => worker.dispose());
  const active = worker.request(message(workspace));
  const cwd = fake.calls[0][2].cwd;
  assert.equal(cwd, path.parse(path.resolve(workspace)).root);
  const relative = path.relative(path.resolve(workspace), cwd);
  assert.ok(relative === ".." || relative.startsWith(`..${path.sep}`) || path.isAbsolute(relative));
  assert.deepEqual(fake.calls[0][1], ["-I", "-X", "utf8", "-m", "graphql_desk.editor_worker"]);
  fake.children[0].stdout.write(JSON.stringify(reply) + "\n");
  assert.deepEqual(await active, reply);
});
const http = require("node:http");
const gql = require("graphql");

test("explicit remote refresh reaches real HTTP and analysis; aborted refresh cannot survive restart", async t => {
  const temporary = await fs.mkdtemp(path.join(os.tmpdir(), "graphql-desk-remote-"));
  t.after(() => fs.rm(temporary, { recursive: true, force: true }));
  const workspace = path.join(temporary, "workspace");
  await fs.mkdir(workspace);
  const source = path.join(workspace, "main.py");
  await fs.writeFile(source, "from graphql_desk import gql\nq=gql('{value}')\n");
  const schema = gql.buildSchema("type Query { value: String }");
  let hits = 0;
  let slow = false;
  let contacted;
  const server = http.createServer(async (req, res) => {
    const chunks = [];
    for await (const chunk of req) chunks.push(chunk);
    hits += 1;
    assert.equal(req.headers["x-key"], "PRIVATE_MARKER");
    const body = JSON.parse(Buffer.concat(chunks).toString("utf8"));
    if (slow) {
      contacted?.();
      await new Promise(resolve => setTimeout(resolve, 100));
    }
    const result = gql.graphqlSync({ schema, source: body.query });
    res.writeHead(200, { "Content-Type": "application/json" });
    res.end(JSON.stringify(result));
  });
  await new Promise(resolve => server.listen(0, "127.0.0.1", resolve));
  t.after(() => { server.closeAllConnections(); server.close(); });
  const config = path.join(workspace, "pyproject.toml");
  await fs.writeFile(config, `[tool.graphql-desk]\nschema={url="http://127.0.0.1:${server.address().port}/PRIVATE_MARKER",headers={"X-Key"="\${env:TOKEN}"}}\nschema_output="graphql_desk_generated/default.pyi"\n`);
  const worker = createAnalysisWorker({ python, workspace, isTrusted: () => true });
  t.after(() => worker.dispose());
  const capture = { ...message(workspace), candidates: [source] };
  const prepared = await worker.request(capture);
  assert.equal(hits, 0);
  assert.equal(prepared.remote_schemas.length, 1);
  const target = prepared.remote_schemas[0];
  const refresh = { ...capture, op: "refresh", id: prepared.id, schema_buffers: [], ...target,
    credential_revision: "credentials-1", environment: { TOKEN: "PRIVATE_MARKER" } };
  const loaded = await worker.request(refresh);
  assert.equal(loaded.status, "ready");
  assert.notEqual(loaded.id, prepared.id);
  assert.equal(hits, 2);
  assert.equal(JSON.stringify(loaded).includes("PRIVATE_MARKER"), false);
  const analyzedRequest = { ...capture, op: "analyze", id: loaded.id, schema_buffers: [],
    remote_revisions: [{ ...target, revision: "credentials-1" }] };
  const analyzed = await worker.request(analyzedRequest);
  assert.equal(analyzed.request.schemas.length, 1);
  assert.equal(analyzed.response.schemas.length, 1);
  assert.equal(analyzed.response.calls.length, 1);
  const artifacts = await worker.request({ ...analyzedRequest, op: "artifacts" });
  assert.match(artifacts.input_fingerprint, /^[a-f0-9]{64}$/);
  assert.equal(JSON.stringify(artifacts).includes("PRIVATE_MARKER"), false);
  assert.deepEqual(await worker.request({ ...analyzedRequest, op: "current" }), { ok: true, current: true });
  assert.equal(hits, 2);
  slow = true;
  const contactedPromise = new Promise(resolve => { contacted = resolve; });
  const abort = new AbortController();
  const active = worker.request({ ...refresh, id: loaded.id }, { signal: abort.signal });
  const rejected = assert.rejects(active, generic);
  await contactedPromise;
  abort.abort();
  await rejected;
  slow = false;
  const restarted = await worker.request(capture);
  assert.notEqual(restarted.id, loaded.id);
  await new Promise(resolve => setTimeout(resolve, 125));
  const fresh = await worker.request({ ...refresh, id: restarted.id });
  assert.equal(fresh.status, "ready");
  await assert.rejects(worker.request({ ...analyzedRequest, op: "current" }), generic);
});
// A late refresh reply cannot pass the final trust check or pollute a restart.
test("refresh replies are redacted and guarded after the await", async t => {
  let trusted = true;
  const fake = fakeSpawn();
  const { worker, workspace } = setup(t, fake, { isTrusted: () => trusted });
  const request = { ...message(workspace), op: "refresh", id: "previous", schema_buffers: [],
    project: "project", schema_name: "default", credential_revision: "revision", environment: { TOKEN: "PRIVATE_MARKER" } };
  const refreshed = { ok: true, id: "next", project: "project", schema_name: "default",
    status: "ready", last_good: false, error: null };
  const active = worker.request(request);
  fake.children[0].stdout.write(JSON.stringify(refreshed) + "\n");
  trusted = false;
  await assert.rejects(active, generic);
  assert.equal(fake.children[0].killed, true);
  trusted = true;
  const next = worker.request(message(workspace));
  fake.children[0].stdout.emit("data", Buffer.from(JSON.stringify(refreshed) + "\n"));
  fake.children[1].stdout.write(JSON.stringify(reply) + "\n");
  assert.deepEqual(await next, reply);
  const leaked = worker.request(request);
  fake.children[1].stdout.write(JSON.stringify({ ...refreshed, environment: { TOKEN: "PRIVATE_MARKER" } }) + "\n");
  await assert.rejects(leaked, generic);
});

test("extended preparation validates redacted preferences, environment names and opaque active revisions", async t => {
  const extended = { ...reply, project_preferences: [{ project: "p", client_directives: ["local"] }],
    remote_schemas: [{ project: "p", schema_name: "default", environment_names: ["TOKEN"], refresh_interval: 0 }],
    remote_revisions: [{ project: "p", schema_name: "default", revision: "opaque" }] };
  for (const malformed of [null, { ...extended, project_preferences: [{ project: "p", client_directives: "local" }] },
    { ...extended, remote_schemas: [{ project: "p", schema_name: "default", environment_names: ["TOKEN", "TOKEN"], refresh_interval: 0 }] },
    ...[-1, "2", null, true].map(refresh_interval => ({ ...extended,
      remote_schemas: [{ project: "p", schema_name: "default", environment_names: [], refresh_interval }] })),
    { ...extended, remote_revisions: [{ project: "p", schema_name: "default", revision: 1 }] }]) {
    const fake = fakeSpawn((child) => child.stdout.write(JSON.stringify(malformed ?? extended) + "\n"));
    const { worker, workspace } = setup(t, fake);
    const pending = worker.request({ ...message(workspace), remote_revisions: [] });
    if (malformed === null) assert.deepEqual(await pending, extended);
    else await assert.rejects(pending, generic);
  }
});

test("analysis artifacts require captured live and saved-source fingerprints", async t => {
  const valid = { ok: true, id: "job", input_fingerprint: "a".repeat(64), saved_source_fingerprint: "b".repeat(64), projects: [],
    parser_overlay: [{ relative_path: "graphql_desk-stubs/__init__.pyi", text: "from graphql import DocumentNode as _DocumentNode, Source as _Source\n" },
      { relative_path: "graphql_desk-stubs/py.typed", text: "partial\n" }], unavailable_schemas: [] };
  for (const reply of [valid, { ...valid, input_fingerprint: "private" }, { ...valid, saved_source_fingerprint: "private" }]) {
    const fake = fakeSpawn(child => child.stdout.write(JSON.stringify(reply) + "\n"));
    const { worker, workspace } = setup(t, fake);
    const request = { ...message(workspace), op: "artifacts", id: "job", schema_buffers: [] };
    if (reply.input_fingerprint === "private" || reply.saved_source_fingerprint === "private") await assert.rejects(worker.request(request), generic);
    else assert.deepEqual(await worker.request(request), valid);
  }
});

test("analysis artifacts validate operation provenance at the worker boundary", async t => {
  const module = { project: "p", module: "main", uri: "file:///main.py", version: 1 };
  const identity = { module, codepoint: { start: 0, end: 8 }, utf16: { start: 0, end: 8 } };
  const record = { owner: identity, selector: "query Q { x }", operation_name: "Q", operation_kind: "query",
    schema_identity: ["p", "default"], context_token: "schema-token", binding: "graphql", status: "precise",
    declaration_path: "graphql_desk_generated/_documents/default/d0.pyi", declaration_symbol: "Operation_Q", source_exports: [],
    dependencies: [module], direct_fragments: [], definition_owners: [identity], canonical_owner: identity,
    selector_known: true, source_dependencies_known: true, mapping_dependencies: [],
    schema_dependencies: [{ coordinate: "field:Query.id", fingerprint: "a".repeat(64) }],
    config_dependencies: [{ kind: "scalar-input", name: "ID", value: null }] };
  const project = { project: "p", generated_package: "graphql_desk_generated", files: [
    { relative_path: "graphql_desk_generated/_documents/default/d0.pyi", text: "" },
  ], diagnostics: [], provenance: [record] };
  const valid = { ok: true, id: "job", input_fingerprint: "a".repeat(64), saved_source_fingerprint: "b".repeat(64), projects: [project],
    parser_overlay: [{ relative_path: "graphql_desk-stubs/__init__.pyi", text: "from graphql import DocumentNode as _DocumentNode, Source as _Source\n" },
      { relative_path: "graphql_desk-stubs/py.typed", text: "partial\n" }], unavailable_schemas: [] };
  const malformed = [
    { ...record, owner: { ...identity, utf16: { start: 9, end: 8 } } },
    { ...record, declaration_path: "../outside.pyi" },
    { ...record, dependencies: [{ ...module, version: -1 }] },
    { ...record, dependencies: [{ ...module, project: "q" }] },
    { ...record, direct_fragments: [{ ...identity, module: { ...module, project: "q" } }] },
    { ...record, definition_owners: [{ ...identity, codepoint: { start: 9, end: 10 }, utf16: { start: 9, end: 10 } }] },
    { ...record, declaration_path: "graphql_desk_generated/_documents/missing.pyi" },
    { ...record, source_exports: [{ relative_path: "graphql_desk_generated/sources/main.pyi", symbol: "Doc", alias: "Doc" }] },
    { ...record, schema_dependencies: [{ coordinate: "User.name", fingerprint: "unknown" }] },
    { ...record, schema_dependencies: [{ coordinate: "User.name", fingerprint: "a".repeat(64) }, { coordinate: "User.name", fingerprint: "b".repeat(64) }] },
    { ...record, schema_dependencies: [{ coordinate: "User.name", fingerprint: "b".repeat(64) }, { coordinate: "User.id", fingerprint: "a".repeat(64) }] },
    { ...record, config_dependencies: [{ kind: "scalar-input", name: "Date", value: 1 }] },
    { ...record, config_dependencies: [{ kind: "enum", name: "Color", value: "app.Color" }, { kind: "enum", name: "Color", value: "app.OtherColor" }] },
    { ...record, unexpected: true },
  ];
  for (const reply of [valid, ...malformed.map(provenance => ({ ...valid,
    projects: [{ ...project, provenance: [provenance] }] }))]) {
    const fake = fakeSpawn(child => child.stdout.write(JSON.stringify(reply) + "\n"));
    const { worker, workspace } = setup(t, fake);
    const request = { ...message(workspace), op: "artifacts", id: "job", schema_buffers: [] };
    if (reply === valid) assert.deepEqual(await worker.request(request), valid);
    else await assert.rejects(worker.request(request), generic);
  }
});

test("parser overlay accepts only the fixed PEP 561 partial package paths", async t => {
  const valid = { ok: true, id: "job", input_fingerprint: "a".repeat(64), saved_source_fingerprint: "b".repeat(64), projects: [],
    parser_overlay: [{ relative_path: "graphql_desk-stubs/__init__.pyi", text: "from graphql import DocumentNode as _DocumentNode\n" },
      { relative_path: "graphql_desk-stubs/py.typed", text: "partial\n" }], unavailable_schemas: [] };
  for (const value of [
    [{ relative_path: "other/__init__.pyi", text: "" }, valid.parser_overlay[1]],
    [valid.parser_overlay[0], { relative_path: "graphql_desk-stubs/extra.pyi", text: "" }],
  ]) {
    const fake = fakeSpawn(child => child.stdout.write(JSON.stringify({ ...valid, parser_overlay: value }) + "\n"));
    const { worker, workspace } = setup(t, fake);
    await assert.rejects(worker.request({ ...message(workspace), op: "artifacts", id: "job", schema_buffers: [] }), generic);
  }
});
