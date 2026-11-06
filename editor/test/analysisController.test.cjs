"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs/promises");
const { realpathSync } = require("node:fs");
const path = require("node:path");
const os = require("node:os");
const { createHash } = require("node:crypto");
const http = require("node:http");
const { pathToFileURL, fileURLToPath } = require("node:url");
const { buildSchema, introspectionFromSchema } = require("graphql");
const { createAnalysisController } = require("../src/analysisController");
const { createAnalysisWorker } = require("../src/analysisWorker");
const { acknowledgePendingConfigWrite, stateKey: projectConfigStateKey } = require("../src/projectPyrightConfig");
const { createLanguageFeatures } = require("../src/languageFeatures");
const { createHighlighting } = require("../src/highlighting");
const { createOwnedAnalysisArtifacts, renderOwnedAnalysisArtifact } = require("../src/ownedAnalysisArtifacts");
const python = process.env.GRAPHQL_DESK_PYTHON ?? path.resolve(__dirname, "../../.venv", process.platform === "win32" ? "Scripts/python.exe" : "bin/python");
const realTimeout = setTimeout;

async function eventually(accept) {
  for (let attempt = 0; attempt < 500; attempt += 1) {
    if (accept()) return;
    await new Promise(resolve => realTimeout(resolve, 10));
  }
  assert.fail("Polling did not settle");
}

async function configRequestStarted(started, request) {
  await Promise.race([started, request.then(result => {
    if (result.error) throw result.error;
    assert.fail("Config request completed before worker dispatch");
  })]);
}

test("config dispatch waits report request errors instead of waiting for an impossible start", async () => {
  const pending = new Promise(() => {});
  const failure = new Error("request failed before dispatch");
  await assert.rejects(configRequestStarted(pending, Promise.resolve({ error: failure })), error => error === failure);
  await assert.rejects(configRequestStarted(pending, Promise.reject(failure)), error => error === failure);
});

function uri(value) {
  const parsed = new URL(value);
  return { scheme: parsed.protocol.slice(0, -1), fsPath: fileURLToPath(new URL(value.replace(/^untitled:/, "file:"))), toString: () => value };
}
const Uri = { file: value => uri(pathToFileURL(value).href), parse: uri };
function pythonApi(fixture, options = {}) {
  let selected = { id: "selected", path: path.dirname(path.dirname(python)) };
  const changes = event();
  const stats = { activations: 0, resolutions: 0, resources: [], disposed: 0 };
  const environments = {
    getActiveEnvironmentPath(resource) { stats.resources.push(resource.toString()); return selected; },
    resolveEnvironment(record) { stats.resolutions += 1; assert.deepEqual(record, selected); return options.resolve ? options.resolve(record) : Promise.resolve({ executable: { uri: Uri.file(python) } }); },
    onDidChangeActiveEnvironmentPath(listener) { const handle = changes.subscribe(listener); return { dispose() { stats.disposed += 1; handle.dispose(); } }; },
  };
  const api = { environments };
  fixture.vscode.extensions = { getExtension(id) { assert.equal(id, "ms-python.python"); return { activate() { stats.activations += 1; return options.activate ? options.activate(api) : Promise.resolve(api); } }; } };
  return { stats, api, changes, set(record) { selected = record; }, select(record, resource = fixture.vscode.workspace.workspaceFolders[0]) { selected = record; changes.fire({ path: record.path, resource }); } };
}
function event() {
  const listeners = new Set();
  return { subscribe(listener) { listeners.add(listener); return { dispose: () => listeners.delete(listener) }; }, fire(value) { for (const listener of listeners) listener(value); } };
}
async function setup(t, config = 'schema="schema.graphql"\n', { deferCleanup = false } = {}) {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), "graphql-desk-controller-"));
  const cleanup = () => fs.rm(root, { recursive: true, force: true, maxRetries: 8, retryDelay: 250 });
  if (!deferCleanup) t.after(cleanup);
  await fs.writeFile(path.join(root, "schema.graphql"), "type Query { title: String }");
  await fs.writeFile(path.join(root, "pyproject.toml"), `[tool.graphql-desk]\n${config}schema_output="graphql_desk_generated/default.pyi"\n`);
  const source = 'raise RuntimeError("NO_IMPORT")\nfrom graphql_desk import gql\nq=gql("{title}")\n';
  await fs.writeFile(path.join(root, "main.py"), source);
  let candidates = [Uri.file(path.join(root, "main.py"))];
  const folder = { uri: Uri.file(root) };
  const events = {};
  const watchers = [];
  let selectedPython = python;
  let selectedStubPath = "";
  const defaultStubPath = "./typings";
  let explicitlyConfiguredStubPath = false;
  const workspace = { isTrusted: true, workspaceFolders: [folder], textDocuments: [],
    getWorkspaceFolder: value => workspace.workspaceFolders.includes(folder) && value.fsPath.toLowerCase().startsWith(root.toLowerCase()) ? folder : undefined,
    getConfiguration: section => ({
      get: (name, fallback) => section === "python" && name === "analysis.stubPath" ? (explicitlyConfiguredStubPath ? selectedStubPath : defaultStubPath) : name === "pythonPath" ? selectedPython : fallback,
      inspect: name => section === "python" && name === "analysis.stubPath" && explicitlyConfiguredStubPath ? { workspaceValue: selectedStubPath } : undefined,
    }),
    findFiles: async (_pattern, exclude) => { assert.equal(exclude, null); return candidates; },
    createFileSystemWatcher: pattern => { const e = event(); const watcher = { pattern, disposed: false, disposals: 0, onDidChange: e.subscribe, onDidCreate: e.subscribe, onDidDelete: e.subscribe, dispose() { this.disposed = true; this.disposals += 1; }, fire: e.fire }; watchers.push(watcher); return watcher; },
  };
  for (const name of ["onDidOpenTextDocument", "onDidChangeTextDocument", "onDidCloseTextDocument", "onDidSaveTextDocument", "onDidChangeConfiguration", "onDidChangeWorkspaceFolders", "onDidGrantWorkspaceTrust"]) {
    events[name] = event(); workspace[name] = events[name].subscribe;
  }
  const vscode = { Uri, workspace, RelativePattern: class { constructor(base, pattern) { this.base = base; this.pattern = pattern; } }, window: { showQuickPick: async choices => choices[0] } };
  function document(name, text = source, scheme = "file") {
    const value = { uri: scheme === "file" ? Uri.file(path.join(root, name)) : uri(pathToFileURL(path.join(root, name)).href.replace(/^file:/, "untitled:")),
      version: 1, isDirty: scheme !== "file", languageId: "python", getText: () => value.text, text };
    workspace.textDocuments.push(value);
    return value;
  }
  const main = document("main.py");
  vscode.window.activeTextEditor = { document: main };
  return { root, vscode, main, document, events, watchers, cleanup, interpreter: value => { selectedPython = value; },
    stubPath: value => { selectedStubPath = value; explicitlyConfiguredStubPath = true; }, candidates: values => { candidates = values.map(value => Uri.file(path.join(root, value))); } };
}

test("real worker captures unsaved source/config/schema, shares concurrent providers and maps associated untitled identities", async t => {
  const fixture = await setup(t);
  const { vscode, main, document, root } = fixture;
  const messages = [];
  const controller = createAnalysisController(vscode, { workerFactory: options => {
    const worker = createAnalysisWorker(options);
    return { ...worker, request(message) { messages.push(message); return worker.request(message); } };
  } });
  t.after(() => controller.dispose());
  const schema = document("schema.graphql", "type Query { buffered: Int }"); schema.isDirty = true;
  document("pyproject.toml", '[tool.graphql-desk]\nschema="schema.graphql"\nclient_directives=["local"]\nschema_output="graphql_desk_generated/default.pyi"\n');
  main.text = 'from graphql_desk import gql\nq=gql("{buffered @local}")\n'; main.version += 1; main.isDirty = true;
  const unsaved = document("new.py", 'from graphql_desk import gql\nq=gql("{buffered}")\n', "untitled");
  const snapshot = controller.snapshot(unsaved);
  const [first, second] = await Promise.all([controller.analyze(snapshot), controller.analyze(snapshot)]);
  assert.ok(first); assert.equal(first, second);
  assert.equal(messages.filter(message => message.op === "analyze").length, 1);
  assert.equal(first.request.schemas[0].text, schema.text);
  assert.equal(first.request.modules.find(item => item.uri === controller.mapSnapshot(snapshot, first).uri).source, unsaved.text);
  assert.equal(controller.resolveUri(snapshot.uri).toString(), unsaved.uri.toString());
  assert.equal(await controller.isCurrent(first, snapshot), true);
  assert.deepEqual(controller.clientDirectives(first, first.response.calls[0]), ["local"]);
  assert.equal(await fs.readFile(path.join(root, "schema.graphql"), "utf8"), "type Query { title: String }");
  assert.equal(unsaved.isDirty, true);
  await assert.rejects(fs.stat(path.join(root, "new.py")), { code: "ENOENT" });
});

test("TOML Pyright ownership maps across the controller, worker protocol and Python worker", async t => {
  const fixture = await setup(t);
  pythonApi(fixture);
  const controller = createAnalysisController(fixture.vscode);
  t.after(() => controller.dispose());
  const text = '[tool.pyright]\nextraPaths = ["vendor"]\n[[tool.pyright.executionEnvironments]]\nroot = "src"\n';
  const reply = await controller.editPyrightConfig(fixture.vscode.workspace.workspaceFolders[0], text, "add",
    ".graphql-desk/live-typings", { topLevel: false, executionEnvironmentRoots: ["src"] });
  assert.equal(reply.ok, true);
  assert.deepEqual(reply.extra_paths, ["vendor", ".graphql-desk/live-typings"]);
  assert.deepEqual(reply.ownership, { top_level: false, execution_environment_roots: ["src"] });
  assert.deepEqual(reply.execution_environments[0].extra_paths, [".graphql-desk/live-typings"]);
});

for (const revokeTrust of [false, true]) {
  test(`native config actions serialize without coalescing${revokeTrust ? " and reject a revoked workspace" : ""}`, async t => {
    const fixture = await setup(t);
    let release;
    const held = new Promise(resolve => { release = resolve; });
    let started;
    const firstStarted = new Promise(resolve => { started = resolve; });
    const requests = [];
    let busy = false;
    const controller = createAnalysisController(fixture.vscode, { workerFactory: options => {
      assert.equal(options.configFiles, true);
      return { invalidate() {}, dispose() {}, async request(request) {
        assert.equal(busy, false);
        busy = true;
        try {
          assert.equal(request.op, "config_file");
          const expectedRoot = realpathSync.native(fixture.root);
          assert.equal(request.workspace, process.platform === "win32" ? expectedRoot.toLowerCase() : expectedRoot);
          requests.push(request);
          if (requests.length === 1) { started(); await held; }
          return { ok: true, token: request.token };
        } finally { busy = false; }
      } };
    } });
    t.after(() => { release(); controller.dispose(); });
    const folder = fixture.vscode.workspace.workspaceFolders[0];
    const first = controller.configFile(folder, "verify", { token: "a".repeat(32) })
      .then(value => ({ value }), error => ({ error }));
    await configRequestStarted(firstStarted, first);
    const second = controller.configFile(folder, "verify", { token: "b".repeat(32) })
      .then(value => ({ value }), error => ({ error }));
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(requests.length, 1);
    if (revokeTrust) fixture.vscode.workspace.isTrusted = false;
    release();
    const results = await Promise.all([first, second]);
    if (revokeTrust) {
      assert.equal(requests.length, 1);
      assert.ok(results.every(result => result.error));
    } else {
      assert.deepEqual(results.map(result => result.value.token), ["a".repeat(32), "b".repeat(32)]);
      assert.equal(requests.length, 2);
    }
  });
}

test("acknowledging a config record disposes the real controller's occupied workspace worker", async t => {
  const fixture = await setup(t);
  const folder = fixture.vscode.workspace.workspaceFolders[0];
  const workers = [];
  const controller = createAnalysisController(fixture.vscode, { workerFactory: () => {
    const worker = { occupied: false, disposed: false, invalidate() {}, dispose() { this.disposed = true; }, async request() {
      if (this.occupied) throw new Error("stage already needs recovery");
      this.occupied = true;
      return { ok: true, token: "a".repeat(32) };
    } };
    workers.push(worker);
    return worker;
  } });
  t.after(() => controller.dispose());
  const temporary = path.join(fixture.root, ".graphql-desk-aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee.tmp");
  const file = path.join(fixture.root, "pyproject.toml");
  const original = await fs.readFile(file);
  await fs.writeFile(temporary, "preserved uncertain stage");
  const request = { file: "pyproject.toml", temporary, expected_hash: createHash("sha256").update(original).digest("hex"), text: "staged" };
  await controller.configFile(folder, "stage", request);
  controller.invalidate();
  await assert.rejects(controller.configFile(folder, "stage", request), /needs recovery/);
  assert.equal(workers[0].disposed, false, "ordinary trusted invalidation is not a worker reset");
  const key = `${projectConfigStateKey(fixture.root)}:pending-config-write`;
  const values = new Map([[key, { version: 2, file: request.file, temporaryPath: temporary,
    hash: "b".repeat(64), previousHash: request.expected_hash, receipt: null, token: null }]]);
  const state = { get: name => values.get(name), update: async (name, value) => values.set(name, value) };
  fixture.vscode.window.showWarningMessage = async (_message, _options, confirm) => confirm;
  assert.equal(await acknowledgePendingConfigWrite(fixture.vscode, folder, state, () => controller.resetConfigWorkspace(folder)), true);
  assert.equal(workers[0].disposed, true);
  assert.equal(state.get(key), undefined);
  await controller.configFile(folder, "stage", request);
  assert.equal(workers.length, 2);
  assert.equal(await fs.readFile(temporary, "utf8"), "preserved uncertain stage");
  assert.deepEqual(await fs.readFile(file), original);
});

for (const scenario of ["success", "failure", "unreadable"]) {
  test(`only an actively dispatched config stage ignores its incomplete temp bytes: ${scenario}`, async t => {
    const rejectStage = scenario === "failure";
    const fixture = await setup(t);
    t.mock.timers.enable({ apis: ["setTimeout"] });
    const file = path.join(fixture.root, "pyproject.toml");
    const temporary = path.join(fixture.root, ".graphql-desk-aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee.tmp");
    const text = '[tool.pyright]\nstubPath="managed"\n';
    const hash = value => createHash("sha256").update(value).digest("hex");
    const previousHash = hash(await fs.readFile(file));
    let invalidations = 0;
    let started;
    const firstStarted = new Promise(resolve => { started = resolve; });
    let release;
    const held = new Promise(resolve => { release = resolve; });
    const controller = createAnalysisController(fixture.vscode, { changed() { invalidations += 1; },
      workerFactory: () => ({ invalidate() {}, dispose() {}, async request(request) {
        assert.equal(request.action, "stage");
        await fs.writeFile(temporary, "");
        started();
        await held;
        if (rejectStage) throw new Error("stage rejected");
        await fs.writeFile(temporary, text);
        return { ok: true, token: "a".repeat(32) };
      } }) });
    t.after(() => { release(); controller.dispose(); });
    const watcher = fixture.watchers.find(item => item.pattern.pattern === "**/*");
    assert.equal(controller.trackOwnedProjectConfigWrite(file, hash(text), { phase: "begin", previousHash, temporaryPath: temporary }), true);
    const stage = controller.configFile(fixture.vscode.workspace.workspaceFolders[0], "stage",
      { file: "pyproject.toml", temporary, expected_hash: previousHash, text })
      .then(value => ({ value }), error => ({ error }));
    await configRequestStarted(firstStarted, stage);
    watcher.fire(Uri.file(temporary));
    assert.equal(invalidations, 0, "secured empty stage is not a source change");
    t.mock.timers.tick(6000);
    await fs.writeFile(temporary, text.slice(0, 7));
    watcher.fire(Uri.file(temporary));
    assert.equal(invalidations, 0, "the active RPC outlives the ordinary notification grace timer");
    if (scenario === "unreadable") {
      const fsSync = require("node:fs");
      const originalRead = fsSync.readFileSync;
      const blockedRead = t.mock.method(fsSync, "readFileSync", function (filename, ...args) {
        if (path.resolve(filename).toLowerCase() === temporary.toLowerCase()) throw Object.assign(new Error("unreadable"), { code: "EACCES" });
        return originalRead.call(this, filename, ...args);
      });
      try { watcher.fire(Uri.file(temporary)); }
      finally { blockedRead.mock.restore(); }
      assert.equal(invalidations, 1, "staging never exempts an unreadable temporary");
      release();
      await stage;
      return;
    }
    release();
    const result = await stage;
    assert.equal(Boolean(result.error), rejectStage);
    if (!rejectStage) {
      watcher.fire(Uri.file(temporary));
      assert.equal(invalidations, 0, "completed matching bytes retain the existing exemption");
    }
    await fs.writeFile(temporary, "foreign replacement");
    watcher.fire(Uri.file(temporary));
    assert.equal(invalidations, 1, "success or failure immediately restores strict hash checking");
  });
}

test("a queued config stage cannot exempt a temporary path before worker dispatch", async t => {
  const fixture = await setup(t);
  const file = path.join(fixture.root, "pyproject.toml");
  const temporary = path.join(fixture.root, ".graphql-desk-aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee.tmp");
  const hash = value => createHash("sha256").update(value).digest("hex");
  const previousHash = hash(await fs.readFile(file));
  let release;
  const held = new Promise(resolve => { release = resolve; });
  let started;
  const firstStarted = new Promise(resolve => { started = resolve; });
  let invalidations = 0;
  let calls = 0;
  const controller = createAnalysisController(fixture.vscode, { changed() { invalidations += 1; },
    workerFactory: () => ({ invalidate() {}, dispose() {}, async request(request) {
      calls += 1; started(); await held; return { ok: true, token: request.token };
    } }) });
  t.after(() => { release(); controller.dispose(); });
  const folder = fixture.vscode.workspace.workspaceFolders[0];
  const first = controller.configFile(folder, "verify", { token: "a".repeat(32) })
    .then(value => ({ value }), error => ({ error }));
  await configRequestStarted(firstStarted, first);
  assert.equal(controller.trackOwnedProjectConfigWrite(file, hash("next"), { phase: "begin", previousHash, temporaryPath: temporary }), true);
  const queued = controller.configFile(folder, "stage", { file: "pyproject.toml", temporary, expected_hash: previousHash, text: "next" }).catch(() => null);
  await new Promise(resolve => setImmediate(resolve));
  await fs.writeFile(temporary, "");
  fixture.watchers.find(item => item.pattern.pattern === "**/*").fire(Uri.file(temporary));
  assert.equal(invalidations, 1);
  fixture.vscode.workspace.isTrusted = false;
  release();
  await Promise.all([first, queued]);
  assert.equal(calls, 1);
});

test("configuration diagnostics follow the current dirty pyproject and clear after repair", async t => {
  const fixture = await setup(t);
  const reports = [];
  const controller = createAnalysisController(fixture.vscode, {
    configurationDiagnostics: (workspace, diagnostics) => reports.push({ workspace, diagnostics }),
  });
  t.after(() => controller.dispose());
  const invalid = '[tool.graphql-desk]\nschema=\nschema_output="graphql_desk_generated/default.pyi"\n';
  const config = fixture.document("pyproject.toml", invalid);
  config.isDirty = true;
  config.version += 1;
  fixture.events.onDidChangeTextDocument.fire({ document: config, contentChanges: [{ rangeOffset: 0, rangeLength: 1 }] });
  await controller.analyze(controller.snapshot(fixture.main));
  const failure = reports.at(-1);
  assert.equal(failure.workspace, fixture.vscode.workspace.workspaceFolders[0].uri.toString());
  assert.equal(failure.diagnostics.length, 1);
  assert.equal(
    await fs.realpath(fileURLToPath(failure.diagnostics[0].uri)),
    await fs.realpath(config.uri.fsPath),
  );
  assert.equal(failure.diagnostics[0].version, config.version);
  assert.equal(failure.diagnostics[0].code, "graphql-desk.configuration");
  assert.deepEqual(failure.diagnostics[0].range.start, { line: 1, character: 7 });

  config.text = '[tool.graphql-desk]\nschema="schema.graphql"\nschema_output="graphql_desk_generated/default.pyi"\n';
  config.version += 1;
  fixture.events.onDidChangeTextDocument.fire({ document: config, contentChanges: [{ rangeOffset: 0, rangeLength: invalid.length }] });
  await controller.analyze(controller.snapshot(fixture.main));
  assert.deepEqual(reports.at(-1).diagnostics, []);
});

test("managed runtime companions are excluded from project source candidates", async t => {
  const fixture = await setup(t);
  const generation = path.join(fixture.root, ".graphql-desk", "typings", "generations", "g-1-01234567-89ab-cdef-0123-456789abcdef");
  const generated = path.join(generation, "graphql_desk_generated", "default.py");
  const previousGeneration = path.join(path.dirname(generation), "g-0-01234567-89ab-cdef-0123-456789abcdef");
  const previousGenerated = path.join(previousGeneration, "graphql_desk_generated", "previous.py");
  await fs.mkdir(path.dirname(generated), { recursive: true });
  await fs.mkdir(path.dirname(previousGenerated), { recursive: true });
  await fs.writeFile(generated, renderOwnedAnalysisArtifact("graphql_desk_generated", "default.py", ""));
  await fs.writeFile(previousGenerated, renderOwnedAnalysisArtifact("graphql_desk_generated", "previous.py", "value = 1\n"));
  fixture.stubPath(path.relative(fixture.root, generation));
  fixture.candidates(["main.py", path.relative(fixture.root, generated), path.relative(fixture.root, previousGenerated)]);
  const requests = [];
  const controller = createAnalysisController(fixture.vscode, { workerFactory: options => {
    const worker = createAnalysisWorker(options);
    return { ...worker, async request(message, requestOptions) { requests.push(message); const reply = await worker.request(message, requestOptions); requests.push(reply); return reply; } };
  } });
  t.after(() => controller.dispose());

  assert.ok(await controller.analyze(controller.snapshot(fixture.main)), JSON.stringify(requests));
  const prepare = requests.find(message => message.op === "prepare");
  assert.ok(prepare);
  const canonicalMain = await fs.realpath(fixture.main.uri.fsPath);
  assert.deepEqual(prepare.candidates.map(value => value.toLowerCase()), [canonicalMain.toLowerCase()]);
});

test("a custom stubPath excludes only generated artifacts, not user source modules", async t => {
  const fixture = await setup(t);
  const canonicalRoot = await fs.realpath(fixture.root);
  const userSource = path.join(canonicalRoot, "src", "main.py");
  const generatedSource = path.join(canonicalRoot, "src", "graphql_desk_generated", "generated.py");
  const largeGeneratedSource = path.join(canonicalRoot, "src", "graphql_desk_generated", "large.py");
  const forgedSource = path.join(canonicalRoot, "src", "forged.py");
  await fs.mkdir(path.dirname(userSource), { recursive: true });
  await fs.writeFile(userSource, "value = 1\n");
  await fs.mkdir(path.dirname(generatedSource), { recursive: true });
  await fs.writeFile(generatedSource, renderOwnedAnalysisArtifact("graphql_desk_generated", "generated.py", "value = 2\n"));
  await fs.writeFile(largeGeneratedSource, renderOwnedAnalysisArtifact("graphql_desk_generated", "large.py", "# x\n".repeat(1024 * 1024 / 4)));
  await fs.writeFile(forgedSource, "# Generated by GraphQL Desk; analysis artifact " + "b".repeat(64) + "\nvalue = 4\n");
  fixture.stubPath("src");
  fixture.candidates(["main.py", path.relative(fixture.root, userSource), path.relative(fixture.root, generatedSource), path.relative(fixture.root, largeGeneratedSource), path.relative(fixture.root, forgedSource)]);
  const unsaved = fixture.document("src/unsaved.py", "value = 3\n");
  unsaved.isDirty = true;
  const requests = [];
  const controller = createAnalysisController(fixture.vscode, { workerFactory: options => {
    const worker = createAnalysisWorker(options);
    return { ...worker, async request(message, requestOptions) { requests.push(message); const reply = await worker.request(message, requestOptions); requests.push(reply); return reply; } };
  } });
  t.after(() => controller.dispose());

  assert.ok(await controller.analyze(controller.snapshot(fixture.main)), JSON.stringify(requests));
  const prepare = requests.find(message => message.op === "prepare");
  assert.ok(prepare);
  const candidates = prepare.candidates.map(value => value.toLowerCase());
  assert.ok(candidates.includes(userSource.toLowerCase()), "a user module under stubPath remains a source candidate");
  assert.ok(candidates.includes(path.join(canonicalRoot, "src", "unsaved.py").toLowerCase()), "a dirty user module under stubPath remains a source candidate");
  assert.ok(candidates.includes(forgedSource.toLowerCase()), "a forged or copied marker with the wrong digest remains a source candidate");
  assert.ok(!candidates.includes(generatedSource.toLowerCase()), "a generated runtime artifact is excluded");
  assert.ok(!candidates.includes(largeGeneratedSource.toLowerCase()), "a valid generated artifact with the maximum permitted source body is excluded");
});

test("stale artifact reply is a no-publish result and the worker remains usable", async t => {
  const fixture = await setup(t);
  const requests = [];
  const controller = createAnalysisController(fixture.vscode, { workerFactory: options => {
    const worker = createAnalysisWorker(options);
    return { ...worker, request(message, requestOptions) {
      requests.push(message.op);
      if (message.op === "artifacts") return Promise.resolve({ ok: true, id: message.id, current: false });
      return worker.request(message, requestOptions);
    } };
  } });
  t.after(() => controller.dispose());
  const snapshot = controller.snapshot(fixture.main);
  const skipped = [];
  assert.equal(await controller.artifacts(snapshot, undefined, reason => skipped.push(reason)), null);
  assert.deepEqual(skipped, ["worker-artifacts-stale"]);
  assert.ok(await controller.analyze(snapshot), "a stale artifact response must not kill the worker");
  assert.equal(requests.filter(operation => operation === "artifacts").length, 1);
  assert.ok(requests.filter(operation => operation === "analyze").length >= 2, "cleared stale analysis is rebuilt");
});

test("source invalidation aborts active artifact work and discards its late generation", async t => {
  const fixture = await setup(t);
  let release;
  let started;
  const ready = new Promise(resolve => { started = resolve; });
  const held = new Promise(resolve => { release = resolve; });
  let artifactCalls = 0;
  let artifactSignal;
  const controller = createAnalysisController(fixture.vscode, { workerFactory: options => {
    const worker = createAnalysisWorker(options);
    return { ...worker, async request(message, requestOptions) {
      const reply = await worker.request(message, requestOptions);
      if (message.op === "artifacts" && ++artifactCalls === 1) {
        artifactSignal = requestOptions?.signal;
        started();
        await held;
      }
      return reply;
    } };
  } });
  t.after(() => controller.dispose());

  const snapshot = controller.snapshot(fixture.main);
  const active = controller.artifacts(snapshot);
  await ready;
  fixture.main.version += 1;
  fixture.main.text += "# invalidating edit\n";
  fixture.main.isDirty = true;
  fixture.events.onDidChangeTextDocument.fire({ document: fixture.main });
  assert.equal(artifactSignal.aborted, true, "invalidation reaches the worker request's cancellation signal");
  release();

  assert.equal(await active, null, "a worker result returned after cancellation cannot be published");
  const current = await controller.artifacts(controller.snapshot(fixture.main));
  assert.ok(current, "the latest source can publish a fresh generation");
  assert.equal(artifactCalls, 2);
  controller.dispose();
});

test("empty text change notifications do not broaden operation invalidation", async t => {
  const fixture = await setup(t);
  const changes = [];
  const controller = createAnalysisController(fixture.vscode, { changed: (...args) => changes.push(args) });
  t.after(() => controller.dispose());

  fixture.events.onDidChangeTextDocument.fire({ document: fixture.main, contentChanges: [] });
  assert.deepEqual(changes, []);

  fixture.events.onDidChangeTextDocument.fire({ document: fixture.main, contentChanges: [
    { rangeOffset: 7, rangeLength: 3 },
  ] });
  assert.deepEqual(changes, [
    [fixture.main.uri.toString(), [{ start: 7, end: 10 }]],
  ]);
});

test("caller cancellation and controller disposal abort active artifacts and reject late generations", async t => {
  for (const transition of ["caller-cancellation", "disposal"]) {
    const fixture = await setup(t);
    let release;
    let started;
    const ready = new Promise(resolve => { started = resolve; });
    const held = new Promise(resolve => { release = resolve; });
    let artifactSignal;
    const controller = createAnalysisController(fixture.vscode, { workerFactory: options => {
      const worker = createAnalysisWorker(options);
      let artifactCalls = 0;
      return { ...worker, async request(message, requestOptions) {
        const reply = await worker.request(message, requestOptions);
        if (message.op === "artifacts" && artifactCalls++ === 0) {
          artifactSignal = requestOptions?.signal;
          started();
          await held;
        }
        return reply;
      } };
    } });
    const cancellation = event();
    const token = { isCancellationRequested: false, onCancellationRequested: cancellation.subscribe };
    const active = controller.artifacts(controller.snapshot(fixture.main), token);
    await ready;
    if (transition === "caller-cancellation") {
      token.isCancellationRequested = true;
      cancellation.fire();
    } else controller.dispose();
    assert.equal(artifactSignal.aborted, true, transition);
    release();
    assert.equal(await active, null, `${transition} cannot publish the worker's late result`);
    if (transition === "caller-cancellation") assert.ok(await controller.artifacts(controller.snapshot(fixture.main)));
    controller.dispose();
  }
});

test("unrelated host documents do not stale artifacts, but required schema edits do", async t => {
  const fixture = await setup(t);
  await fs.writeFile(path.join(fixture.root, "dependency.py"), "dependency = 1\n");
  fixture.candidates(["main.py", "dependency.py"]);
  const externalDocument = fixture.document("../unrelated.pyi", "value: int\n");
  const schema = fixture.document("schema.graphql", "type Query { title: String }");
  schema.isDirty = true;
  let artifactCalls = 0;
  let capturedDocuments = [];
  const controller = createAnalysisController(fixture.vscode, { workerFactory: options => {
    const worker = createAnalysisWorker(options);
    return { ...worker, async request(message, requestOptions) {
      if (message.op === "prepare") capturedDocuments = message.documents;
      const reply = await worker.request(message, requestOptions);
      if (message.op === "artifacts" && ++artifactCalls === 1) {
        fixture.document("dependency.py", "dependency = 1\n");
      }
      else if (message.op === "artifacts") { schema.text = "type Query { title: String extra: Int }"; schema.version += 1; }
      return reply;
    } };
  } });
  t.after(() => controller.dispose());
  const snapshot = controller.snapshot(fixture.main);

  const current = await controller.artifacts(snapshot);
  assert.ok(current, "an unrelated host document must not invalidate generated artifacts");
  assert.ok(!capturedDocuments.some(document => document.id === externalDocument.uri.toString()),
    "Documents outside the workspace must not enter the captured analysis snapshot");
  const analysis = await controller.analyze(snapshot);
  assert.ok(analysis);
  assert.equal(await controller.isCurrent(analysis, snapshot), true, "an unrelated host document must not stale editor analysis");
  const skipped = [];
  assert.equal(await controller.artifacts(snapshot, undefined, reason => skipped.push(reason)), null,
    "editing a required schema buffer must invalidate its prior result");
  assert.deepEqual(skipped, ["analysis-snapshot-stale"]);
});

test("saving an unchanged relevant buffer does not stale its artifacts", async t => {
  const fixture = await setup(t);
  const schemaPath = path.join(fixture.root, "schema.graphql");
  await fs.writeFile(schemaPath, "type Query { title: String }\n");
  const schema = fixture.document("schema.graphql", "type Query { title: String }\n");
  schema.isDirty = true;
  const controller = createAnalysisController(fixture.vscode, { workerFactory: options => {
    const worker = createAnalysisWorker(options);
    return { ...worker, async request(message, requestOptions) {
      const reply = await worker.request(message, requestOptions);
      if (message.op === "artifacts") { schema.isDirty = false; schema.version += 1; }
      return reply;
    } };
  } });
  t.after(() => controller.dispose());

  const snapshot = controller.snapshot(fixture.main);
  const result = await controller.artifacts(snapshot);
  assert.ok(result, "a clean transition with identical effective text must preserve current artifacts");
  assert.equal(result.isCurrent(), true);
});

test("a transient Python extension activation failure is traced and retried for live typing", async t => {
  const fixture = await setup(t);
  fixture.interpreter("");
  let activations = 0;
  const api = pythonApi(fixture, { activate: async value => {
    activations += 1;
    if (activations === 1) throw new Error("private activation detail");
    return value;
  } });
  const skipped = [];
  const controller = createAnalysisController(fixture.vscode);
  t.after(() => controller.dispose());
  const snapshot = controller.snapshot(fixture.main);

  assert.equal(await controller.artifacts(snapshot, undefined, reason => skipped.push(reason)), null);
  assert.deepEqual(skipped, ["python-extension-activation-failed"]);
  const result = await controller.artifacts(snapshot, undefined, reason => skipped.push(reason));
  assert.ok(result?.projects.some(project => project.files.some(file => file.relative_path.endsWith(".pyi"))));
  assert.equal(activations, 2);
  assert.deepEqual(skipped, ["python-extension-activation-failed"]);
  assert.ok(!skipped.join(" ").includes("private activation detail"));
  assert.ok(api.stats.resolutions >= 1);
});

test("closed-source rename and deletion replace only owned obsolete companion artifacts", async t => {
  const fixture = await setup(t);
  const controller = createAnalysisController(fixture.vscode);
  t.after(() => controller.dispose());
  const dependency = path.join(fixture.root, "dependency.py");
  const renamed = path.join(fixture.root, "renamed.py");
  const source = 'from graphql_desk_generated.default import graphql\ndoc=graphql("query Dependency { title }")\n';
  await fs.writeFile(dependency, source);
  fixture.document("dependency.py", source);
  fixture.candidates(["main.py", "dependency.py"]);
  const snapshot = controller.snapshot(fixture.main);
  const initial = await controller.artifacts(snapshot);
  assert.ok(initial);
  const stubs = path.join(fixture.root, "stubs");
  await fs.mkdir(stubs);
  const stubFiles = result => result.projects.flatMap(project => project.files.filter(file => file.relative_path.endsWith(".pyi")));
  const owned = createOwnedAnalysisArtifacts(fixture.vscode, fixture.root, stubs, "graphql_desk_generated", stubFiles(initial));
  const alias = name => path.join(stubs, "graphql_desk_generated", "sources", `${name}.pyi`);
  await fs.access(alias("dependency"));
  await fs.rename(dependency, renamed);
  fixture.candidates(["main.py", "renamed.py"]);
  fixture.watchers.find(watcher => watcher.pattern.pattern === "**/*").fire(Uri.file(dependency));
  const next = await controller.artifacts(snapshot);
  assert.ok(next);
  assert.equal(initial.isCurrent(), false);
  await owned.publish(stubFiles(next), next.isCurrent);
  await fs.access(alias("renamed"));
  await assert.rejects(fs.access(alias("dependency")), { code: "ENOENT" });
  fixture.document("renamed.py", source);
  await fs.unlink(renamed);
  fixture.candidates(["main.py"]);
  fixture.watchers.find(watcher => watcher.pattern.pattern === "**/*").fire(Uri.file(renamed));
  const removed = await controller.artifacts(snapshot);
  assert.ok(removed);
  await owned.publish(stubFiles(removed), removed.isCurrent);
  await assert.rejects(fs.access(alias("renamed")), { code: "ENOENT" });
  assert.doesNotMatch(await fs.readFile(path.join(stubs, "graphql_desk_generated", "default.pyi"), "utf8"), /query Dependency/);
  await owned.cleanup();
});

test("real configured factory capture highlights incomplete Python lexically and repairs without semantic promotion", async t => {
  const fixture = await setup(t);
  const { main } = fixture;
  main.text = "# 😀\r\nfrom graphql_desk_generated.default import graphql as query\r\nq=query('''query Read { title";
  main.version += 1; main.isDirty = true;
  const controller = createAnalysisController(fixture.vscode); t.after(() => controller.dispose());
  const service = createHighlighting(controller);
  const snapshot = controller.snapshot(main);
  const captured = await controller.analyze(snapshot); assert.ok(captured);
  assert.equal(captured.response.calls.length, 0);
  assert.ok(captured.response.diagnostics.some(item => item.code === "invalid-python"));
  assert.ok(captured.request.bindings[0].factories.some(item => item.module === "graphql_desk_generated.default" && item.export === "graphql"));
  const lexical = await service.highlight(snapshot);
  assert.ok(lexical.some(item => main.text.slice(item.start, item.end) === "query"));
  assert.ok(lexical.every(item => item.mode === "lexical"));
  const features = createLanguageFeatures(controller);
  assert.deepEqual(await features.complete(snapshot, main.text.length), []);
  assert.equal(await features.hover(snapshot, main.text.length), null);
  assert.deepEqual(await features.definition(snapshot, main.text.length), []);
  assert.ok((await features.diagnostics(snapshot)).every(item => item.code === "graphql-desk.invalid-python"));
  const foreign = structuredClone(captured);
  foreign.request.bindings[0].project = "foreign";
  assert.deepEqual(await createHighlighting({ ...controller, analyze: async () => foreign, isCurrent: async () => true }).highlight(snapshot), []);
  const foreignDiagnostic = structuredClone(captured);
  foreignDiagnostic.response.diagnostics[0].module.project = "foreign";
  assert.deepEqual(await createHighlighting({ ...controller, analyze: async () => foreignDiagnostic, isCurrent: async () => true }).highlight(snapshot), []);
  const ambiguous = structuredClone(captured);
  ambiguous.request.modules.push({ ...ambiguous.request.modules[0], project: "foreign" });
  assert.deepEqual(await createHighlighting({ ...controller, analyze: async () => ambiguous, isCurrent: async () => true }).highlight(snapshot), []);
  assert.deepEqual(await createHighlighting({ ...controller, analyze: async () => captured }).highlight({ ...snapshot, version: snapshot.version + 1 }), []);
  fixture.vscode.workspace.isTrusted = false;
  assert.deepEqual(await service.highlight(snapshot), []);
  fixture.vscode.workspace.isTrusted = true;
  const config = fixture.document("pyproject.toml", '[tool.graphql-desk]\nschema="schema.graphql"\ngenerated_package="changed"\nschema_output="changed/default.pyi"\n');
  config.isDirty = true;
  assert.equal(await controller.isCurrent(captured, snapshot), false);
  assert.deepEqual(await createHighlighting({ ...controller, analyze: async () => captured }).highlight(snapshot), []);
  assert.deepEqual(await service.highlight(snapshot), [], "New config does not retain the old generated factory");
  main.text = "from changed.default import graphql as query\nq=query('''query Read { title";
  main.version += 1;
  const changed = await service.highlight(controller.snapshot(main));
  assert.ok(changed.length);
  assert.ok(changed.every(item => item.mode === "lexical"));
  main.text += " }''')"; main.version += 1;
  const repaired = await service.highlight(controller.snapshot(main));
  assert.ok(repaired.length);
  assert.ok(repaired.every(item => item.mode === "resolved"));
  assert.equal(await fs.readFile(path.join(fixture.root, "main.py"), "utf8"), 'raise RuntimeError("NO_IMPORT")\nfrom graphql_desk import gql\nq=gql("{title}")\n');
  assert.equal(main.isDirty, true);
  await assert.rejects(fs.stat(path.join(fixture.root, "changed")), { code: "ENOENT" });
});

test("opening a verified live generation artifact does not change the source snapshot", async t => {
  const fixture = await setup(t);
  const controller = createAnalysisController(fixture.vscode);
  t.after(() => controller.dispose());
  const snapshot = controller.snapshot(fixture.main);
  const bundle = await controller.analyze(snapshot);
  assert.ok(bundle);
  const generation = `g-1-${"a".repeat(8)}-${"b".repeat(4)}-${"c".repeat(4)}-${"d".repeat(4)}-${"e".repeat(12)}`;
  const generationRoot = path.join(fixture.root, ".graphql-desk", "live-typings", "generations", generation);
  const generated = path.join(generationRoot, "graphql_desk_generated", "default.pyi");
  const bytes = renderOwnedAnalysisArtifact("graphql_desk_generated", "default.pyi", "def graphql(query: str) -> object: ...\n");
  await fs.mkdir(path.dirname(generated), { recursive: true });
  await fs.writeFile(generated, bytes);
  fixture.stubPath(path.relative(fixture.root, generationRoot));
  const generatedDocument = fixture.document(path.relative(fixture.root, generated), bytes.toString("utf8"));
  fixture.events.onDidOpenTextDocument.fire(generatedDocument);
  assert.equal(await controller.isCurrent(bundle, snapshot), true,
    "Pylance opening GraphQL Desk's own generated stub must not invalidate the analysis that produced it");
  fixture.events.onDidCloseTextDocument.fire(generatedDocument);
  assert.equal(await controller.isCurrent(bundle, snapshot), true,
    "Closing the generated document does not change the captured source snapshot");
  assert.equal(generatedDocument.isDirty, false);
});

test("only exact owned config replacement temp paths and hashes are ignored by the source watcher", async t => {
  const fixture = await setup(t);
  let invalidations = 0;
  const controller = createAnalysisController(fixture.vscode, { changed() { invalidations += 1; } });
  t.after(() => controller.dispose());
  assert.ok(await controller.analyze(controller.snapshot(fixture.main)));
  const watcher = fixture.watchers.find(item => item.pattern.pattern === "**/*");
  for (const [filename, originalText, ownedText, userText] of [
    ["pyrightconfig.json", '{"stubPath":"typings"}\n', '{"stubPath":".graphql-desk/live-typings/generations/g-1-owned"}\n',
      '{"stubPath":".graphql-desk/live-typings/generations/g-1-owned","typeCheckingMode":"strict"}\n'],
    ["pyproject.toml", '[tool.pyright]\nstubPath = "typings"\n',
      '[tool.pyright]\nstubPath = ".graphql-desk/live-typings/generations/g-1-owned"\n',
      '[tool.pyright]\nstubPath = ".graphql-desk/live-typings/generations/g-1-owned"\ntypeCheckingMode = "strict"\n'],
  ]) {
    const file = path.join(fixture.root, filename);
    const beforeOwnedWrite = invalidations;
    await fs.writeFile(file, originalText);
    const previousHash = createHash("sha256").update(originalText).digest("hex");
    const nextHash = createHash("sha256").update(ownedText).digest("hex");
    const temporaryPath = path.join(fixture.root, ".graphql-desk-aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee.tmp");
    assert.equal(controller.markOwnedProjectConfig(file, previousHash), true);
    assert.equal(controller.trackOwnedProjectConfigWrite(file, nextHash,
      { phase: "begin", previousHash, temporaryPath }), true);
    await fs.writeFile(temporaryPath, ownedText);
    watcher.fire(Uri.file(temporaryPath));
    assert.equal(invalidations, beforeOwnedWrite, `The exact owned ${filename} temp file and hash do not invalidate analysis`);
    await fs.unlink(temporaryPath);
    watcher.fire(Uri.file(temporaryPath));
    assert.equal(invalidations, beforeOwnedWrite, `The exact owned ${filename} temp rename event does not invalidate analysis`);
    await fs.unlink(file);
    watcher.fire(Uri.file(file));
    assert.equal(invalidations, beforeOwnedWrite, `The delete event inside an owned ${filename} replacement must not invalidate analysis`);
    await fs.writeFile(file, ownedText);
    assert.equal(controller.trackOwnedProjectConfigWrite(file, nextHash, { phase: "commit" }), true);
    watcher.fire(Uri.file(file));
    assert.equal(invalidations, beforeOwnedWrite, `An exact owned ${filename} stubPath rotation must not restart source analysis`);
    await fs.writeFile(file, userText);
    watcher.fire(Uri.file(file));
    assert.equal(invalidations, beforeOwnedWrite + 1,
      `A later user edit to ${filename} is still observed`);
    await fs.unlink(file);
    watcher.fire(Uri.file(file));
    assert.equal(invalidations, beforeOwnedWrite + 2,
      `A later user deletion of ${filename} is still observed`);
  }
  const foreignTemporary = path.join(fixture.root, ".graphql-desk-11111111-2222-3333-4444-555555555555.tmp");
  await fs.writeFile(foreignTemporary, "user temp content");
  watcher.fire(Uri.file(foreignTemporary));
  assert.equal(invalidations, 5, "An unregistered GraphQL Desk-shaped temp path remains a visible source change");
});

test("delayed owned config temp deletion preserves fresh artifacts after an interpreter reset", async t => {
  for (const phase of ["commit", "cancel"]) await t.test(phase, async t => {
    const fixture = await setup(t);
    let invalidations = 0;
    const controller = createAnalysisController(fixture.vscode, { changed() { invalidations += 1; } });
    t.after(() => controller.dispose());
    const snapshot = controller.snapshot(fixture.main);
    const previous = await controller.artifacts(snapshot);
    assert.ok(previous);
    pythonApi(fixture);
    fixture.interpreter("");
    fixture.events.onDidChangeConfiguration.fire({ affectsConfiguration: name => name === "graphqlDesk" });
    const fresh = await controller.artifacts(snapshot);
    assert.ok(fresh);
    assert.equal(previous.isCurrent(), false);
    assert.equal(fresh.isCurrent(), true);
    const watcher = fixture.watchers.find(item => item.pattern.pattern === "**/*");
    const file = path.join(fixture.root, "pyrightconfig.json");
    const original = '{"stubPath":"typings"}\n';
    const updated = '{"stubPath":".graphql-desk/live-typings"}\n';
    await fs.writeFile(file, original);
    const previousHash = createHash("sha256").update(original).digest("hex");
    const nextHash = createHash("sha256").update(updated).digest("hex");
    const temporaryPath = path.join(fixture.root, ".graphql-desk-aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee.tmp");
    controller.markOwnedProjectConfig(file, previousHash);
    t.mock.timers.enable({ apis: ["setTimeout"], now: 1000 });
    assert.equal(controller.trackOwnedProjectConfigWrite(file, nextHash, { phase: "begin", previousHash, temporaryPath }), true);
    await fs.writeFile(temporaryPath, updated);
    watcher.fire(Uri.file(temporaryPath));
    if (phase === "commit") await fs.rename(temporaryPath, file);
    else await fs.unlink(temporaryPath);
    assert.equal(controller.trackOwnedProjectConfigWrite(file, phase === "commit" ? nextHash : null, { phase }), true);
    t.mock.timers.tick(5001);
    const beforeDelete = invalidations;
    watcher.fire(Uri.file(temporaryPath));
    assert.equal(invalidations, beforeDelete, "A delayed delete from the exact finished transaction must not restart analysis");
    assert.equal(fresh.isCurrent(), true, "The new interpreter's artifacts remain eligible for selection");
    watcher.fire(Uri.file(temporaryPath));
    assert.equal(invalidations, beforeDelete, "Duplicate delayed deletes remain deduplicated");
    await fs.writeFile(temporaryPath, "foreign replacement\n");
    watcher.fire(Uri.file(temporaryPath));
    assert.equal(invalidations, beforeDelete + 1, "Recreated foreign bytes remain source changes");
    await fs.unlink(temporaryPath);
    watcher.fire(Uri.file(temporaryPath));
    assert.equal(invalidations, beforeDelete + 2, "Deleting the foreign replacement is observed");
    if (phase === "cancel") {
      for (let index = 0; index < 520; index += 1) watcher.fire(Uri.file(path.join(fixture.root, `foreign-${index}.tmp`)));
      const afterEviction = invalidations;
      watcher.fire(Uri.file(temporaryPath));
      assert.equal(invalidations, afterEviction + 1, "Evicted cleanup fingerprints revert to conservative invalidation");
    }
  });
});

test("finished config transactions do not exempt existing, unreadable or unregistered temp paths", async t => {
  for (const kind of ["foreign bytes", "matching bytes", "unreadable", "unregistered"]) await t.test(kind, async t => {
    const fixture = await setup(t);
    let invalidations = 0;
    const controller = createAnalysisController(fixture.vscode, { changed() { invalidations += 1; } });
    t.after(() => controller.dispose());
    const watcher = fixture.watchers.find(item => item.pattern.pattern === "**/*");
    const file = path.join(fixture.root, "pyrightconfig.json");
    const temporaryPath = path.join(fixture.root, ".graphql-desk-aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee.tmp");
    const previousHash = createHash("sha256").update("previous").digest("hex");
    const nextHash = createHash("sha256").update("owned bytes").digest("hex");
    if (kind !== "unregistered") {
      assert.equal(controller.trackOwnedProjectConfigWrite(file, nextHash, { phase: "begin", previousHash, temporaryPath }), true);
      await fs.writeFile(temporaryPath, kind === "foreign bytes" ? "foreign bytes" : "owned bytes");
    }
    if (kind === "unreadable") {
      const fsSync = require("node:fs");
      const lstat = fsSync.lstatSync;
      const blocked = t.mock.method(fsSync, "lstatSync", filename => {
        if (path.resolve(filename) === path.resolve(temporaryPath)) throw Object.assign(new Error("unreadable"), { code: "EACCES" });
        return lstat(filename);
      });
      controller.trackOwnedProjectConfigWrite(file, null, { phase: "cancel" });
      blocked.mock.restore();
      await fs.unlink(temporaryPath);
    } else controller.trackOwnedProjectConfigWrite(file, null, { phase: "cancel" });
    const beforeEvent = invalidations;
    watcher.fire(Uri.file(temporaryPath));
    assert.equal(invalidations, beforeEvent + 1, `${kind} must remain a source change after cancellation`);
  });
});

test("only the active workspace TOML stubPath ownership is forwarded to Python analysis", async t => {
  const fixture = await setup(t);
  const requests = [];
  const controller = createAnalysisController(fixture.vscode, { workerFactory: options => {
    const worker = createAnalysisWorker(options);
    return { ...worker, request(message) { requests.push(message); return worker.request(message); } };
  } });
  t.after(() => controller.dispose());
  const generation = ".graphql-desk/live-typings/generations/g-1-aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee";
  const configPath = path.join(fixture.root, "pyproject.toml");
  const originalConfig = await fs.readFile(configPath, "utf8");
  await fs.writeFile(configPath, `${originalConfig}[tool.pyright]\nstubPath = "${generation}"\n`);
  assert.equal(controller.setManagedProjectStubPath(fixture.root,
    configPath, generation, "custom-stubs", true), true);
  assert.equal(controller.setManagedProjectStubPath(fixture.root,
    path.join(fixture.root, "pyrightconfig.json"), generation, "custom-stubs", true), false);
  assert.equal(controller.setManagedProjectStubPath(fixture.root,
    configPath, "../custom-stubs", "custom-stubs", true), false);
  const bundle = await controller.analyze(controller.snapshot(fixture.main));
  assert.ok(bundle);
  const prepare = requests.find(message => message.op === "prepare");
  assert.equal(prepare.managed_pyright_config_path, "pyproject.toml");
  assert.equal(prepare.managed_pyright_stub_path, generation);
  assert.equal(prepare.managed_pyright_previous_stub_path, "custom-stubs");
  assert.equal(prepare.managed_pyright_previous_stub_path_present, true);
});

test("current analysis accepts only an exact owned TOML stubPath rotation", async t => {
  const fixture = await setup(t);
  const requests = [];
  const controller = createAnalysisController(fixture.vscode, { workerFactory: options => {
    const worker = createAnalysisWorker(options);
    return { ...worker, request(message) { requests.push(message); return worker.request(message); } };
  } });
  t.after(() => controller.dispose());
  const first = ".graphql-desk/live-typings/generations/g-1-aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee";
  const next = ".graphql-desk/live-typings/generations/g-2-aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee";
  assert.equal(controller.setManagedProjectStubPath(fixture.root,
    path.join(fixture.root, "pyproject.toml"), first), true);
  const original = await fs.readFile(path.join(fixture.root, "pyproject.toml"), "utf8");
  await fs.writeFile(path.join(fixture.root, "pyproject.toml"),
    `${original}[tool.pyright]\nstubPath = "${first}"\n`);
  const analysis = await controller.analyze(controller.snapshot(fixture.main));
  assert.ok(analysis);

  assert.equal(controller.setManagedProjectStubPath(fixture.root,
    path.join(fixture.root, "pyproject.toml"), next), true);
  await fs.writeFile(path.join(fixture.root, "pyproject.toml"),
    `${original}[tool.pyright]\nstubPath = "${next}"\n`);
  const generated = await controller.artifacts(controller.snapshot(fixture.main));
  assert.ok(generated, "Artifact freshness tolerates an exact owner-verified root rotation");
  assert.equal(requests.filter(message => message.op === "artifacts").at(-1).managed_pyright_stub_path, next,
    "The artifact freshness check receives the exact newly owned project setting");
  assert.equal(await controller.isCurrent(analysis, controller.snapshot(fixture.main)), true,
    "An exact owner-verified generation rotation does not stale unchanged analysis inputs");
  assert.equal(requests.filter(message => message.op === "current").at(-1).managed_pyright_stub_path, next,
    "The current check receives the newly owned exact project setting");

  const afterRotation = await controller.analyze(controller.snapshot(fixture.main));
  assert.ok(afterRotation);
  await fs.writeFile(path.join(fixture.root, "pyproject.toml"),
    `${original}[tool.pyright]\nstubPath = "${next}"\ntypeCheckingMode = "strict"\n`);
  assert.equal(await controller.isCurrent(afterRotation, controller.snapshot(fixture.main)), false,
    "Unrelated project-config edits remain visible to freshness checks");
});

test("current analysis accepts the exact owner-verified initial TOML takeover", async t => {
  const fixture = await setup(t);
  const requests = [];
  const controller = createAnalysisController(fixture.vscode, { workerFactory: options => {
    const worker = createAnalysisWorker(options);
    return { ...worker, request(message) { requests.push(message); return worker.request(message); } };
  } });
  t.after(() => controller.dispose());
  const configPath = path.join(fixture.root, "pyproject.toml");
  const original = await fs.readFile(configPath, "utf8");
  await fs.writeFile(configPath, `${original}[tool.pyright]\nstubPath = "custom-stubs"\nextraPaths = ["vendor"]\n`);
  const analysis = await controller.analyze(controller.snapshot(fixture.main));
  assert.ok(analysis);

  const managed = ".graphql-desk/live-typings";
  assert.equal(controller.setManagedProjectStubPath(fixture.root, configPath, managed, "custom-stubs", true), true);
  await fs.writeFile(configPath,
    `${original}[tool.pyright]\nstubPath = "${managed}"\nextraPaths = ["vendor"]\n`);
  const generated = await controller.artifacts(controller.snapshot(fixture.main));
  assert.ok(generated, "The first owned config takeover preserves unchanged analysis facts");
  const current = requests.filter(message => message.op === "artifacts").at(-1);
  assert.equal(current.managed_pyright_stub_path, managed);
  assert.equal(current.managed_pyright_previous_stub_path, "custom-stubs");
  assert.equal(current.managed_pyright_previous_stub_path_present, true);

  await fs.writeFile(configPath,
    `${original}[tool.pyright]\nstubPath = "${managed}"\nextraPaths = ["vendor"]\ntypeCheckingMode = "strict"\n`);
  assert.equal(await controller.isCurrent(analysis, controller.snapshot(fixture.main)), false,
    "Unrelated TOML edits remain stale even with the owner transition pair");
});

test("persisted TOML ownership is loaded before the first restart artifacts request", async t => {
  const fixture = await setup(t);
  const requests = [];
  const controller = createAnalysisController(fixture.vscode, { workerFactory: options => {
    const worker = createAnalysisWorker(options);
    return { ...worker, request(message) { requests.push(message); return worker.request(message); } };
  } });
  t.after(() => controller.dispose());
  const configPath = path.join(fixture.root, "pyproject.toml");
  const original = await fs.readFile(configPath, "utf8");
  const managed = ".graphql-desk/live-typings";
  await fs.writeFile(configPath, `${original}[tool.pyright]\nstubPath = "${managed}"\nextraPaths = ["vendor"]\n`);
  assert.equal(controller.setManagedProjectStubPath(fixture.root, configPath, managed, undefined, false), true);
  const artifacts = await controller.artifacts(controller.snapshot(fixture.main));
  assert.ok(artifacts, "The first request after restoring persisted ownership can publish artifacts");
  const request = requests.find(message => message.op === "artifacts");
  assert.equal(request.managed_pyright_config_path, "pyproject.toml");
  assert.equal(request.managed_pyright_stub_path, managed);
  assert.equal(request.managed_pyright_previous_stub_path_present, false);
  assert.equal(Object.hasOwn(request, "managed_pyright_previous_stub_path"), false);
});

test("duplicate watcher reports for identical schema bytes cause one refresh, changed bytes still refresh", async t => {
  const fixture = await setup(t);
  let invalidations = 0;
  const controller = createAnalysisController(fixture.vscode, { changed() { invalidations += 1; } });
  t.after(() => controller.dispose());
  assert.ok(await controller.analyze(controller.snapshot(fixture.main)));
  const workspaceWatcher = fixture.watchers.find(item => item.pattern.pattern === "**/*");
  const schemaWatcher = fixture.watchers.find(item => item.pattern.pattern === "schema.graphql");
  assert.ok(workspaceWatcher);
  assert.ok(schemaWatcher);
  const schemaUri = Uri.file(path.join(fixture.root, "schema.graphql"));
  workspaceWatcher.fire(schemaUri);
  schemaWatcher.fire(schemaUri);
  assert.equal(invalidations, 1, "The same disk bytes reported by both watchers share one refresh");
  await fs.writeFile(path.join(fixture.root, "schema.graphql"), "type Query { title: String changed: Int }");
  workspaceWatcher.fire(schemaUri);
  schemaWatcher.fire(schemaUri);
  assert.equal(invalidations, 2, "A different schema fingerprint remains a new refresh");
});

for (const kind of ["artifact", "generation manifest", "generation directory"]) {
  test(`repeated owned ${kind} deletions preserve analysis without hiding foreign replacements`, async t => {
    const fixture = await setup(t);
    let invalidations = 0;
    const controller = createAnalysisController(fixture.vscode, { changed() { invalidations += 1; } });
    t.after(() => controller.dispose());
    const snapshot = controller.snapshot(fixture.main);
    const bundle = await controller.analyze(snapshot);
    assert.ok(bundle);
    const watcher = fixture.watchers.find(item => item.pattern.pattern === "**/*");
    const generation = path.join(fixture.root, ".graphql-desk", "typings", "generations", "g-1-00000000-0000-0000-0000-000000000001");
    const filename = kind === "generation directory" ? generation : kind === "generation manifest"
      ? path.join(generation, "complete.json")
      : path.join(fixture.root, ".graphql-desk", "typings", "graphql_desk_generated", "default.pyi");
    const resource = Uri.file(filename);
    const writeOwned = async () => {
      await fs.mkdir(path.dirname(filename), { recursive: true });
      if (kind === "generation directory") await fs.mkdir(filename);
      else await fs.writeFile(filename, kind === "artifact"
        ? renderOwnedAnalysisArtifact("graphql_desk_generated", "default.pyi", "class Generated: ...\n")
        : JSON.stringify({ owner: "graphql-desk-analysis-generation" }));
    };
    const removeOwned = () => kind === "generation directory" ? fs.rmdir(filename) : fs.unlink(filename);
    // A foreign artifact directory exercises the unreadable/unfingerprintable file case too.
    const writeForeign = () => kind === "artifact" ? fs.mkdir(filename) : fs.writeFile(filename, "user bytes\n");
    const removeForeign = () => kind === "artifact" ? fs.rmdir(filename) : fs.unlink(filename);
    const reportMissing = () => { for (let repeat = 0; repeat < 3; repeat += 1) watcher.fire(resource); };

    await writeOwned(); watcher.fire(resource);
    await removeOwned(); reportMissing();
    assert.equal(invalidations, 0, "All repeated missing reports for a verified owned output are ignored");
    assert.equal(await controller.isCurrent(bundle, snapshot), true, "Cleanup must not stale the publication's capture");

    await writeForeign(); watcher.fire(resource);
    assert.equal(invalidations, 1, "A foreign recreation must invalidate despite the cached missing fingerprint");
    await removeForeign(); reportMissing();
    assert.equal(invalidations, 2, "Deleting the foreign recreation is a new change, not an old owned deletion");

    await writeOwned(); watcher.fire(resource);
    assert.equal(invalidations, 2, "A verified owned recreation remains an output");
    await removeOwned(); reportMissing();
    assert.equal(invalidations, 2, "The recreated owned output can be cleaned up repeatedly again");

    await writeOwned(); watcher.fire(resource);
    await removeOwned(); await writeForeign(); watcher.fire(resource);
    assert.equal(invalidations, 3, "A coalesced foreign replacement revokes the previous ownership");
    await removeForeign(); reportMissing();
    assert.equal(invalidations, 4, "The foreign replacement's deletion must not consume stale ownership");
  });
}

test("clean generated document notifications after owned cleanup preserve fresh analysis", async t => {
  const fixture = await setup(t);
  let invalidations = 0;
  const controller = createAnalysisController(fixture.vscode, { changed() { invalidations += 1; } });
  t.after(() => controller.dispose());
  const watcher = fixture.watchers.find(item => item.pattern.pattern === "**/*");
  const filename = path.join(fixture.root, ".graphql-desk", "typings", "graphql_desk_generated", "default.pyi");
  const bytes = renderOwnedAnalysisArtifact("graphql_desk_generated", "default.pyi", "class Generated: ...\n");
  await fs.mkdir(path.dirname(filename), { recursive: true });
  await fs.writeFile(filename, bytes);
  watcher.fire(Uri.file(filename));
  const document = fixture.document(path.relative(fixture.root, filename), bytes.toString("utf8"));
  await fs.unlink(filename);
  watcher.fire(document.uri);
  for (const name of ["onDidOpenTextDocument", "onDidSaveTextDocument", "onDidCloseTextDocument"]) {
    fixture.events[name].fire(document);
  }
  fixture.events.onDidChangeTextDocument.fire({ document, contentChanges: [{ rangeOffset: 0, rangeLength: 1 }] });
  assert.equal(invalidations, 0, "Delayed clean editor events must not turn owned cleanup into a source edit");
  document.isDirty = true;
  fixture.events.onDidChangeTextDocument.fire({ document, contentChanges: [{ rangeOffset: 0, rangeLength: 1 }] });
  assert.equal(invalidations, 1, "Unsaved edits to a deleted generated document still invalidate analysis");
  document.isDirty = false;
  await fs.writeFile(filename, "class UserOwned: ...\n");
  fixture.events.onDidOpenTextDocument.fire(document);
  assert.equal(invalidations, 2, "A foreign recreation is observed even before its watcher event arrives");
  await fs.unlink(filename);
  watcher.fire(document.uri);
  fixture.events.onDidCloseTextDocument.fire(document);
  assert.equal(invalidations, 4, "Foreign deletion must not inherit the former generated document exemption");
  await fs.writeFile(filename, bytes);
  fixture.events.onDidOpenTextDocument.fire(document);
  await fs.unlink(filename);
  watcher.fire(document.uri);
  fixture.events.onDidCloseTextDocument.fire(document);
  assert.equal(invalidations, 4, "A verified document recreation resets an earlier generic missing fingerprint");
});

test("unobserved output deletion still invalidates and owned-delete bookkeeping stays bounded", async t => {
  const fixture = await setup(t);
  let invalidations = 0;
  const controller = createAnalysisController(fixture.vscode, { changed() { invalidations += 1; } });
  t.after(() => controller.dispose());
  const watcher = fixture.watchers.find(item => item.pattern.pattern === "**/*");
  const filename = path.join(fixture.root, ".graphql-desk", "typings", "graphql_desk_generated", "default.pyi");
  const resource = Uri.file(filename);
  await fs.mkdir(path.dirname(filename), { recursive: true });
  const ownedBytes = renderOwnedAnalysisArtifact("graphql_desk_generated", "default.pyi", "class Generated: ...\n");
  await fs.writeFile(filename, ownedBytes);
  await fs.unlink(filename);
  watcher.fire(resource); watcher.fire(resource);
  assert.equal(invalidations, 1, "Missing files whose creation was never observed cannot be assumed owned");

  await fs.writeFile(filename, ownedBytes); watcher.fire(resource);
  await fs.unlink(filename); watcher.fire(resource); watcher.fire(resource);
  assert.equal(invalidations, 1, "An observed owned recreation can be deleted without invalidating");
  await fs.writeFile(filename, "class UserOwned: ...\n"); watcher.fire(resource);
  assert.equal(invalidations, 2, "Recreated unowned bytes replace the cached missing fingerprint");
  await fs.writeFile(filename, "class UserChanged: ...\n"); watcher.fire(resource);
  assert.equal(invalidations, 3, "Further foreign-byte changes remain observed");
  await fs.unlink(filename); watcher.fire(resource); watcher.fire(resource);
  assert.equal(invalidations, 4, "Foreign deletion remains a new change after an owned path was reused");
  await fs.writeFile(filename, ownedBytes); watcher.fire(resource);
  await fs.unlink(filename); watcher.fire(resource); watcher.fire(resource);
  assert.equal(invalidations, 4);
  for (let index = 0; index < 513; index += 1) watcher.fire(Uri.file(path.join(fixture.root, `missing-${index}.txt`)));
  assert.equal(invalidations, 517);
  watcher.fire(resource);
  assert.equal(invalidations, 518, "Eviction conservatively invalidates rather than retaining owned-delete tombstones forever");
  watcher.fire(resource);
  assert.equal(invalidations, 518, "The evicted path is deduplicated again after its conservative invalidation");
});

test("ownership metadata staging does not invalidate analysis after generation selection", async t => {
  const fixture = await setup(t);
  let invalidations = 0;
  const controller = createAnalysisController(fixture.vscode, { changed() { invalidations += 1; } });
  t.after(() => controller.dispose());
  const root = path.join(fixture.root, ".graphql-desk", "live-typings");
  await fs.mkdir(root, { recursive: true });
  fixture.stubPath(path.relative(fixture.root, path.join(root, "generations", "g-3-123e4567-e89b-12d3-a456-426614174000")));
  const watcher = fixture.watchers.find(item => item.pattern.pattern === "**/*");
  const filename = path.join(root, ".graphql-desk-owner-123e4567-e89b-12d3-a456-426614174000.tmp");
  const resource = Uri.file(filename);
  for (const text of ["", '{"phase":"rotate"}\n']) {
    await fs.writeFile(filename, text);
    watcher.fire(resource);
    fixture.events.onDidOpenTextDocument.fire({ uri: resource });
  }
  await fs.unlink(filename);
  watcher.fire(resource);
  watcher.fire(resource);
  fixture.events.onDidCloseTextDocument.fire({ uri: resource });
  assert.equal(invalidations, 0, "Ownership-file staging is control metadata outside the selected generation");
  for (const foreign of [path.join(root, ".graphql-desk-123e4567-e89b-12d3-a456-426614174000.tmp"),
    path.join(root, ".graphql-desk-owner-not-a-uuid.tmp"),
    path.join(fixture.root, path.basename(filename))]) {
    const before = invalidations;
    await fs.writeFile(foreign, "user temporary content\n");
    watcher.fire(Uri.file(foreign));
    assert.equal(invalidations, before + 1, "Unrelated, malformed and out-of-root temporary files remain observable");
  }
});

test("stat fallback refreshes closed sources when every VS Code watcher event is lost", async t => {
  const disk = require("node:fs");
  const watchFile = disk.watchFile;
  const unwatchFile = disk.unwatchFile;
  const active = new Map();
  t.mock.method(disk, "watchFile", (filename, options, listener) => {
    assert.deepEqual(options, { persistent: false, bigint: true });
    active.set(filename.toLowerCase(), listener);
    return watchFile(filename, { ...options, interval: 25 }, listener);
  });
  t.mock.method(disk, "unwatchFile", (filename, listener) => {
    assert.equal(active.get(filename.toLowerCase()), listener);
    active.delete(filename.toLowerCase());
    return unwatchFile(filename, listener);
  });
  const fixture = await setup(t);
  const pollKey = name => path.join(disk.realpathSync.native(fixture.root), name).toLowerCase();
  const source = 'from graphql_desk import gql\nclosed = gql("{title}")\n';
  let filename = path.join(fixture.root, "closed.py");
  await fs.writeFile(filename, source);
  fixture.candidates(["main.py", "closed.py"]);
  const changes = [];
  let latest;
  let refresh;
  const controller = createAnalysisController(fixture.vscode, { changed(uri) {
    changes.push(uri);
    refresh = controller.analyze(controller.snapshot(fixture.main)).then(bundle => { latest = bundle; });
  } });
  t.after(() => controller.dispose());
  assert.ok(await controller.analyze(controller.snapshot(fixture.main)));
  assert.equal(active.size, 2, "Only analyzed source paths are stat-polled");
  const broad = fixture.watchers.find(watcher => watcher.pattern.pattern === "**/*");
  for (const name of ["renamed.py", "final.py"]) {
    const previous = filename;
    const next = path.join(fixture.root, name);
    await fs.rename(filename, next);
    fixture.candidates(["main.py", name]);
    await eventually(() => latest?.request.modules.some(module => module.module === path.parse(name).name));
    assert.equal(active.has(pollKey(path.basename(previous))), false, "Obsolete poll listeners are retired");
    assert.ok(active.has(pollKey(name)), "The renamed source has a replacement poll");
    assert.equal(active.size, 2);
    const count = changes.length;
    broad.fire(Uri.file(previous));
    assert.equal(changes.length, count, "Late native duplicate shares the missing-file fingerprint");
    filename = next;
  }
  const edited = source.replace("{title}", "{title title}");
  await fs.writeFile(filename, edited);
  await eventually(() => latest?.request.modules.find(module => module.module === "final")?.source === edited);
  const count = changes.length;
  await new Promise(resolve => realTimeout(resolve, 100));
  assert.equal(changes.length, count, "Unchanged stat scans do not republish");
  await fs.unlink(filename);
  fixture.candidates(["main.py"]);
  await eventually(() => latest && !latest.request.modules.some(module => module.module === "final"));
  await refresh;
  assert.equal(active.size, 1);
  controller.dispose();
  assert.equal(active.size, 0, "Every exact polling listener is removed on dispose");
});

test("source polls stay filtered through invalid config and ignore retired or untrusted callbacks", async t => {
  const disk = require("node:fs");
  const active = new Map();
  t.mock.method(disk, "watchFile", (filename, _options, listener) => active.set(filename.toLowerCase(), listener));
  t.mock.method(disk, "unwatchFile", (filename, listener) => {
    assert.equal(active.get(filename.toLowerCase()), listener);
    active.delete(filename.toLowerCase());
  });
  const fixture = await setup(t);
  const pollKey = name => path.join(disk.realpathSync.native(fixture.root), name).toLowerCase();
  const config = path.join(fixture.root, "pyproject.toml");
  const original = await fs.readFile(config, "utf8");
  const filename = path.join(fixture.root, "closed.py");
  await fs.writeFile(filename, 'from graphql_desk import gql\nclosed = gql("{title}")\n');
  fixture.candidates(["main.py", "closed.py"]);
  let refresh;
  let changes = 0;
  const controller = createAnalysisController(fixture.vscode, { changed() {
    changes += 1;
    refresh = controller.analyze(controller.snapshot(fixture.main));
  } });
  t.after(() => controller.dispose());
  assert.ok(await controller.analyze(controller.snapshot(fixture.main)));
  const retired = active.get(pollKey("closed.py"));
  const schema = fixture.watchers.find(watcher => watcher.pattern.pattern === "schema.graphql");
  const broad = fixture.watchers.find(watcher => watcher.pattern.pattern === "**/*");
  await fs.writeFile(config, '[tool.graphql-desk]\nschema =\n');
  broad.fire(Uri.file(config)); await refresh;
  assert.equal(active.size, 2, "Previously analyzed inputs remain available during config repair");
  const next = path.join(fixture.root, "renamed.py");
  await fs.rename(filename, next);
  fixture.candidates(["main.py", "renamed.py"]);
  retired(); await refresh;
  assert.equal(active.has(pollKey("closed.py")), false);
  assert.equal(active.has(pollKey("renamed.py")), false, "Invalid config does not adopt new raw candidates");
  assert.equal(active.size, 1);
  assert.equal(schema.disposed, false, "Existing schema repair watch is preserved");
  let count = changes;
  retired();
  assert.equal(changes, count, "A queued obsolete callback cannot invalidate its replacement");
  await fs.writeFile(config, original);
  broad.fire(Uri.file(config)); await refresh;
  assert.equal(active.size, 2);
  const unsaved = fixture.document("unsaved.py", 'from graphql_desk import gql\nunsaved = gql("{title}")\n');
  unsaved.isDirty = true;
  assert.ok(await controller.analyze(controller.snapshot(fixture.main)));
  const initiallyAbsent = active.get(pollKey("unsaved.py"));
  count = changes;
  initiallyAbsent({ nlink: 0n }, { nlink: 0n });
  assert.equal(changes, count, "A never-saved input's initial absence is already captured");
  await fs.writeFile(unsaved.uri.fsPath, unsaved.text);
  fixture.candidates(["main.py", "renamed.py", "unsaved.py"]);
  assert.ok(await controller.analyze(controller.snapshot(fixture.main)));
  assert.equal(active.get(pollKey("unsaved.py")), initiallyAbsent, "Reconciliation reuses the exact listener");
  await fs.unlink(unsaved.uri.fsPath);
  fixture.candidates(["main.py", "renamed.py"]);
  initiallyAbsent({ nlink: 0n }, { nlink: 0n }); await refresh;
  assert.equal(changes, count + 1, "A retained listener uses the latest disk-discovery facts");
  initiallyAbsent({ nlink: 0n }, { nlink: 0n });
  assert.equal(changes, count + 1, "Stable missing dirty buffers do not loop");
  const callbacks = [...active.values()];
  fixture.vscode.workspace.isTrusted = false;
  count = changes;
  callbacks.forEach(listener => listener());
  assert.equal(changes, count);
  assert.equal(active.size, 0, "Revoked trust removes every source polling listener");
  fixture.vscode.workspace.isTrusted = true;
  assert.ok(await controller.analyze(controller.snapshot(fixture.main)));
  assert.equal(active.size, 3);
  controller.dispose();
  count = changes;
  callbacks.forEach(listener => listener());
  assert.equal(changes, count);
  assert.equal(active.size, 0);
});

test("source polling handles initial disappearance, refuses redirection and cleans failed registrations", async t => {
  const disk = require("node:fs");
  const active = new Map();
  let reject = false;
  t.mock.method(disk, "watchFile", (filename, _options, listener) => {
    active.set(filename.toLowerCase(), listener);
    if (reject) throw new Error("Polling unavailable");
  });
  t.mock.method(disk, "unwatchFile", (filename, listener) => {
    assert.equal(active.get(filename.toLowerCase()), listener);
    active.delete(filename.toLowerCase());
  });
  const fixture = await setup(t);
  const changes = [];
  const controller = createAnalysisController(fixture.vscode, { changed: uri => changes.push(uri) });
  t.after(() => controller.dispose());
  reject = true;
  assert.equal(await controller.analyze(controller.snapshot(fixture.main)), null);
  assert.equal(active.size, 0, "A failed registration cannot retain its listener");
  reject = false;
  assert.ok(await controller.analyze(controller.snapshot(fixture.main)));
  const realFilename = path.join(disk.realpathSync.native(fixture.root), "main.py");
  const filename = realFilename.toLowerCase();
  const inputUri = Uri.file(process.platform === "win32" ? filename : realFilename).toString();
  const listener = active.get(filename);
  await fs.unlink(fixture.main.uri.fsPath);
  listener({ nlink: 0n }, { nlink: 0n });
  assert.deepEqual(changes, [inputUri], "A discovered disk source disappearing before the first stat still invalidates");
  const realpath = disk.realpathSync.native;
  t.mock.method(disk.realpathSync, "native", value => value.toLowerCase() === filename
    ? path.resolve(fixture.root, "..", "redirected.py") : realpath(value));
  const reads = [];
  const readFile = disk.readFileSync;
  t.mock.method(disk, "readFileSync", (...args) => { reads.push(args[0]); return readFile(...args); });
  listener({ nlink: 0n }, { nlink: 0n });
  assert.deepEqual(changes, [inputUri, null], "Redirected input requests a new capture without trusting its contents");
  assert.deepEqual(reads, [], "The disk-event fingerprint must not read a redirected file");
  assert.equal(active.size, 0);
  listener();
  assert.equal(changes.length, 2);
});

test("workspace folder watchers follow additions and removals and refresh closed source/config changes", async t => {
  const fixture = await setup(t);
  const added = await setup(t);
  const { workspace } = fixture.vscode;
  const originalFolder = workspace.workspaceFolders[0];
  const addedFolder = added.vscode.workspace.workspaceFolders[0];
  const dependency = path.join(added.root, "dependency.py");
  await fs.writeFile(dependency, 'from graphql_desk import gql\nq=gql("{beforeMissing}")\n');
  workspace.textDocuments.push(added.main);
  workspace.findFiles = async pattern => pattern.base === addedFolder
    ? [Uri.file(path.join(added.root, "main.py")), Uri.file(dependency)]
    : [Uri.file(path.join(fixture.root, "main.py"))];
  let invalidations = 0;
  let refresh;
  let active;
  const controller = createAnalysisController(fixture.vscode, { changed() {
    invalidations += 1;
    if (active) refresh = controller.analyze(controller.snapshot(active));
  } });
  t.after(() => controller.dispose());
  const broad = () => fixture.watchers.filter(watcher => watcher.pattern.pattern === "**/*");
  assert.equal(broad().length, 1);
  const originalWatch = broad()[0];
  active = added.main;
  workspace.workspaceFolders.push(addedFolder);
  fixture.events.onDidChangeWorkspaceFolders.fire({ added: [addedFolder], removed: [] });
  const first = await refresh; assert.ok(first);
  const addedWatch = broad().find(watcher => watcher.pattern.base === addedFolder);
  assert.ok(addedWatch); assert.equal(originalWatch.disposals, 0);
  fixture.events.onDidChangeWorkspaceFolders.fire({ added: [], removed: [] });
  await refresh;
  assert.equal(broad().length, 2, "Unchanged folders keep their watcher");
  let beforeGeneratedStub = invalidations;
  const generatedStub = path.join(added.root, ".graphql-desk", "typings", "graphql_desk_generated", "default.pyi");
  await fs.mkdir(path.dirname(generatedStub), { recursive: true });
  await fs.writeFile(generatedStub, renderOwnedAnalysisArtifact("graphql_desk_generated", "default.pyi", "class Generated: ...\n"));
  addedWatch.fire(Uri.file(generatedStub));
  assert.equal(invalidations, beforeGeneratedStub, "Generated typing files must not invalidate the capture that produced them");
  const generatedDocument = { uri: Uri.file(generatedStub) };
  fixture.events.onDidOpenTextDocument.fire(generatedDocument);
  fixture.events.onDidChangeTextDocument.fire({ document: generatedDocument, contentChanges: [{ rangeOffset: 0, rangeLength: 1 }] });
  fixture.events.onDidSaveTextDocument.fire(generatedDocument);
  fixture.events.onDidCloseTextDocument.fire(generatedDocument);
  assert.equal(invalidations, beforeGeneratedStub, "Pylance opening generated typing files must not invalidate their own capture");
  const internalDocument = { uri: { scheme: "vscode-userdata", fsPath: "", toString: () => "vscode-userdata:/input-0" } };
  fixture.events.onDidOpenTextDocument.fire(internalDocument);
  fixture.events.onDidChangeTextDocument.fire({ document: internalDocument, contentChanges: [{ rangeOffset: 0, rangeLength: 1 }] });
  fixture.events.onDidCloseTextDocument.fire(internalDocument);
  assert.equal(invalidations, beforeGeneratedStub, "VS Code internal documents must not invalidate GraphQL analysis");
  const externalDocument = fixture.document("../external-library.pyi");
  fixture.events.onDidOpenTextDocument.fire(externalDocument);
  fixture.events.onDidChangeTextDocument.fire({ document: externalDocument, contentChanges: [{ rangeOffset: 0, rangeLength: 1 }] });
  fixture.events.onDidCloseTextDocument.fire(externalDocument);
  assert.equal(invalidations, beforeGeneratedStub, "Documents outside workspaces must not invalidate GraphQL analysis");
  generatedDocument.isDirty = true;
  fixture.events.onDidChangeTextDocument.fire({ document: generatedDocument, contentChanges: [{ rangeOffset: 0, rangeLength: 1 }] });
  assert.equal(invalidations, beforeGeneratedStub + 1, "Editing a generated buffer still invalidates analysis");
  await refresh;
  beforeGeneratedStub = invalidations;
  await fs.unlink(generatedStub);
  addedWatch.fire(Uri.file(generatedStub));
  assert.equal(invalidations, beforeGeneratedStub, "Deleting an observed GraphQL Desk-owned stub must not invalidate its cleanup");
  fixture.stubPath("custom_stubs");
  const generatedStage = path.join(added.root, "custom_stubs", "graphql_desk_generated", ".graphql-desk-123e4567-e89b-12d3-a456-426614174000.tmp");
  await fs.mkdir(path.dirname(generatedStage), { recursive: true });
  await fs.writeFile(generatedStage, renderOwnedAnalysisArtifact("graphql_desk_generated", "default.pyi", "class Generated: ...\n"));
  addedWatch.fire(Uri.file(generatedStage));
  assert.equal(invalidations, beforeGeneratedStub, "Generated staging files must not invalidate their own publication");
  await fs.unlink(generatedStage);
  addedWatch.fire(Uri.file(generatedStage));
  assert.equal(invalidations, beforeGeneratedStub, "Deleting an observed GraphQL Desk staging file must not invalidate its rename");
  const foreignStage = path.join(path.dirname(generatedStage), ".graphql-desk-not-a-uuid.tmp");
  await fs.writeFile(foreignStage, "preserve me\n");
  addedWatch.fire(Uri.file(foreignStage));
  assert.equal(invalidations, beforeGeneratedStub + 1, "Unrecognized temporary files under a custom stubPath remain observed");
  await refresh;
  const beforeCustomArtifact = invalidations;
  const customGeneratedStub = path.join(added.root, "custom_stubs", "graphql_desk_generated", "default.pyi");
  await fs.writeFile(customGeneratedStub, renderOwnedAnalysisArtifact("graphql_desk_generated", "default.pyi", "class Generated: ...\n"));
  addedWatch.fire(Uri.file(customGeneratedStub));
  assert.equal(invalidations, beforeCustomArtifact, "Final artifacts under a custom stubPath remain recognized");
  const beforeSourceEdit = invalidations;
  addedWatch.fire(Uri.file(dependency));
  assert.equal(invalidations, beforeSourceEdit + 1, "Ordinary Python edits still invalidate analysis");
  await refresh;
  const settingsFile = path.join(added.root, ".vscode", "settings.json");
  await fs.mkdir(path.dirname(settingsFile), { recursive: true });
  await fs.writeFile(settingsFile, '{"python.analysis.stubPath":".graphql-desk/typings/generations/g-1"}\n');
  addedWatch.fire(Uri.file(settingsFile));
  assert.equal(invalidations, beforeGeneratedStub + 2, "VS Code workspace settings writes are observed through configuration events, not as source changes");
  const generationDir = path.join(added.root, ".graphql-desk", "typings", "generations", `g-1-${"a".repeat(8)}-${"b".repeat(4)}-${"c".repeat(4)}-${"d".repeat(4)}-${"e".repeat(12)}`);
  await fs.mkdir(generationDir, { recursive: true });
  const oldActiveGeneration = path.join(path.dirname(generationDir), "g-0-01234567-89ab-cdef-0123-456789abcdef");
  await fs.mkdir(oldActiveGeneration, { recursive: true });
  fixture.stubPath(path.relative(added.root, oldActiveGeneration));
  const nestedGenerationDir = path.join(generationDir, "graphql_desk_generated");
  await fs.mkdir(nestedGenerationDir);
  addedWatch.fire(Uri.file(nestedGenerationDir));
  const typedMarkerDir = path.join(generationDir, "graphql_desk-stubs");
  await fs.mkdir(typedMarkerDir);
  const typedMarker = path.join(typedMarkerDir, "py.typed");
  const typedMarkerBytes = Buffer.from("partial\n");
  await fs.writeFile(typedMarker, typedMarkerBytes);
  const completeManifest = path.join(generationDir, "complete.json");
  await fs.writeFile(completeManifest, JSON.stringify({ padding: "x".repeat(5000), owner: "graphql-desk-analysis-generation",
    format: 2, files: [{ path: "graphql_desk-stubs/py.typed", sha256: createHash("sha256").update(typedMarkerBytes).digest("hex") }] }));
  addedWatch.fire(Uri.file(typedMarker));
  assert.equal(invalidations, beforeGeneratedStub + 2,
    "Format 2 py.typed is recognized before its directory events arrive");
  const generationArtifact = path.join(generationDir, "graphql_desk_generated", "default.pyi");
  await fs.writeFile(generationArtifact, renderOwnedAnalysisArtifact("graphql_desk_generated", "default.pyi", "class Generated: ...\n"));
  addedWatch.fire(Uri.file(generationDir));
  addedWatch.fire(Uri.file(generationArtifact));
  assert.equal(invalidations, beforeGeneratedStub + 2, "Immutable generation creation and contents must not self-invalidate the source snapshot");
  const liveGeneration = path.join(added.root, ".graphql-desk", "live-typings", "generations", `g-1-${"a".repeat(8)}-${"b".repeat(4)}-${"c".repeat(4)}-${"d".repeat(4)}-${"e".repeat(12)}`);
  fixture.stubPath(path.relative(added.root, liveGeneration));
  const liveGenerationDir = path.join(liveGeneration, "graphql_desk_generated");
  await fs.mkdir(liveGenerationDir, { recursive: true });
  const liveGenerationStage = path.join(liveGenerationDir, ".graphql-desk-123e4567-e89b-12d3-a456-426614174000.tmp");
  await fs.writeFile(liveGenerationStage, "");
  addedWatch.fire(Uri.file(liveGenerationStage));
  fixture.events.onDidOpenTextDocument.fire({ uri: Uri.file(liveGenerationStage) });
  fixture.events.onDidChangeTextDocument.fire({ document: { uri: Uri.file(liveGenerationStage), isDirty: true }, contentChanges: [{ rangeOffset: 0, rangeLength: 0 }] });
  assert.equal(invalidations, beforeGeneratedStub + 2, "Live project-config generation files do not invalidate their own analysis");
  const liveGenerated = path.join(liveGenerationDir, "default.pyi");
  const liveGeneratedBytes = renderOwnedAnalysisArtifact("graphql_desk_generated", "default.pyi", "class Generated: ...\n");
  await fs.writeFile(liveGenerated, liveGeneratedBytes);
  await fs.writeFile(path.join(liveGeneration, "complete.json"), JSON.stringify({ owner: "graphql-desk-analysis-generation", format: 2,
    files: [{ path: "graphql_desk_generated/default.pyi", sha256: createHash("sha256").update(liveGeneratedBytes).digest("hex") }] }));
  addedWatch.fire(Uri.file(liveGeneration));
  addedWatch.fire(Uri.file(liveGenerationDir));
  addedWatch.fire(Uri.file(liveGenerated));
  addedWatch.fire(Uri.file(path.join(liveGeneration, "complete.json")));
  assert.equal(invalidations, beforeGeneratedStub + 2,
    "Complete live-typings generations and generated files do not restart their own publication");
  const projectOwnerRecord = path.join(added.root, ".graphql-desk", "live-typings", "project-config-owner.json");
  await fs.writeFile(projectOwnerRecord, "{}\n");
  addedWatch.fire(Uri.file(projectOwnerRecord));
  fixture.events.onDidOpenTextDocument.fire({ uri: Uri.file(projectOwnerRecord) });
  assert.equal(invalidations, beforeGeneratedStub + 2,
    "The durable project-config owner record does not invalidate the analysis that writes it");
  const generationStage = path.join(nestedGenerationDir, ".graphql-desk-123e4567-e89b-12d3-a456-426614174000.tmp");
  await fs.writeFile(generationStage, "");
  addedWatch.fire(Uri.file(generationStage));
  fixture.events.onDidOpenTextDocument.fire({ uri: Uri.file(generationStage) });
  fixture.events.onDidChangeTextDocument.fire({ document: { uri: Uri.file(generationStage), isDirty: true }, contentChanges: [{ rangeOffset: 0, rangeLength: 0 }] });
  assert.equal(invalidations, beforeGeneratedStub + 2, "GraphQL Desk staging files in an immutable generation must not restart analysis while being written");
  const generationArtifactStage = path.join(nestedGenerationDir, ".graphql-desk-123e4567-e89b-12d3-a456-426614174000-50c7a130a8b02154-d4f20a36-1a8b-4376-954d-c39ec7eca38e.tmp");
  await fs.writeFile(generationArtifactStage, "partial generated bytes\n");
  addedWatch.fire(Uri.file(generationArtifactStage));
  assert.equal(invalidations, beforeGeneratedStub + 2, "Generation-owned artifact staging files must not self-invalidate before their manifest exists");
  const manifestStage = path.join(generationDir, ".graphql-desk-123e4567-e89b-12d3-a456-426614174000-manifest-123e4567-e89b-12d3-a456-426614174001.tmp");
  await fs.writeFile(manifestStage, "partial manifest bytes\n");
  addedWatch.fire(Uri.file(manifestStage));
  assert.equal(invalidations, beforeGeneratedStub + 2, "Generation manifest staging files must not self-invalidate before atomic publication");
  const stagingId = `g-2-${"f".repeat(8)}-${"a".repeat(4)}-${"b".repeat(4)}-${"c".repeat(4)}-${"d".repeat(12)}`;
  const stagingMarker = path.join(path.dirname(generationDir), `${stagingId}.staging.json`);
  await fs.writeFile(stagingMarker, "{}");
  addedWatch.fire(Uri.file(stagingMarker));
  assert.equal(invalidations, beforeGeneratedStub + 2, "Owned generation staging markers must not invalidate their own publication");
  const markerTemporary = path.join(path.dirname(generationDir), ".graphql-desk-123e4567-e89b-12d3-a456-426614174000-marker-123e4567-e89b-12d3-a456-426614174001.tmp");
  await fs.writeFile(markerTemporary, "{}");
  addedWatch.fire(Uri.file(markerTemporary));
  fixture.events.onDidOpenTextDocument.fire({ uri: Uri.file(markerTemporary) });
  fixture.events.onDidChangeTextDocument.fire({ document: { uri: Uri.file(markerTemporary), isDirty: true }, contentChanges: [{ rangeOffset: 0, rangeLength: 0 }] });
  assert.equal(invalidations, beforeGeneratedStub + 2, "Owned marker temporary files must not invalidate their own publication");
  const foreignMarkerTemporary = path.join(path.dirname(generationDir), ".graphql-desk-not-a-uuid-marker-not-a-uuid.tmp");
  await fs.writeFile(foreignMarkerTemporary, "preserve me\n");
  addedWatch.fire(Uri.file(foreignMarkerTemporary));
  assert.equal(invalidations, beforeGeneratedStub + 3, "Unrecognized marker temporary files remain visible to the broad watcher");
  addedWatch.fire(Uri.file(completeManifest));
  assert.equal(invalidations, beforeGeneratedStub + 3, "Large owned generation manifests are read completely before classifying watcher events");
  const foreignGenerationFile = path.join(generationDir, "foreign.txt");
  await fs.writeFile(foreignGenerationFile, "preserve me\n");
  addedWatch.fire(Uri.file(foreignGenerationFile));
  assert.equal(invalidations, beforeGeneratedStub + 4, "Foreign files under a generation remain visible to the broad watcher");
  const settingsDocument = { uri: Uri.file(settingsFile) };
  fixture.events.onDidOpenTextDocument.fire(settingsDocument);
  fixture.events.onDidChangeTextDocument.fire({ document: settingsDocument });
  fixture.events.onDidSaveTextDocument.fire(settingsDocument);
  fixture.events.onDidCloseTextDocument.fire(settingsDocument);
  assert.equal(invalidations, beforeGeneratedStub + 4, "GraphQL Desk settings writes do not invalidate through text-document events");
  fixture.events.onDidChangeConfiguration.fire({ affectsConfiguration: section => section === "graphqlDesk" });
  assert.equal(invalidations, beforeGeneratedStub + 5, "GraphQL Desk settings changes still invalidate analysis through VS Code's configuration event");
  await refresh;
  fixture.events.onDidChangeConfiguration.fire({ affectsConfiguration: section => section === "python.analysis.stubPath" || section === "python" });
  assert.equal(invalidations, beforeGeneratedStub + 5, "Managed stub-path rotations are handled by the extension without invalidating their own publication");
  fixture.events.onDidChangeConfiguration.fire({ affectsConfiguration: section => section === "python" });
  assert.equal(invalidations, beforeGeneratedStub + 6, "Python configuration changes still invalidate through VS Code's configuration event");
  await refresh;
  const beforeUserStub = invalidations;
  const userStub = path.join(added.root, "third_party", "custom.pyi");
  await fs.mkdir(path.dirname(userStub), { recursive: true });
  await fs.writeFile(userStub, "class UserOwned: ...\n");
  addedWatch.fire(Uri.file(userStub));
  assert.equal(invalidations, beforeUserStub + 1, "User-owned typing stubs remain observed");
  const beforeUserDocument = invalidations;
  fixture.events.onDidOpenTextDocument.fire({ uri: Uri.file(userStub) });
  assert.equal(invalidations, beforeUserDocument + 1, "Opening user-owned stubs still invalidates analysis");
  await fs.unlink(userStub);
  addedWatch.fire(Uri.file(userStub));
  assert.equal(invalidations, beforeUserStub + 3, "Deleting a user-owned typing stub remains observed");
  const features = createLanguageFeatures(controller);
  const dependencySnapshot = bundle => {
    const module = bundle.request.modules.find(item => item.module === "dependency");
    return { uri: module.uri, version: module.version, text: module.source };
  };
  assert.ok((await features.diagnostics(dependencySnapshot(first))).some(item => /beforeMissing/.test(item.message)));
  await fs.writeFile(dependency, 'from graphql_desk import gql\nq=gql("{afterMissing @local}")\n');
  const beforeEdit = invalidations;
  addedWatch.fire(Uri.file(dependency));
  assert.equal(invalidations, beforeEdit + 1);
  const second = await refresh; assert.ok(second); assert.notEqual(second, first);
  const changed = await features.diagnostics(dependencySnapshot(second));
  assert.ok(changed.some(item => /afterMissing/.test(item.message)));
  assert.ok(changed.some(item => /Unknown directive.*local/.test(item.message)));
  assert.equal(changed.some(item => /beforeMissing/.test(item.message)), false);
  await fs.appendFile(path.join(added.root, "pyproject.toml"), 'client_directives=["local"]\n');
  addedWatch.fire(Uri.file(path.join(added.root, "pyproject.toml")));
  const third = await refresh; assert.ok(third);
  assert.equal((await features.diagnostics(dependencySnapshot(third))).some(item => /Unknown directive.*local/.test(item.message)), false);
  active = undefined;
  workspace.workspaceFolders = [originalFolder];
  fixture.events.onDidChangeWorkspaceFolders.fire({ added: [], removed: [addedFolder] });
  assert.equal(addedWatch.disposals, 1);
  const afterRemoval = invalidations;
  addedWatch.fire(Uri.file(dependency));
  assert.equal(invalidations, afterRemoval, "Removed watcher subscriptions cannot refresh");
  controller.dispose();
  assert.equal(originalWatch.disposals, 1);
  assert.equal(addedWatch.disposals, 1);
});

test("fresh enumeration and document inventory reject stale success even without watcher events", async t => {
  const fixture = await setup(t);
  const controller = createAnalysisController(fixture.vscode); t.after(() => controller.dispose());
  const snapshot = controller.snapshot(fixture.main);
  const first = await controller.analyze(snapshot);
  assert.ok(first);
  await fs.writeFile(path.join(fixture.root, "dependency.py"), "x=1\n");
  fixture.candidates(["main.py", "dependency.py"]);
  assert.equal(await controller.isCurrent(first, snapshot), false);
  const second = await controller.analyze(snapshot); assert.ok(second);
  assert.equal(second.request.modules.length, 2);
  const extra = fixture.document("dependency.py", "x=2\n"); extra.isDirty = true;
  assert.equal(await controller.isCurrent(second, snapshot), false);
  const third = await controller.analyze(snapshot); assert.ok(third);
  fixture.vscode.workspace.textDocuments.splice(fixture.vscode.workspace.textDocuments.indexOf(extra), 1);
  fixture.events.onDidCloseTextDocument.fire(extra);
  assert.equal(await controller.isCurrent(third, snapshot), false);
});

test("late analysis results are rejected after any captured input changes", async t => {
  for (const input of ["source", "dependency", "schema", "project-config", "interpreter"]) {
    await t.test(input, async t => {
      const fixture = await setup(t);
      const dependencyPath = path.join(fixture.root, "dependency.py");
      const schema = fixture.document("schema.graphql", "type Query { title: String }");
      schema.isDirty = true;
      if (input === "dependency") {
        await fs.writeFile(dependencyPath, 'from graphql_desk import gql\nq=gql("{title}")\n');
        fixture.candidates(["main.py", "dependency.py"]);
      }
      if (input === "interpreter") fixture.interpreter("");
      const python = input === "interpreter" ? pythonApi(fixture) : null;
      let release;
      let started;
      const ready = new Promise(resolve => { started = resolve; });
      const held = new Promise(resolve => { release = resolve; });
      t.after(() => release());
      let workerCreates = 0;
      const controller = createAnalysisController(fixture.vscode, {
        workerFactory: options => {
          workerCreates += 1;
          const worker = createAnalysisWorker(options);
          let delayed = false;
          return { ...worker, async request(message, requestOptions) {
            const reply = await worker.request(message, requestOptions);
            if (message.op === "analyze" && !delayed) {
              delayed = true;
              started();
              await held;
            }
            return reply;
          } };
        },
      });
      t.after(() => controller.dispose());

      const snapshot = controller.snapshot(fixture.main);
      const pending = controller.analyze(snapshot);
      await ready;

      if (input === "source") {
        fixture.main.text += "# changed during analysis\n";
        fixture.main.version += 1;
        fixture.main.isDirty = true;
        fixture.events.onDidChangeTextDocument.fire({ document: fixture.main });
      } else if (input === "dependency") {
        const watcher = fixture.watchers.find(item => item.pattern.pattern === "**/*");
        assert.ok(watcher, "the dependency watcher is installed before analysis");
        await fs.writeFile(dependencyPath, 'from graphql_desk import gql\nq=gql("{missing}")\n');
        watcher.fire(Uri.file(dependencyPath));
      } else if (input === "schema") {
        schema.text = "type Query { title: Int }";
        schema.version += 1;
        const watcher = fixture.watchers.find(item => item.pattern.pattern.endsWith(".graphql"));
        assert.ok(watcher, "the required schema watcher is installed before analysis");
        watcher.fire(schema.uri);
      } else if (input === "project-config") {
        const configPath = path.join(fixture.root, "pyproject.toml");
        await fs.appendFile(configPath, 'client_directives=["local"]\n');
        fixture.watchers.find(item => item.pattern.pattern === "**/*").fire(Uri.file(configPath));
      } else {
        python.select({ id: "changed", path: "changed-environment" });
      }
      release();

      assert.equal(await pending, null, "the old worker response cannot become current after invalidation");
      const fresh = await controller.analyze(controller.snapshot(fixture.main));
      assert.ok(fresh, "a later analysis can publish under the changed inputs");
      if (input === "source") {
        assert.match(fresh.request.modules.find(module => module.module === "main").source, /changed during analysis/);
      } else if (input === "dependency") {
        assert.equal(fresh.request.modules.find(module => module.module === "dependency").source,
          'from graphql_desk import gql\nq=gql("{missing}")\n');
      } else if (input === "schema") {
        assert.equal(fresh.request.schemas[0].text, "type Query { title: Int }");
      } else if (input === "project-config") {
        assert.deepEqual(controller.clientDirectives(fresh, fresh.response.calls[0]), ["local"]);
      } else if (input === "interpreter") {
        assert.equal(workerCreates, 2, "a changed interpreter gets a fresh worker");
      }
    });
  }
});

test("in-flight artifacts abort and cannot publish after dependency, schema, config or interpreter changes", async t => {
  for (const input of ["dependency", "schema", "project-config", "interpreter"]) {
    await t.test(input, async t => {
      const fixture = await setup(t);
      const dependencyPath = path.join(fixture.root, "dependency.py");
      const schema = fixture.document("schema.graphql", "type Query { title: String }");
      schema.isDirty = true;
      if (input === "dependency") {
        await fs.writeFile(dependencyPath, 'from graphql_desk import gql\nq=gql("{title}")\n');
        fixture.candidates(["main.py", "dependency.py"]);
      }
      if (input === "interpreter") fixture.interpreter("");
      const api = input === "interpreter" ? pythonApi(fixture) : null;
      let started;
      const ready = new Promise(resolve => { started = resolve; });
      let artifactCalls = 0;
      let signal;
      let requestObservedAbort = false;
      const controller = createAnalysisController(fixture.vscode, { workerFactory: options => {
        const worker = createAnalysisWorker(options);
        return { ...worker, async request(message, requestOptions) {
          if (message.op === "artifacts" && artifactCalls++ === 0) {
            signal = requestOptions?.signal;
            started();
            return new Promise((_, reject) => signal.addEventListener("abort", () => {
              requestObservedAbort = signal.aborted;
              reject(new Error("Worker request aborted"));
            }, { once: true }));
          }
          return worker.request(message, requestOptions);
        } };
      } });
      t.after(() => controller.dispose());

      const snapshot = controller.snapshot(fixture.main);
      assert.ok(await controller.analyze(snapshot));
      const pending = controller.artifacts(snapshot);
      await ready;
      if (input === "dependency") {
        await fs.writeFile(dependencyPath, 'from graphql_desk import gql\nq=gql("{missing}")\n');
        fixture.watchers.find(item => item.pattern.pattern === "**/*").fire(Uri.file(dependencyPath));
      } else if (input === "schema") {
        schema.text = "type Query { title: Int }";
        schema.version += 1;
        fixture.watchers.find(item => item.pattern.pattern.endsWith(".graphql")).fire(schema.uri);
      } else if (input === "project-config") {
        const configPath = path.join(fixture.root, "pyproject.toml");
        await fs.appendFile(configPath, 'client_directives=["local"]\n');
        fixture.watchers.find(item => item.pattern.pattern === "**/*").fire(Uri.file(configPath));
      } else {
        api.select({ id: "changed", path: "changed-environment" });
      }
      assert.equal(signal.aborted, true, `${input} invalidation reaches active artifact generation`);
      assert.equal(requestObservedAbort, true, "the pending worker-side request observes cancellation before resolving");
      assert.equal(await pending, null, "a late result from the invalidated artifact request cannot publish");

      const fresh = await controller.artifacts(controller.snapshot(fixture.main));
      assert.ok(fresh, "the current inputs can publish a fresh artifact generation");
      assert.equal(fresh.isCurrent(), true);
      if (input === "interpreter") assert.equal(artifactCalls, 2, "the changed interpreter receives a new artifact request");
    });
  }
});

test("untrusted, cancelled, superseded and disposed captures cannot publish or launch unauthorized workers", async t => {
  const fixture = await setup(t);
  let launches = 0;
  const controller = createAnalysisController(fixture.vscode, { workerFactory: options => { launches += 1; return createAnalysisWorker(options); } });
  const snapshot = controller.snapshot(fixture.main);
  fixture.vscode.workspace.isTrusted = false;
  assert.equal(await controller.analyze(snapshot), null); assert.equal(launches, 0);
  fixture.vscode.workspace.isTrusted = true;
  assert.equal(await controller.analyze(snapshot, { isCancellationRequested: true }), null);
  const pending = controller.analyze(snapshot);
  fixture.events.onDidChangeTextDocument.fire({ document: fixture.main });
  // Jobs queued before invalidation may start afterward, but never match an edited document.
  fixture.main.version += 1; fixture.main.text += "# edited\n"; fixture.main.isDirty = true;
  const result = await pending;
  if (result) assert.equal(result.request.modules[0].version, fixture.main.version);
  const current = await controller.analyze(controller.snapshot(fixture.main)); assert.ok(current);
  fixture.vscode.workspace.isTrusted = false;
  assert.equal(await controller.isCurrent(current, controller.snapshot(fixture.main)), false);
  controller.dispose();
  assert.equal(await controller.analyze(snapshot), null);
});

test("explicit remote refresh survives typing without HTTP, but changed credentials/config remove contexts", async t => {
  let requests = 0;
  const payload = JSON.stringify({ data: introspectionFromSchema(buildSchema("type Query { title: String }")) });
  const server = http.createServer((request, response) => { requests += 1; request.resume(); response.setHeader("Content-Type", "application/json"); response.setHeader("Content-Length", Buffer.byteLength(payload)); response.end(payload); });
  await new Promise(resolve => server.listen(0, "127.0.0.1", resolve));
  t.after(() => new Promise(resolve => server.close(resolve)));
  const fixture = await setup(t, `schema={url="http://127.0.0.1:${server.address().port}/graphql",headers={"X-Key"="\${env:TOKEN}"}}\n`);
  let environment = { TOKEN: "PRIVATE_SECRET", UNRELATED: "NEVER_SEND" };
  const messages = [];
  const statuses = [];
  const reports = [];
  const controller = createAnalysisController(fixture.vscode, { status: value => statuses.push(value), report: value => reports.push(value), environment: () => environment, workerFactory: options => {
    const worker = createAnalysisWorker(options);
    return { ...worker, request(message) { messages.push(message); return worker.request(message); } };
  } }); t.after(() => controller.dispose());
  let snapshot = controller.snapshot(fixture.main);
  assert.equal((await controller.analyze(snapshot)).request.schemas.length, 0); assert.equal(requests, 0);
  assert.equal(statuses.at(-1), "Schema unavailable");
  await controller.refresh(); assert.ok(requests > 0); assert.equal(reports.at(-1), "Schema refreshed.");
  const fetched = requests;
  let bundle = await controller.analyze(snapshot); assert.equal(bundle.request.schemas.length, 1);
  assert.equal(statuses.at(-1), null);
  fixture.main.text += "# typing\n"; fixture.main.version += 1; fixture.main.isDirty = true;
  fixture.events.onDidChangeTextDocument.fire({ document: fixture.main });
  snapshot = controller.snapshot(fixture.main);
  bundle = await controller.analyze(snapshot); assert.equal(bundle.request.schemas.length, 1); assert.equal(requests, fetched);
  const refresh = messages.find(message => message.op === "refresh");
  assert.deepEqual({ ...refresh.environment }, { TOKEN: "PRIVATE_SECRET" });
  assert.match(refresh.credential_revision, /^[0-9a-f-]{36}$/);
  assert.equal(JSON.stringify(messages.filter(message => message.op !== "refresh")).includes("PRIVATE_SECRET"), false);
  environment = { TOKEN: "CHANGED" };
  assert.equal(await controller.isCurrent(bundle, snapshot), false);
  bundle = await controller.analyze(snapshot); assert.equal(bundle.request.schemas.length, 0); assert.equal(requests, fetched);
  await controller.refresh(); assert.equal((await controller.analyze(snapshot)).request.schemas.length, 1);
  await fs.appendFile(path.join(fixture.root, "pyproject.toml"), 'client_directives=["local"]\n');
  fixture.events.onDidChangeConfiguration.fire();
  assert.equal((await controller.analyze(snapshot)).request.schemas.length, 0);
});

test("refresh does not offer stale remote targets when its queued capture is invalidated", async t => {
  const fixture = await setup(t, 'schema={url="http://127.0.0.1:1/graphql"}\n');
  let release;
  let started;
  const ready = new Promise(resolve => { started = resolve; });
  const held = new Promise(resolve => { release = resolve; });
  let holdNextPrepare = false;
  let choices = 0;
  fixture.vscode.window.showQuickPick = async values => { choices += 1; return values[0]; };
  const controller = createAnalysisController(fixture.vscode, { workerFactory: options => {
    const worker = createAnalysisWorker(options);
    return { ...worker, async request(message, settings) {
      const reply = await worker.request(message, settings);
      if (message.op === "prepare" && holdNextPrepare) { holdNextPrepare = false; started(); await held; }
      return reply;
    } };
  } }); t.after(() => controller.dispose());

  const snapshot = controller.snapshot(fixture.main);
  assert.ok(await controller.analyze(snapshot));
  controller.invalidate();
  holdNextPrepare = true;
  const active = controller.analyze(snapshot);
  await ready;
  const refresh = controller.refresh();
  await new Promise(resolve => setImmediate(resolve));
  const config = fixture.document("pyproject.toml", '[tool.graphql-desk]\nschema="schema.graphql"\nschema_output="graphql_desk_generated/default.pyi"\n');
  config.isDirty = true;
  fixture.events.onDidChangeTextDocument.fire({ document: config });
  release();

  assert.equal(await active, null);
  await refresh;
  assert.equal(choices, 0, "refresh must stop when its queued capture is stale");
});



test("opt-in polling reuses authenticated refresh, retains source-edit deadlines and stops on config disable", async t => {
  const headers = [];
  let fail = false;
  const payload = JSON.stringify({ data: introspectionFromSchema(buildSchema("type Query { title: String }")) });
  const server = http.createServer((request, response) => {
    headers.push(request.headers["x-key"]); request.resume(); response.setHeader("Content-Type", "application/json");
    if (fail) { response.statusCode = 503; response.end("{}"); } else response.end(payload);
  });
  await new Promise(resolve => server.listen(0, "127.0.0.1", resolve));
  t.after(() => { server.closeAllConnections(); return new Promise(resolve => server.close(resolve)); });
  const fixture = await setup(t, `schema={url="http://127.0.0.1:${server.address().port}/graphql",headers={"X-Key"="\${env:TOKEN}"}}\nschema_refresh_interval=2\n`);
  let environment = { TOKEN: "FIRST_PRIVATE" };
  let changes = 0;
  const reports = [];
  const pollReplies = [];
  const controller = createAnalysisController(fixture.vscode, { environment: () => environment, changed: () => { changes += 1; }, report: value => reports.push(value),
    workerFactory: options => {
      const worker = createAnalysisWorker(options);
      return { ...worker, async request(message, settings) {
        const reply = await worker.request(message, settings);
        if (message.op === "refresh" || message.op === "analyze") pollReplies.push({ op: message.op,
          status: reply.status, schemas: reply.request?.schemas?.length,
          unavailable: reply.unavailable_schemas?.length });
        return reply;
      } };
    } });
  t.after(() => controller.dispose());
  t.mock.timers.enable({ apis: ["setTimeout", "Date"], now: 1000 });
  const snapshot = () => controller.snapshot(fixture.main);
  assert.equal((await controller.analyze(snapshot())).request.schemas.length, 0);
  t.mock.timers.tick(1000);
  fixture.main.text += "# dirty source\n"; fixture.main.version += 1; fixture.main.isDirty = true;
  fixture.events.onDidChangeTextDocument.fire({ document: fixture.main });
  await controller.analyze(snapshot());
  assert.equal(headers.length, 0);
  t.mock.timers.tick(1000);
  await eventually(() => changes === 2);
  assert.ok(headers.length > 0);
  assert.ok(headers.every(value => value === "FIRST_PRIVATE"));
  const firstPolled = await controller.analyze(snapshot());
  assert.equal(firstPolled.request.schemas.length, 1, JSON.stringify(pollReplies));
  assert.deepEqual(reports, [], "Automatic refreshes do not display periodic notifications");
  const first = headers.length;
  environment = { TOKEN: "SECOND_PRIVATE" };
  t.mock.timers.tick(2000);
  await eventually(() => changes === 3);
  assert.ok(headers.length > first);
  assert.ok(headers.slice(first).every(value => value === "SECOND_PRIVATE"));
  fail = true;
  t.mock.timers.tick(2000);
  await eventually(() => changes === 4);
  assert.equal((await controller.analyze(snapshot())).request.schemas.length, 0, "A failed poll never reuses a last-good schema as current");
  const fetched = headers.length;
  const config = path.join(fixture.root, "pyproject.toml");
  await fs.writeFile(config, (await fs.readFile(config, "utf8")).replace("schema_refresh_interval=2", "schema_refresh_interval=0"));
  // No watcher event: the due poll must reprepare and honor the newly disabled setting.
  t.mock.timers.tick(2000);
  await eventually(() => changes === 5 && reports.length === 0);
  t.mock.timers.tick(20000);
  await new Promise(resolve => realTimeout(resolve, 30));
  assert.equal(headers.length, fetched);
});

test("polling waits for interactive work without losing its deadline and cancels active HTTP on retirement", async t => {
  let requests = 0;
  let heldResponse;
  const server = http.createServer((request, response) => { requests += 1; request.resume(); heldResponse = response; });
  await new Promise(resolve => server.listen(0, "127.0.0.1", resolve));
  t.after(() => { server.closeAllConnections(); return new Promise(resolve => server.close(resolve)); });
  const fixture = await setup(t, `schema="http://127.0.0.1:${server.address().port}/graphql"\nschema_refresh_interval=1\n`);
  let release;
  let entered;
  let hold = false;
  const ready = new Promise(resolve => { entered = resolve; });
  const gate = new Promise(resolve => { release = resolve; });
  let active = 0;
  let maximum = 0;
  const controller = createAnalysisController(fixture.vscode, { workerFactory: options => {
    const worker = createAnalysisWorker(options);
    return { ...worker, async request(message, settings) {
      active += 1; maximum = Math.max(maximum, active);
      try {
        if (hold && message.op === "prepare") { hold = false; entered(); await gate; }
        return await worker.request(message, settings);
      } finally { active -= 1; }
    } };
  } }); t.after(() => controller.dispose());
  t.mock.timers.enable({ apis: ["setTimeout", "Date"], now: 1000 });
  await controller.analyze(controller.snapshot(fixture.main));
  hold = true;
  fixture.main.text += "# edit\n"; fixture.main.version += 1; fixture.main.isDirty = true;
  const interactive = controller.analyze(controller.snapshot(fixture.main));
  await ready;
  t.mock.timers.tick(1000);
  assert.equal(requests, 0);
  release(); await interactive;
  t.mock.timers.tick(1);
  await eventually(() => requests > 0);
  assert.equal(maximum, 1);
  controller.dispose();
  await eventually(() => active === 0);
  const count = requests;
  heldResponse?.end("{}");
  t.mock.timers.tick(10000);
  assert.equal(requests, count);
});

test("huge finite polling intervals use safe timeout chunks and untrusted workspaces never fetch", async t => {
  const fixture = await setup(t, 'schema="https://example.invalid/graphql"\nschema_refresh_interval=1e308\n');
  const messages = [];
  const controller = createAnalysisController(fixture.vscode, { workerFactory: options => {
    const worker = createAnalysisWorker(options);
    return { ...worker, request(message, settings) { messages.push(message); return worker.request(message, settings); } };
  } }); t.after(() => controller.dispose());
  t.mock.timers.enable({ apis: ["setTimeout", "Date"], now: 1000 });
  await controller.analyze(controller.snapshot(fixture.main));
  t.mock.timers.tick(2_147_483_647);
  await new Promise(resolve => realTimeout(resolve, 20));
  assert.equal(messages.some(message => message.op === "refresh"), false);
  fixture.vscode.workspace.isTrusted = false;
  t.mock.timers.tick(2_147_483_647);
  assert.equal(messages.some(message => message.op === "refresh"), false);
  controller.dispose();
  t.mock.timers.tick(2_147_483_647);
});

test("polling revalidates increased intervals before HTTP and fairly refreshes each named schema", async t => {
  const requests = [];
  const payload = JSON.stringify({ data: introspectionFromSchema(buildSchema("type Query { title: String }")) });
  const server = http.createServer((request, response) => {
    requests.push(request.url); request.resume(); response.setHeader("Content-Type", "application/json"); response.end(payload);
  });
  await new Promise(resolve => server.listen(0, "127.0.0.1", resolve));
  t.after(() => { server.closeAllConnections(); return new Promise(resolve => server.close(resolve)); });
  const fixture = await setup(t);
  const config = path.join(fixture.root, "pyproject.toml");
  const text = `[tool.graphql-desk]\nschema_refresh_interval=1\n` + ["alpha", "beta"].map(name =>
    `[[tool.graphql-desk.schemas]]\nname="${name}"\nschema="http://127.0.0.1:${server.address().port}/${name}"\nschema_output="graphql_desk_generated/${name}.pyi"\n`).join("");
  await fs.writeFile(config, text);
  let changes = 0;
  const refreshed = [];
  const controller = createAnalysisController(fixture.vscode, { changed: () => { changes += 1; }, workerFactory: options => {
    const worker = createAnalysisWorker(options);
    return { ...worker, request(message, settings) {
      if (message.op === "refresh") refreshed.push(message.schema_name);
      return worker.request(message, settings);
    } };
  } }); t.after(() => controller.dispose());
  t.mock.timers.enable({ apis: ["setTimeout", "Date"], now: 1000 });
  const snapshot = controller.snapshot(fixture.main);
  await controller.analyze(snapshot);
  await fs.writeFile(config, text.replace("schema_refresh_interval=1", "schema_refresh_interval=10"));
  t.mock.timers.tick(1000);
  await controller.analyze(snapshot);
  assert.deepEqual(refreshed, [], "The old due callback cannot fetch under a longer current interval");
  assert.equal(changes, 1, "Observed configuration changes invalidate displayed analysis without another user request");
  t.mock.timers.tick(9999);
  assert.deepEqual(requests, []);
  t.mock.timers.tick(1);
  await eventually(() => changes === 2);
  t.mock.timers.tick(1);
  await eventually(() => changes === 3);
  assert.deepEqual(refreshed, ["alpha", "beta"]);
  assert.ok(requests.includes("/alpha") && requests.includes("/beta"));
  fixture.vscode.window.showQuickPick = async () => {
    t.mock.timers.tick(20000);
    await new Promise(resolve => realTimeout(resolve, 20));
    assert.deepEqual(refreshed, ["alpha", "beta"], "Foreground schema selection pauses automatic HTTP");
    return undefined;
  };
  await controller.refresh();
  assert.deepEqual(refreshed, ["alpha", "beta"], "Cancelling the foreground picker performs no refresh");
  t.mock.timers.tick(1);
  await eventually(() => changes === 4);
  assert.deepEqual(refreshed, ["alpha", "beta", "alpha"], "Cancelling a foreground picker resumes the existing polling deadline");
});

test("automatic HTTP is aborted on trust loss, interpreter changes and workspace retirement", async t => {
  for (const reason of ["trust", "interpreter", "workspace"]) {
    let requests = 0;
    let closed = false;
    const server = http.createServer(request => {
      requests += 1; request.resume(); request.socket.once("close", () => { closed = true; });
    });
    await new Promise(resolve => server.listen(0, "127.0.0.1", resolve));
    t.after(() => { server.closeAllConnections(); return new Promise(resolve => server.close(resolve)); });
    const fixture = await setup(t, `schema="http://127.0.0.1:${server.address().port}/graphql"\nschema_refresh_interval=1\n`);
    const controller = createAnalysisController(fixture.vscode);
    t.after(() => controller.dispose());
    t.mock.timers.enable({ apis: ["setTimeout", "Date"], now: 1000 });
    await controller.analyze(controller.snapshot(fixture.main));
    t.mock.timers.tick(1000);
    await eventually(() => requests > 0);
    if (reason === "trust") { fixture.vscode.workspace.isTrusted = false; controller.isTrusted(); }
    else if (reason === "interpreter") {
      fixture.interpreter(path.join(fixture.root, "missing-python.exe"));
      fixture.events.onDidChangeConfiguration.fire({ affectsConfiguration: () => true });
    } else {
      fixture.vscode.workspace.workspaceFolders = [];
      fixture.events.onDidChangeWorkspaceFolders.fire({ added: [], removed: [] });
    }
    await eventually(() => closed);
    const count = requests;
    t.mock.timers.tick(10000);
    assert.equal(requests, count, reason);
    controller.dispose();
    t.mock.timers.reset();
  }
});

test("explicit refresh preempts automatic HTTP while retaining normal foreground notifications", async t => {
  let requests = 0;
  let automaticClosed = false;
  const payload = JSON.stringify({ data: introspectionFromSchema(buildSchema("type Query { title: String }")) });
  const server = http.createServer((request, response) => {
    requests += 1; request.resume();
    if (requests === 1) { request.socket.once("close", () => { automaticClosed = true; }); return; }
    response.setHeader("Content-Type", "application/json"); response.end(payload);
  });
  await new Promise(resolve => server.listen(0, "127.0.0.1", resolve));
  t.after(() => { server.closeAllConnections(); return new Promise(resolve => server.close(resolve)); });
  const fixture = await setup(t, `schema="http://127.0.0.1:${server.address().port}/graphql"\nschema_refresh_interval=1\n`);
  const reports = [];
  const controller = createAnalysisController(fixture.vscode, { report: value => reports.push(value) });
  t.after(() => controller.dispose());
  t.mock.timers.enable({ apis: ["setTimeout", "Date"], now: 1000 });
  const snapshot = controller.snapshot(fixture.main);
  await controller.analyze(snapshot);
  t.mock.timers.tick(1000);
  await eventually(() => requests === 1);
  await controller.refresh();
  assert.equal(automaticClosed, true);
  assert.ok(requests > 1);
  assert.deepEqual(reports, ["Schema refreshed."]);
  assert.equal((await controller.analyze(snapshot)).request.schemas.length, 1);
});

test("cancelling a resolved automatic prepare cannot install its watchers before foreground refresh", async t => {
  const payload = JSON.stringify({ data: introspectionFromSchema(buildSchema("type Query { title: String }")) });
  const server = http.createServer((request, response) => { request.resume(); response.setHeader("Content-Type", "application/json"); response.end(payload); });
  await new Promise(resolve => server.listen(0, "127.0.0.1", resolve));
  t.after(() => { server.closeAllConnections(); return new Promise(resolve => server.close(resolve)); });
  const fixture = await setup(t, `schema="http://127.0.0.1:${server.address().port}/graphql"\nschema_refresh_interval=1\n`);
  const cancelledPath = path.join(fixture.root, "cancelled-schema.graphql");
  let foreground;
  let intercepted = false;
  const controller = createAnalysisController(fixture.vscode, { workerFactory: options => {
    const worker = createAnalysisWorker(options);
    return { ...worker, async request(message, settings) {
      const result = await worker.request(message, settings);
      if (message.op === "prepare" && settings?.signal && !intercepted) {
        intercepted = true;
        foreground = controller.refresh();
        await new Promise(resolve => setImmediate(resolve));
        assert.equal(settings.signal.aborted, true);
        return { ...result, local_schemas: [{ project: result.remote_schemas[0].project, schema_name: "default",
          path: cancelledPath, canonical_path: cancelledPath }] };
      }
      return result;
    } };
  } }); t.after(() => controller.dispose());
  t.mock.timers.enable({ apis: ["setTimeout", "Date"], now: 1000 });
  await controller.analyze(controller.snapshot(fixture.main));
  t.mock.timers.tick(1000);
  await eventually(() => foreground !== undefined);
  await foreground;
  assert.equal(fixture.watchers.some(watcher => watcher.pattern.pattern === "cancelled-schema.graphql"), false);
  assert.equal((await controller.analyze(controller.snapshot(fixture.main))).request.schemas.length, 1);
});

test("edit storms coalesce into one latest capture and cancelling one provider preserves shared work", async t => {
  const fixture = await setup(t);
  let release;
  let started;
  const ready = new Promise(resolve => { started = resolve; });
  const held = new Promise(resolve => { release = resolve; });
  let prepares = 0;
  const controller = createAnalysisController(fixture.vscode, { workerFactory: options => {
    const worker = createAnalysisWorker(options);
    return { ...worker, async request(message) {
      if (message.op === "prepare" && ++prepares === 1) { started(); await held; }
      return worker.request(message);
    } };
  } }); t.after(() => controller.dispose());
  const cancelled = { isCancellationRequested: false };
  const first = controller.analyze(controller.snapshot(fixture.main), cancelled);
  await ready;
  const pending = [];
  for (let index = 0; index < 50; index += 1) {
    fixture.main.version += 1; fixture.main.text += "# edit\n"; fixture.main.isDirty = true;
    fixture.events.onDidChangeTextDocument.fire({ document: fixture.main });
    pending.push(controller.analyze(controller.snapshot(fixture.main)));
  }
  cancelled.isCancellationRequested = true;
  release();
  assert.equal(await first, null);
  const results = await Promise.all(pending);
  assert.equal(prepares, 2);
  assert.ok(results.every(Boolean));
  assert.ok(results.every(result => result === results[0]));
  assert.equal(results[0].request.modules[0].version, fixture.main.version);
  assert.equal(await controller.isCurrent(results[0], controller.snapshot(fixture.main)), true);
  const token = { isCancellationRequested: false };
  const one = controller.analyze(controller.snapshot(fixture.main), token);
  const two = controller.analyze(controller.snapshot(fixture.main));
  token.isCancellationRequested = true;
  assert.equal(await one, null); assert.equal(await two, results[0]);
});

test("artifact publication and a newer analysis both survive queue coalescing", async t => {
  const fixture = await setup(t);
  let release;
  let started;
  const ready = new Promise(resolve => { started = resolve; });
  const held = new Promise(resolve => { release = resolve; });
  let currentCalls = 0;
  let artifactCalls = 0;
  let analyzeCalls = 0;
  const controller = createAnalysisController(fixture.vscode, { workerFactory: options => {
    const worker = createAnalysisWorker(options);
    return { ...worker, async request(message, requestOptions) {
      if (message.op === "current" && currentCalls++ === 0) { started(); await held; }
      if (message.op === "artifacts") artifactCalls += 1;
      if (message.op === "analyze") analyzeCalls += 1;
      return worker.request(message, requestOptions);
    } };
  } }); t.after(() => controller.dispose());

  const snapshot = controller.snapshot(fixture.main);
  const bundle = await controller.analyze(snapshot);
  assert.ok(bundle);
  const current = controller.isCurrent(bundle, snapshot);
  await ready;
  const artifacts = controller.artifacts(snapshot);
  await new Promise(resolve => setImmediate(resolve));
  const latestAnalysis = controller.analyze(snapshot);
  await new Promise(resolve => setImmediate(resolve));
  release();

  assert.equal(await current, true);
  const generated = await artifacts;
  assert.ok(generated?.ok, "artifact work queued behind current analysis is not silently discarded");
  assert.equal(await latestAnalysis, bundle, "the newer analysis still receives the coalesced result");
  assert.equal(artifactCalls, 1, "artifact work reaches the worker exactly once");
  assert.equal(analyzeCalls, 1, "the queued analysis reuses the cached bundle without another worker analysis");
  assert.equal(generated.isCurrent(), true, "the generated artifacts still belong to the live analysis ticket");
});

test("same-input provider analysis does not stale an equivalent generated typing snapshot", async t => {
  const fixture = await setup(t);
  const controller = createAnalysisController(fixture.vscode);
  t.after(() => controller.dispose());
  const snapshot = controller.snapshot(fixture.main);
  assert.ok(await controller.analyze(snapshot));
  const generated = await controller.artifacts(snapshot);
  assert.ok(generated?.isCurrent());
  assert.ok(await controller.analyze(snapshot));
  assert.equal(generated.isCurrent(), true, "an equivalent analysis preserves artifact eligibility");
});

test("an invalidated queued analysis is skipped before worker dispatch", async t => {
  const fixture = await setup(t);
  let release;
  let started;
  const ready = new Promise(resolve => { started = resolve; });
  const held = new Promise(resolve => { release = resolve; });
  let prepares = 0;
  const controller = createAnalysisController(fixture.vscode, { workerFactory: options => {
    const worker = createAnalysisWorker(options);
    return { ...worker, async request(message, settings) {
      const reply = await worker.request(message, settings);
      if (message.op === "prepare" && ++prepares === 1) { started(); await held; }
      return reply;
    } };
  } }); t.after(() => controller.dispose());

  const snapshot = controller.snapshot(fixture.main);
  const active = controller.analyze(snapshot);
  await ready;
  const queued = controller.analyze(snapshot);
  await new Promise(resolve => setImmediate(resolve));
  fixture.main.version += 1;
  fixture.main.text += "# newer source\n";
  fixture.main.isDirty = true;
  fixture.events.onDidChangeTextDocument.fire({ document: fixture.main });
  release();

  assert.equal(await active, null, "the in-flight capture cannot survive source invalidation");
  assert.equal(await queued, null, "queued work captured before invalidation is discarded");
  assert.equal(prepares, 1, "the stale queued job never reaches the worker");
  const current = await controller.analyze(controller.snapshot(fixture.main));
  assert.ok(current);
  assert.equal(current.request.modules[0].version, fixture.main.version);
  assert.equal(prepares, 2, "a fresh request still dispatches normally");
});

test("quarantine aborts active artifact work and sends the owned manifest to the matching worker", async t => {
  const fixture = await setup(t);
  let artifactStarted;
  const started = new Promise(resolve => { artifactStarted = resolve; });
  let invalidations = 0;
  let quarantineRequest;
  const controller = createAnalysisController(fixture.vscode, { workerFactory: options => {
    const worker = createAnalysisWorker(options);
    return { ...worker,
      invalidate() { invalidations += 1; worker.invalidate(); },
      request(message, settings) {
        if (message.op === "artifacts") return new Promise((_resolve, reject) => {
          artifactStarted();
          settings.signal.addEventListener("abort", () => reject(new Error("aborted")), { once: true });
        });
        if (message.op === "quarantine") { quarantineRequest = message; return Promise.resolve({ ok: true, id: message.id }); }
        return worker.request(message, settings);
      },
    };
  } }); t.after(() => controller.dispose());

  const snapshot = controller.snapshot(fixture.main);
  assert.ok(await controller.analyze(snapshot));
  const artifacts = controller.artifacts(snapshot);
  await started;
  const expectedWorkspace = await fs.realpath(fixture.root);
  const projects = [{ project: "p", generated_package: "generated", files: [], provenance: [] }];
  const invalidatedOwners = [{ module: { project: "p", module: "main", uri: "file:///main.py", version: 1 },
    codepoint: { start: 0, end: 8 }, utf16: { start: 0, end: 8 } }];
  const reply = await controller.quarantine({ workspace: fixture.root, projects, invalidated_owners: invalidatedOwners });

  assert.equal(await artifacts, null, "aborted artifact work cannot return a late generation");
  assert.ok(reply?.ok);
  assert.equal(quarantineRequest.workspace, process.platform === "win32" ? expectedWorkspace.toLowerCase() : expectedWorkspace);
  assert.equal(quarantineRequest.op, "quarantine");
  assert.deepEqual(quarantineRequest.projects, projects);
  assert.deepEqual(quarantineRequest.invalidated_owners, invalidatedOwners);
  assert.ok(invalidations > 0, "the old worker is terminated before quarantine is requested");
});

test("selective post-analysis quarantine keeps the current snapshot valid", async t => {
  const fixture = await setup(t);
  let invalidations = 0;
  const controller = createAnalysisController(fixture.vscode, { workerFactory: options => {
    const worker = createAnalysisWorker(options);
    return { ...worker, invalidate() { invalidations += 1; return worker.invalidate(); }, request(message, settings) {
      if (message.op === "quarantine") return Promise.resolve({ ok: true, id: message.id });
      return worker.request(message, settings);
    } };
  } });
  t.after(() => controller.dispose());

  const snapshot = controller.snapshot(fixture.main);
  const analysis = await controller.artifacts(snapshot);
  assert.ok(analysis);
  const reply = await controller.quarantine({ workspace: fixture.root,
    projects: [{ project: "p", generated_package: "generated", files: [], provenance: [] }],
    invalidated_owners: [], preserve_current: true });

  assert.ok(reply?.ok);
  assert.equal(invalidations, 0, "post-analysis quarantine must not kill the fresh snapshot's worker");
  assert.equal(analysis.isCurrent(), true, "selective quarantine must not stale the facts it just compared");
});

test("explicit refresh cancellation aborts the worker and cannot admit a late remote schema", async t => {
  let entered;
  const requested = new Promise(resolve => { entered = resolve; });
  const server = http.createServer(request => { request.resume(); entered(); });
  await new Promise(resolve => server.listen(0, "127.0.0.1", resolve));
  t.after(() => { server.closeAllConnections(); return new Promise(resolve => server.close(resolve)); });
  const fixture = await setup(t, `schema="http://127.0.0.1:${server.address().port}/graphql"\n`, { deferCleanup: true });
  const cancellation = event();
  const token = { isCancellationRequested: false, onCancellationRequested: cancellation.subscribe };
  fixture.vscode.ProgressLocation = { Notification: 15 };
  fixture.vscode.window.withProgress = async (options, action) => { assert.equal(options.cancellable, true); return action({}, token); };
  const controller = createAnalysisController(fixture.vscode);
  t.after(() => controller.dispose());
  // Register workspace cleanup after controller disposal: Node runs test after-hooks in registration order.
  t.after(fixture.cleanup);
  const refreshing = controller.refresh();
  await requested;
  token.isCancellationRequested = true; cancellation.fire();
  await refreshing;
  const bundle = await controller.analyze(controller.snapshot(fixture.main));
  assert.ok(bundle); assert.equal(bundle.request.schemas.length, 0);
});

test("interpreter switches and workspace removal dispose old workers without trusting old results", async t => {
  const fixture = await setup(t);
  let disposed = 0;
  const controller = createAnalysisController(fixture.vscode, { workerFactory: options => {
    const worker = createAnalysisWorker(options);
    return { ...worker, dispose() { disposed += 1; worker.dispose(); } };
  } }); t.after(() => controller.dispose());
  const snapshot = controller.snapshot(fixture.main);
  const first = await controller.analyze(snapshot); assert.ok(first);
  fixture.interpreter(path.join(fixture.root, "missing-python.exe"));
  fixture.events.onDidChangeConfiguration.fire({ affectsConfiguration: name => name === "graphqlDesk" });
  assert.equal(await controller.analyze(snapshot), null); assert.equal(disposed, 1);
  fixture.interpreter(python);
  fixture.events.onDidChangeConfiguration.fire({ affectsConfiguration: name => name === "graphqlDesk" });
  const repaired = await controller.analyze(snapshot); assert.ok(repaired); assert.equal(disposed, 2);
  assert.equal(await controller.isCurrent(first, snapshot), false);
  fixture.interpreter("");
  fixture.events.onDidChangeConfiguration.fire({ affectsConfiguration: name => name === "graphqlDesk" });
  assert.equal(disposed, 3);
  assert.equal(await controller.analyze(snapshot), null);
  fixture.interpreter("relative-python");
  assert.equal(await controller.analyze(snapshot), null);
  fixture.interpreter(python);
  assert.ok(await controller.analyze(snapshot));
  fixture.vscode.workspace.workspaceFolders = [];
  fixture.events.onDidChangeWorkspaceFolders.fire();
  assert.equal(disposed, 4);
  assert.equal(await controller.analyze(snapshot), null);
});

test("credentials changing during explicit network refresh cannot publish the old authenticated context", async t => {
  let environment = { TOKEN: "first" };
  const payload = JSON.stringify({ data: introspectionFromSchema(buildSchema("type Query { title: String }")) });
  const server = http.createServer((request, response) => { request.resume(); environment = { TOKEN: "changed-during-fetch" }; response.setHeader("Content-Type", "application/json"); response.end(payload); });
  await new Promise(resolve => server.listen(0, "127.0.0.1", resolve));
  t.after(() => new Promise(resolve => server.close(resolve)));
  const fixture = await setup(t, `schema={url="http://127.0.0.1:${server.address().port}/graphql",headers={"X-Key"="\${env:TOKEN}"}}\n`);
  const controller = createAnalysisController(fixture.vscode, { environment: () => environment }); t.after(() => controller.dispose());
  await controller.refresh();
  const bundle = await controller.analyze(controller.snapshot(fixture.main));
  assert.ok(bundle); assert.equal(bundle.request.schemas.length, 0);
});


test("a missed closed disk edit clears failed freshness so the very next analysis has fresh diagnostics", async t => {
  const fixture = await setup(t);
  const dependency = path.join(fixture.root, "dependency.py");
  await fs.writeFile(dependency, 'from graphql_desk import gql\nq=gql("{oldMissing}")\n');
  fixture.candidates(["main.py", "dependency.py"]);
  let analyses = 0;
  const controller = createAnalysisController(fixture.vscode, { workerFactory: options => {
    const worker = createAnalysisWorker(options);
    return { ...worker, request(message) { if (message.op === "analyze") analyses += 1; return worker.request(message); } };
  } }); t.after(() => controller.dispose());
  const snapshot = controller.snapshot(fixture.main);
  const first = await controller.analyze(snapshot); assert.ok(first);
  const edited = 'from graphql_desk import gql\nq=gql("{newMissing}")\n';
  await fs.writeFile(dependency, edited);
  assert.equal(await controller.isCurrent(first, snapshot), false);
  const fresh = await controller.analyze(snapshot);
  assert.ok(fresh); assert.notEqual(fresh, first);
  const module = fresh.request.modules.find(item => item.module === "dependency");
  assert.equal(module.source, edited);
  const features = createLanguageFeatures(controller);
  const diagnostics = await features.diagnostics({ uri: module.uri, version: module.version, text: module.source });
  assert.ok(diagnostics.some(item => /newMissing/.test(item.message)));
  assert.equal(diagnostics.some(item => /oldMissing/.test(item.message)), false);
  assert.equal(analyses, 2, "The next analysis repairs freshness without another failed provider cycle");
});


test("real worker retains associated unsaved sources through an aliased workspace and missing parents", async t => {
  const fixture = await setup(t);
  const alias = path.join(fixture.root, "workspace-alias");
  await fs.symlink(fixture.root, alias, process.platform === "win32" ? "junction" : "dir");
  const folder = fixture.vscode.workspace.workspaceFolders[0];
  folder.uri = Uri.file(alias);
  fixture.vscode.workspace.getWorkspaceFolder = value => value.fsPath.startsWith(alias + path.sep) ? folder : undefined;
  fixture.main.uri = Uri.file(path.join(alias, "main.py"));
  const unsaved = fixture.document("future/deep/new.py", 'from graphql_desk import gql\nq=gql("{title}")\n', "untitled");
  unsaved.uri = uri(pathToFileURL(path.join(alias, "future/deep/new.py")).href.replace(/^file:/, "untitled:"));
  const controller = createAnalysisController(fixture.vscode); t.after(() => controller.dispose());
  const snapshot = controller.snapshot(unsaved);
  assert.equal(fixture.vscode.workspace.getWorkspaceFolder(Uri.parse(snapshot.uri)), undefined, "Canonical identity differs from the lexical workspace URI");
  const bundle = await controller.analyze(snapshot); assert.ok(bundle);
  const mapped = controller.mapSnapshot(snapshot, bundle);
  assert.equal(bundle.request.modules.find(item => item.uri === mapped.uri).source, unsaved.text);
  assert.equal(controller.resolveUri(mapped.uri).toString(), unsaved.uri.toString());
  assert.equal(await controller.isCurrent(bundle, mapped), true);
  assert.ok(await controller.analyze(controller.snapshot(fixture.main)));
});


test("deepest canonical workspace owns a nested alias even when native lookup chooses its outer folder", async t => {
  const fixture = await setup(t);
  const nested = path.join(fixture.root, "nested");
  await fs.mkdir(nested);
  const alias = path.join(fixture.root, "nested-alias");
  await fs.symlink(nested, alias, process.platform === "win32" ? "junction" : "dir");
  await fs.writeFile(path.join(nested, "pyproject.toml"), '[tool.graphql-desk]\nschema="../schema.graphql"\nschema_output="graphql_desk_generated/default.pyi"\n');
  const source = 'from graphql_desk import gql\nq=gql("{title}")\n';
  await fs.writeFile(path.join(nested, "main.py"), source);
  const outer = fixture.vscode.workspace.workspaceFolders[0];
  const inner = { uri: Uri.file(alias) };
  fixture.vscode.workspace.workspaceFolders.push(inner);
  fixture.vscode.workspace.getWorkspaceFolder = () => outer;
  fixture.vscode.workspace.findFiles = async () => [Uri.file(path.join(alias, "main.py"))];
  const document = fixture.document("nested/main.py", source);
  document.uri = Uri.file(path.join(alias, "main.py"));
  let selectedWorkspace;
  const controller = createAnalysisController(fixture.vscode, { workerFactory: options => { selectedWorkspace = options.workspace; return createAnalysisWorker(options); } });
  t.after(() => controller.dispose());
  const snapshot = controller.snapshot(document);
  const bundle = await controller.analyze(snapshot); assert.ok(bundle);
  assert.equal(selectedWorkspace.toLowerCase(), (await fs.realpath(nested)).toLowerCase());
  assert.equal(await controller.isCurrent(bundle, controller.mapSnapshot(snapshot, bundle)), true);
  assert.equal(await controller.analyze({ uri: Uri.file(path.dirname(fixture.root)).toString(), version: 1, text: "" }), null);
});


test("two configured aliases for one canonical workspace reject ambiguous ownership without a worker", async t => {
  const fixture = await setup(t);
  const alias = path.join(fixture.root, "other-workspace");
  await fs.symlink(fixture.root, alias, process.platform === "win32" ? "junction" : "dir");
  const original = fixture.vscode.workspace.workspaceFolders[0];
  const alternate = { uri: Uri.file(alias) };
  fixture.vscode.workspace.workspaceFolders.push(alternate);
  fixture.vscode.workspace.getWorkspaceFolder = () => original;
  fixture.vscode.workspace.getConfiguration = (_section, folder) => ({ get(name, fallback) {
    if (name === "pythonPath") return python;
    if (name === "project") return { schema: folder.toString() === alternate.uri.toString() ? "alternate.graphql" : "schema.graphql", schema_output: "graphql_desk_generated/default.pyi" };
    return fallback;
  } });
  fixture.main.uri = Uri.file(path.join(alias, "main.py"));
  let launches = 0;
  const controller = createAnalysisController(fixture.vscode, { workerFactory: options => { launches += 1; return createAnalysisWorker(options); } });
  t.after(() => controller.dispose());
  assert.equal(await controller.analyze(controller.snapshot(fixture.main)), null);
  assert.equal(launches, 0);
});


test("blank override uses the official selected environment directory and caches only its current record", async t => {
  const fixture = await setup(t); fixture.interpreter("");
  const selected = pythonApi(fixture);
  let disposals = 0;
  const controller = createAnalysisController(fixture.vscode, { workerFactory: options => {
    const worker = createAnalysisWorker(options); return { ...worker, dispose() { disposals += 1; worker.dispose(); } };
  } }); t.after(() => controller.dispose());
  const snapshot = controller.snapshot(fixture.main);
  fixture.vscode.workspace.isTrusted = false;
  assert.equal(await controller.analyze(snapshot), null); assert.equal(selected.stats.activations, 0);
  fixture.vscode.workspace.isTrusted = true;
  const first = await controller.analyze(snapshot); assert.ok(first);
  assert.equal(await controller.isCurrent(first, snapshot), true);
  assert.equal(await controller.analyze(snapshot), first);
  assert.equal(selected.stats.activations, 1); assert.equal(selected.stats.resolutions, 1);
  assert.ok(selected.stats.resources.every(resource => resource === fixture.vscode.workspace.workspaceFolders[0].uri.toString()));
  selected.select({ id: "other", path: path.dirname(python) });
  assert.equal(disposals, 1, "Official WorkspaceFolder-shaped events evict immediately, before another provider request");
  const second = await controller.analyze(snapshot); assert.ok(second); assert.notEqual(second, first);
  assert.equal(selected.stats.resolutions, 2);
  selected.set({ id: "changed-without-event", path: "another-environment" });
  const third = await controller.analyze(snapshot); assert.ok(third); assert.notEqual(third, second);
  assert.equal(selected.stats.resolutions, 3);
  controller.dispose(); assert.equal(selected.stats.disposed, 1);
});

test("explicit overrides are authoritative and ignore Python selection events", async t => {
  const fixture = await setup(t);
  const selected = pythonApi(fixture);
  let disposals = 0;
  const controller = createAnalysisController(fixture.vscode, { workerFactory: options => {
    const worker = createAnalysisWorker(options); return { ...worker, dispose() { disposals += 1; worker.dispose(); } };
  } }); t.after(() => controller.dispose());
  const snapshot = controller.snapshot(fixture.main);
  assert.ok(await controller.analyze(snapshot)); assert.equal(selected.stats.activations, 0);
  fixture.interpreter("relative-python"); fixture.events.onDidChangeConfiguration.fire();
  assert.equal(await controller.analyze(snapshot), null); assert.equal(selected.stats.activations, 0);
  fixture.interpreter(""); fixture.events.onDidChangeConfiguration.fire();
  assert.ok(await controller.analyze(snapshot)); assert.equal(selected.stats.activations, 1);
  fixture.interpreter(python); fixture.events.onDidChangeConfiguration.fire();
  const overridden = await controller.analyze(snapshot); assert.ok(overridden);
  const before = disposals;
  selected.select({ id: "changed", path: "other-environment" });
  selected.changes.fire({ path: "global-change" });
  assert.equal(disposals, before); assert.equal(await controller.analyze(snapshot), overridden);
  assert.equal(selected.stats.resolutions, 1);
});

test("missing API or unresolved/non-file executables never launch a worker and can recover", async t => {
  const fixture = await setup(t); fixture.interpreter("");
  let launches = 0;
  const controller = createAnalysisController(fixture.vscode, { workerFactory: options => { launches += 1; return createAnalysisWorker(options); } }); t.after(() => controller.dispose());
  const snapshot = controller.snapshot(fixture.main);
  assert.equal(await controller.analyze(snapshot), null); assert.equal(launches, 0);
  let result;
  pythonApi(fixture, { resolve: () => Promise.resolve(result) });
  assert.equal(await controller.analyze(snapshot), null); assert.equal(launches, 0);
  result = { executable: { uri: { scheme: "remote", fsPath: python } } };
  assert.equal(await controller.analyze(snapshot), null); assert.equal(launches, 0);
  result = { executable: { uri: { scheme: "file", fsPath: "relative" } } };
  assert.equal(await controller.analyze(snapshot), null); assert.equal(launches, 0);
  result = { executable: { uri: Uri.file(python) } };
  assert.ok(await controller.analyze(snapshot)); assert.equal(launches, 1);
});

test("delayed Python activation cannot resolve or launch after trust loss, disposal or override changes", async t => {
  for (const mode of ["trust", "dispose", "override"]) {
    const fixture = await setup(t); fixture.interpreter("");
    let entered; const started = new Promise(resolve => { entered = resolve; });
    let release; const held = new Promise(resolve => { release = resolve; });
    const selected = pythonApi(fixture, { activate: () => { entered(); return held; } });
    let launches = 0;
    const controller = createAnalysisController(fixture.vscode, { workerFactory: options => { launches += 1; return createAnalysisWorker(options); } }); t.after(() => controller.dispose());
    const pending = controller.analyze(controller.snapshot(fixture.main)); await started;
    if (mode === "trust") fixture.vscode.workspace.isTrusted = false;
    if (mode === "dispose") controller.dispose();
    if (mode === "override") { fixture.interpreter(python); fixture.events.onDidChangeConfiguration.fire(); }
    release(selected.api);
    assert.equal(await pending, null, mode); assert.equal(launches, 0); assert.equal(selected.stats.resolutions, 0);
  }
});

test("late selected-environment resolutions cannot survive a switch, trust loss, disposal or config change", async t => {
  for (const mode of ["switch", "trust", "dispose", "override"]) {
    const fixture = await setup(t); fixture.interpreter("");
    let entered; const started = new Promise(resolve => { entered = resolve; });
    let release; const held = new Promise(resolve => { release = resolve; });
    const selected = pythonApi(fixture, { resolve: () => { entered(); return held; } });
    let launches = 0;
    const controller = createAnalysisController(fixture.vscode, { workerFactory: options => { launches += 1; return createAnalysisWorker(options); } }); t.after(() => controller.dispose());
    const pending = controller.analyze(controller.snapshot(fixture.main)); await started;
    if (mode === "switch") selected.select({ id: "changed", path: "new-environment" });
    if (mode === "trust") fixture.vscode.workspace.isTrusted = false;
    if (mode === "dispose") controller.dispose();
    if (mode === "override") { fixture.interpreter(python); fixture.events.onDidChangeConfiguration.fire(); }
    release({ executable: { uri: Uri.file(python) } });
    assert.equal(await pending, null, mode); assert.equal(launches, 0);
  }
});


test("selected-environment events evict only their affected workspace and unchanged other-folder caches survive", async t => {
  const first = await setup(t); const second = await setup(t);
  first.interpreter("");
  const folderA = first.vscode.workspace.workspaceFolders[0];
  const folderB = second.vscode.workspace.workspaceFolders[0];
  first.vscode.workspace.workspaceFolders.push(folderB);
  first.vscode.workspace.textDocuments.push(second.main);
  first.vscode.workspace.findFiles = async pattern => [Uri.file(path.join(pattern.base.uri.fsPath, "main.py"))];
  const selected = pythonApi(first);
  const choices = new Map([[folderA.uri.toString(), { id: "a", path: "environment-a" }], [folderB.uri.toString(), { id: "b", path: "environment-b" }]]);
  const resources = [];
  selected.api.environments.getActiveEnvironmentPath = resource => { resources.push(resource.toString()); return choices.get(resource.toString()); };
  selected.api.environments.resolveEnvironment = () => Promise.resolve({ executable: { uri: Uri.file(python) } });
  const disposals = [];
  const controller = createAnalysisController(first.vscode, { workerFactory: options => {
    const worker = createAnalysisWorker(options); return { ...worker, dispose() { disposals.push(options.workspace); worker.dispose(); } };
  } }); t.after(() => controller.dispose());
  const snapshotA = controller.snapshot(first.main); const snapshotB = controller.snapshot(second.main);
  const a = await controller.analyze(snapshotA); const b = await controller.analyze(snapshotB);
  assert.ok(a); assert.ok(b);
  choices.set(folderA.uri.toString(), { id: "new-a", path: "new-environment-a" });
  selected.changes.fire({ path: "new-environment-a", resource: folderA.uri });
  assert.equal(disposals.length, 1);
  assert.equal(disposals[0].toLowerCase(), (await fs.realpath(first.root)).toLowerCase());
  assert.equal(await controller.analyze(snapshotB), b);
  const newA = await controller.analyze(snapshotA); assert.ok(newA); assert.notEqual(newA, a);
  assert.ok(resources.includes(folderA.uri.toString())); assert.ok(resources.includes(folderB.uri.toString()));
  selected.changes.fire({ path: "global-change" });
  assert.equal(disposals.length, 3, "Global events immediately evict both blank-derived workers");
});

test("local schema watchers retire with their state while live external buffers remain observed", async t => {
  const external = await setup(t);
  const schemaPath = path.join(external.root, "schema.graphql");
  const fixture = await setup(t, `schema="${schemaPath.replaceAll("\\", "/")}"\n`);
  const schema = external.document("schema.graphql", "type Query { buffered: Int }");
  schema.isDirty = true;
  fixture.vscode.workspace.textDocuments.push(schema);
  fixture.main.text = 'from graphql_desk import gql\nq=gql("{buffered}")\n';
  fixture.main.version += 1; fixture.main.isDirty = true;
  const selected = pythonApi(fixture);
  let invalidations = 0;
  const controller = createAnalysisController(fixture.vscode, { changed() { invalidations += 1; } });
  t.after(() => controller.dispose());
  const snapshot = controller.snapshot(fixture.main);
  const schemaWatches = () => fixture.watchers.filter(watcher => watcher.pattern.pattern.endsWith(".graphql"));
  const live = () => schemaWatches().filter(watcher => !watcher.disposed);
  assert.ok(await controller.analyze(snapshot));
  assert.equal(live().length, 1);
  const first = live()[0];
  fixture.interpreter(""); fixture.events.onDidChangeConfiguration.fire();
  assert.equal(live().length, 0);
  let before = invalidations;
  first.fire(Uri.file(schemaPath));
  assert.equal(invalidations, before, "Retired override watcher cannot invalidate");
  assert.ok(await controller.analyze(snapshot));
  assert.equal(live().length, 1);
  const second = live()[0];
  selected.select({ id: "changed", path: "another-environment" });
  assert.equal(live().length, 0);
  before = invalidations;
  second.fire(Uri.file(schemaPath));
  assert.equal(invalidations, before, "Retired selected-environment watcher cannot invalidate");
  assert.ok(await controller.analyze(snapshot));
  assert.equal(live().length, 1, "Interpreter changes replace rather than accumulate schema watches");
  const third = live()[0];
  await fs.writeFile(schemaPath, "type Query { diskOnly: String }");
  before = invalidations;
  third.fire(Uri.file(schemaPath));
  assert.equal(invalidations, before + 1, "Live external schema changes still invalidate");
  const fresh = await controller.analyze(snapshot); assert.ok(fresh);
  assert.equal(fresh.request.schemas[0].text, schema.text, "Dirty external schema stays authoritative");
  assert.deepEqual(await createLanguageFeatures(controller).diagnostics(snapshot), []);
  const replacement = path.join(external.root, "replacement.graphql");
  const configPath = path.join(fixture.root, "pyproject.toml");
  const replacementConfig = `[tool.graphql-desk]\nschema="${replacement.replaceAll("\\", "/")}"\nschema_output="graphql_desk_generated/default.pyi"\n`;
  const projectWatch = fixture.watchers.find(watcher => watcher.pattern.pattern === "**/*");
  await fs.writeFile(configPath, replacementConfig);
  projectWatch.fire(Uri.file(configPath));
  assert.equal(await controller.analyze(snapshot), null);
  assert.equal(third.disposals, 1, "Successful config preparation retires the old schema path");
  assert.equal(live().length, 1, "A missing replacement schema is still watched for repair");
  const replacementWatch = live()[0];
  before = invalidations;
  third.fire(Uri.file(schemaPath));
  assert.equal(invalidations, before);
  await fs.writeFile(configPath, "[INVALID");
  projectWatch.fire(Uri.file(configPath));
  assert.ok(await controller.analyze(snapshot));
  assert.equal(live()[0], replacementWatch, "Failed captures preserve existing repair observation");
  await fs.writeFile(configPath, replacementConfig);
  projectWatch.fire(Uri.file(configPath));
  assert.equal(await controller.analyze(snapshot), null);
  await fs.writeFile(replacement, "type Query { buffered: Float }");
  replacementWatch.fire(Uri.file(replacement));
  const repaired = await controller.analyze(snapshot); assert.ok(repaired);
  assert.equal(repaired.request.schemas[0].text, "type Query { buffered: Float }");
  assert.equal(live().length, 1);
  const folder = fixture.vscode.workspace.workspaceFolders[0];
  const generatedStub = path.join(fixture.root, "graphql_desk_generated", "default.pyi");
  await fs.mkdir(path.dirname(generatedStub), { recursive: true });
  await fs.writeFile(generatedStub, "# Generated by GraphQL Desk; analysis session test\nclass Generated: ...\n");
  const folderWatch = fixture.watchers.find(watcher => watcher.pattern.pattern === "**/*");
  folderWatch.fire(Uri.file(generatedStub));
  fixture.vscode.workspace.workspaceFolders = [];
  fixture.events.onDidChangeWorkspaceFolders.fire();
  assert.equal(live().length, 0);
  before = invalidations;
  replacementWatch.fire(Uri.file(replacement));
  assert.equal(invalidations, before, "Removed workspace schema subscriptions are inert");
  fixture.vscode.workspace.workspaceFolders = [folder];
  fixture.events.onDidChangeWorkspaceFolders.fire();
  assert.ok(await controller.analyze(snapshot)); assert.equal(live().length, 1);
  const readdedFolderWatch = fixture.watchers.find(watcher => watcher.pattern.pattern === "**/*" && !watcher.disposed);
  const afterReadd = invalidations;
  await fs.writeFile(generatedStub, "class UserOwned: ...\n");
  await fs.unlink(generatedStub);
  readdedFolderWatch.fire(Uri.file(generatedStub));
  assert.equal(invalidations, afterReadd + 1, "Removing a folder clears ownership records before a later user-stub deletion");
  const final = live()[0];
  controller.dispose();
  assert.equal(live().length, 0);
  assert.ok(schemaWatches().every(watcher => watcher.disposals === 1));
  before = invalidations;
  final.fire(Uri.file(replacement));
  assert.equal(invalidations, before);
});

test("Pyright config requests wait for in-flight worker analysis", async t => {
  const fixture = await setup(t);
  let analyzeStarted;
  const started = new Promise(resolve => { analyzeStarted = resolve; });
  let releaseAnalyze;
  const hold = new Promise(resolve => { releaseAnalyze = resolve; });
  let active = 0;
  let configRequests = 0;
  const controller = createAnalysisController(fixture.vscode, { workerFactory: options => {
    const worker = createAnalysisWorker(options);
    return { ...worker, async request(message, requestOptions) {
      assert.equal(active, 0, "controller must serialize access to the single-request worker");
      active += 1;
      try {
        const reply = await worker.request(message, requestOptions);
        if (message.op === "analyze") { analyzeStarted(); await hold; }
        if (message.op === "pyright_config") configRequests += 1;
        return reply;
      } finally { active -= 1; }
    } };
  } });
  t.after(() => { releaseAnalyze(); controller.dispose(); });
  const analysis = controller.analyze(controller.snapshot(fixture.main));
  await started;
  const folder = fixture.vscode.workspace.workspaceFolders[0];
  const config = await fs.readFile(path.join(fixture.root, "pyproject.toml"), "utf8");
  const inspection = controller.editPyrightConfig(folder, config, "inspect");
  const inspected = inspection.then(reply => ({ reply }), error => ({ error }));
  await new Promise(resolve => realTimeout(resolve, 20));
  assert.equal(configRequests, 0, "config inspection stays queued while analysis owns the worker");
  releaseAnalyze();
  const [bundle, result] = await Promise.all([analysis, inspected]);
  assert.ok(bundle);
  assert.ok(result.reply, result.error?.message);
  const reply = result.reply;
  assert.equal(reply.found, false);
  assert.equal(configRequests, 1);
});

for (const scenario of ["source invalidation", "same-action requests", "trust revocation"]) {
  test(`Pyright config requests handle ${scenario} safely`, async t => {
    const fixture = await setup(t);
    let releaseAnalysis;
    const hold = new Promise(resolve => { releaseAnalysis = resolve; });
    let analysisStarted;
    const started = new Promise(resolve => { analysisStarted = resolve; });
    const configRequests = [];
    let active = false;
    const controller = createAnalysisController(fixture.vscode, { workerFactory: options => {
      const worker = createAnalysisWorker(options);
      return { ...worker, async request(message, requestOptions) {
        assert.equal(active, false, "configuration and analysis still share a single-request worker");
        active = true;
        try {
          const reply = await worker.request(message, requestOptions);
          if (message.op === "analyze") { analysisStarted(); await hold; }
          if (message.op === "pyright_config") configRequests.push(message.text);
          return reply;
        } finally { active = false; }
      } };
    } });
    t.after(() => { releaseAnalysis(); controller.dispose(); });
    const analysis = controller.analyze(controller.snapshot(fixture.main));
    await started;
    const names = scenario === "same-action requests" ? ["first", "second"] : ["first"];
    const texts = names.map(name => `[tool.pyright]\nextraPaths = ["${name}"]\n`);
    const inspections = texts.map(text => controller.editPyrightConfig(
      fixture.vscode.workspace.workspaceFolders[0], text, "inspect")
      .then(reply => ({ reply }), error => ({ error })));
    await new Promise(resolve => realTimeout(resolve, 20));
    if (scenario === "source invalidation") {
      fixture.main.version += 1;
      fixture.main.text += "\n";
      fixture.events.onDidChangeTextDocument.fire({ document: fixture.main,
        contentChanges: [{ rangeOffset: 0, rangeLength: 0, text: "\n" }] });
    }
    if (scenario === "trust revocation") fixture.vscode.workspace.isTrusted = false;
    releaseAnalysis();
    await analysis;
    const results = await Promise.all(inspections);
    if (scenario === "trust revocation") {
      assert.deepEqual(configRequests, [], "untrusted config requests must not reach the worker");
      assert.ok(results[0].error);
      return;
    }
    assert.deepEqual(configRequests, texts, "each captured configuration request must reach the worker once");
    for (const [index, result] of results.entries()) {
      assert.ok(result.reply, result.error?.message);
      assert.deepEqual(result.reply.extra_paths, [names[index]], "callers receive their own text's reply");
    }
  });
}
