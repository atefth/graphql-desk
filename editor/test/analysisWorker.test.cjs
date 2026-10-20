"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs/promises");
const os = require("node:os");
const path = require("node:path");
const { EventEmitter } = require("node:events");
const { PassThrough, Writable } = require("node:stream");
const { createAnalysisWorker, MAX_FRAME_BYTES } = require("../src/analysisWorker");

// Same caller-selected interpreter convention as the existing Python fixture helper.
const root = path.resolve(__dirname, "../..");
const python = process.env.GRAPHQL_DESK_PYTHON ?? path.join(root, ".venv", process.platform === "win32" ? "Scripts/python.exe" : "bin/python");
const message = workspace => ({ op: "prepare", workspace, candidates: [], buffers: [], documents: [], editor_fallback: null });
const reply = { ok: true, id: "job", local_schemas: [], required_buffers: [] };
const generic = { message: "GraphQL Desk worker request failed" };

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
