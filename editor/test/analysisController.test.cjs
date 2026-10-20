"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs/promises");
const path = require("node:path");
const os = require("node:os");
const http = require("node:http");
const { pathToFileURL, fileURLToPath } = require("node:url");
const { buildSchema, introspectionFromSchema } = require("graphql");
const { createAnalysisController } = require("../src/analysisController");
const { createAnalysisWorker } = require("../src/analysisWorker");
const { createLanguageFeatures } = require("../src/languageFeatures");
const python = process.env.GRAPHQL_DESK_PYTHON ?? path.resolve(__dirname, "../../.venv", process.platform === "win32" ? "Scripts/python.exe" : "bin/python");

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
async function setup(t, config = 'schema="schema.graphql"\n') {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), "graphql-desk-controller-"));
  t.after(() => fs.rm(root, { recursive: true, force: true }));
  await fs.writeFile(path.join(root, "schema.graphql"), "type Query { title: String }");
  await fs.writeFile(path.join(root, "pyproject.toml"), `[tool.graphql-desk]\n${config}schema_output="graphql_desk_generated/default.pyi"\n`);
  const source = 'raise RuntimeError("NO_IMPORT")\nfrom graphql_desk import gql\nq=gql("{title}")\n';
  await fs.writeFile(path.join(root, "main.py"), source);
  let candidates = [Uri.file(path.join(root, "main.py"))];
  const folder = { uri: Uri.file(root) };
  const events = {};
  const watchers = [];
  let selectedPython = python;
  const workspace = { isTrusted: true, workspaceFolders: [folder], textDocuments: [],
    getWorkspaceFolder: value => workspace.workspaceFolders.includes(folder) && value.fsPath.toLowerCase().startsWith(root.toLowerCase()) ? folder : undefined,
    getConfiguration: () => ({ get: (name, fallback) => name === "pythonPath" ? selectedPython : fallback }),
    findFiles: async (_pattern, exclude) => { assert.equal(exclude, null); return candidates; },
    createFileSystemWatcher: () => { const e = event(); const watcher = { onDidChange: e.subscribe, onDidCreate: e.subscribe, onDidDelete: e.subscribe, dispose() {}, fire: e.fire }; watchers.push(watcher); return watcher; },
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
  return { root, vscode, main, document, events, watchers, interpreter: value => { selectedPython = value; }, candidates: values => { candidates = values.map(value => Uri.file(path.join(root, value))); } };
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
  fixture.main.version += 1; fixture.main.text += "# edited\n";
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
  const server = http.createServer((request, response) => { requests += 1; request.resume(); response.setHeader("Content-Type", "application/json"); response.end(payload); });
  await new Promise(resolve => server.listen(0, "127.0.0.1", resolve));
  t.after(() => new Promise(resolve => server.close(resolve)));
  const fixture = await setup(t, `schema={url="http://127.0.0.1:${server.address().port}/graphql",headers={"X-Key"="\${env:TOKEN}"}}\n`);
  let environment = { TOKEN: "PRIVATE_SECRET", UNRELATED: "NEVER_SEND" };
  const messages = [];
  const statuses = [];
  const controller = createAnalysisController(fixture.vscode, { status: value => statuses.push(value), environment: () => environment, workerFactory: options => {
    const worker = createAnalysisWorker(options);
    return { ...worker, request(message) { messages.push(message); return worker.request(message); } };
  } }); t.after(() => controller.dispose());
  let snapshot = controller.snapshot(fixture.main);
  assert.equal((await controller.analyze(snapshot)).request.schemas.length, 0); assert.equal(requests, 0);
  assert.equal(statuses.at(-1), "Schema unavailable");
  await controller.refresh(); assert.ok(requests > 0);
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
    fixture.main.version += 1; fixture.main.text += "# edit\n";
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

test("explicit refresh cancellation aborts the worker and cannot admit a late remote schema", async t => {
  let entered;
  const requested = new Promise(resolve => { entered = resolve; });
  const server = http.createServer(request => { request.resume(); entered(); });
  await new Promise(resolve => server.listen(0, "127.0.0.1", resolve));
  t.after(() => { server.closeAllConnections(); return new Promise(resolve => server.close(resolve)); });
  const fixture = await setup(t, `schema="http://127.0.0.1:${server.address().port}/graphql"\n`);
  const cancellation = event();
  const token = { isCancellationRequested: false, onCancellationRequested: cancellation.subscribe };
  fixture.vscode.ProgressLocation = { Notification: 15 };
  fixture.vscode.window.withProgress = async (options, action) => { assert.equal(options.cancellable, true); return action({}, token); };
  const controller = createAnalysisController(fixture.vscode); t.after(() => controller.dispose());
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
