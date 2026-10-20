"use strict";

const path = require("node:path");
const fs = require("node:fs");
const { createHash, randomUUID } = require("node:crypto");
const { createAnalysisWorker } = require("./analysisWorker");

const key = target => JSON.stringify([target.project, target.schema_name]);
const canonical = value => {
  let result;
  try { result = fs.realpathSync.native(value); } catch {
    // Unsaved paths still inherit existing parent junctions and Windows short-name expansion.
    const absolute = path.resolve(value);
    const parent = path.dirname(absolute);
    result = parent === absolute ? absolute : path.join(canonical(parent), path.basename(absolute));
  }
  return process.platform === "win32" ? result.toLowerCase() : result;
};
const inside = (root, value) => {
  const relative = path.relative(root, value);
  return relative === "" || (!path.isAbsolute(relative) && relative !== ".." && !relative.startsWith(`..${path.sep}`));
};

/** Captures are finite capabilities; watchers only request earlier invalidation. */
function createAnalysisController(vscode, { workerFactory = createAnalysisWorker, environment = () => process.env, changed = () => {}, report = () => {}, status = () => {} } = {}) {
  let disposed = false;
  let epoch = 0;
  const states = new Map();
  const subscriptions = [];
  const folderWatches = new Map();
  const interpreters = new Map();
  let pythonApiPromise;
  let pythonEnvironments;
  const configuredPython = folder => vscode.workspace.getConfiguration("graphqlDesk", folder.uri).get("pythonPath", "");
  function dropState(root) {
    const state = states.get(root);
    if (state) {
      state.cached = null; state.worker.dispose();
      reconcileSchemaWatches(state, new Set()); states.delete(root);
    }
  }
  const selectedKey = selected => selected && typeof selected.id === "string" && selected.id && typeof selected.path === "string" && selected.path ? JSON.stringify([selected.id, selected.path]) : null;
  async function environments(ticket) {
    check(ticket);
    if (!pythonApiPromise) {
      const extension = vscode.extensions?.getExtension("ms-python.python");
      if (!extension) return null;
      pythonApiPromise = Promise.resolve().then(() => { check(ticket); return extension.activate(); }).catch(() => null);
    }
    const api = await pythonApiPromise;
    check(ticket);
    const current = api?.environments;
    if (!current || typeof current.getActiveEnvironmentPath !== "function" || typeof current.resolveEnvironment !== "function" || typeof current.onDidChangeActiveEnvironmentPath !== "function") { pythonApiPromise = null; return null; }
    if (!pythonEnvironments) {
      pythonEnvironments = current;
      subscriptions.push(current.onDidChangeActiveEnvironmentPath(event => {
        if (!trusted()) return;
        const resource = event.resource?.uri ?? event.resource;
        if (resource && (resource.scheme !== "file" || !path.isAbsolute(resource.fsPath))) return;
        const filename = resource && canonical(resource.fsPath);
        let affected = false;
        const folders = new Map((vscode.workspace.workspaceFolders ?? [])
          .filter(folder => folder.uri.scheme === "file")
          .map(folder => [canonical(folder.uri.fsPath), folder]));
        for (const [root, state] of states) folders.set(root, state.folder);
        for (const [root, entry] of interpreters) folders.set(root, entry.folder);
        for (const [root, folder] of folders) {
          if (configuredPython(folder) !== "" || (filename && !inside(root, filename) && !inside(filename, root))) continue;
          interpreters.delete(root); dropState(root); affected = true;
        }
        if (affected) changed();
      }));
    }
    return current;
  }
  async function selectedPython(folder, root, ticket) {
    const api = await environments(ticket);
    check(ticket);
    if (!api || configuredPython(folder) !== "") return null;
    const selected = api.getActiveEnvironmentPath(folder.uri);
    const key = selectedKey(selected);
    if (!key) { interpreters.delete(root); dropState(root); return null; }
    let entry = interpreters.get(root);
    if (entry?.key !== key) {
      dropState(root);
      entry = { key, folder, promise: Promise.resolve().then(() => {
        check(ticket);
        if (configuredPython(folder) !== "" || interpreters.get(root) !== entry) return undefined;
        return api.resolveEnvironment({ id: selected.id, path: selected.path });
      }) };
      interpreters.set(root, entry);
    }
    let resolved;
    try { resolved = await entry.promise; }
    catch (error) {
      if (interpreters.get(root) === entry) { interpreters.delete(root); dropState(root); }
      throw error;
    }
    check(ticket);
    if (configuredPython(folder) !== "" || interpreters.get(root) !== entry || selectedKey(api.getActiveEnvironmentPath(folder.uri)) !== key) {
      if (interpreters.get(root) === entry) { interpreters.delete(root); dropState(root); }
      return null;
    }
    const executable = resolved?.executable?.uri;
    if (!executable || executable.scheme !== "file" || !path.isAbsolute(executable.fsPath)) { interpreters.delete(root); dropState(root); return null; }
    return executable.fsPath;
  }
  const trusted = () => {
    const allowed = !disposed && vscode.workspace.isTrusted === true;
    if (!allowed) status(null);
    if (!allowed) for (const state of states.values()) { state.cached = null; state.worker.invalidate(); }
    return allowed;
  };
  const localPath = document => {
    const uri = document.uri;
    if (!["file", "untitled"].includes(uri.scheme) || !path.isAbsolute(uri.fsPath)) return null;
    return canonical(uri.fsPath);
  };
  async function stateFor(snapshot) {
    const ticket = epoch;
    if (!trusted()) return null;
    const uri = vscode.Uri.parse(snapshot.uri);
    if (uri.scheme !== "file" || !path.isAbsolute(uri.fsPath)) return null;
    const filename = canonical(uri.fsPath);
    // Worker facts use real paths; lexical aliases must not hide a deeper owner.
    const owners = (vscode.workspace.workspaceFolders ?? [])
      .filter(item => item.uri.scheme === "file" && inside(canonical(item.uri.fsPath), filename))
      .sort((left, right) => canonical(right.uri.fsPath).length - canonical(left.uri.fsPath).length);
    const deepest = owners.filter(item => canonical(item.uri.fsPath) === (owners[0] && canonical(owners[0].uri.fsPath)));
    const folder = deepest.length === 1 ? deepest[0] : null;
    if (!folder || folder.uri.scheme !== "file") return null;
    const root = canonical(folder.uri.fsPath);
    const setting = configuredPython(folder);
    let python = setting;
    if (setting === "") {
      try { python = await selectedPython(folder, root, ticket); } catch { return null; }
    }
    if (!trusted() || epoch !== ticket || configuredPython(folder) !== setting) return null;
    let state = states.get(root);
    if (typeof python !== "string" || !path.isAbsolute(python)) {
      dropState(root);
      return null;
    }
    if (state && (state.python !== python || state.setting !== setting)) { dropState(root); state = null; }
    if (!state) {
      state = { root, folder, python, setting, worker: workerFactory({ python, workspace: root, isTrusted: trusted }),
        running: false, pending: null, cached: null, targets: [], credentials: new Map(), revisions: [], watches: new Map() };
      states.set(root, state);
    }
    return state;
  }
  function invalidate() {
    if (disposed) return;
    epoch += 1;
    for (const state of states.values()) { state.cached = null; if (!trusted()) state.worker.invalidate(); }
    changed();
  }
  function snapshot(document) {
    const filename = localPath(document);
    return { uri: filename ? vscode.Uri.file(filename).toString() : document.uri.toString(), version: document.version, text: document.getText() };
  }
  function mapSnapshot(value, bundle) {
    const parsed = vscode.Uri.parse(value.uri);
    if (parsed.scheme !== "file") return value;
    const matches = bundle.request.modules.filter(item => {
      const candidate = vscode.Uri.parse(item.uri);
      return candidate.scheme === "file" && canonical(candidate.fsPath) === canonical(parsed.fsPath);
    });
    return matches.length === 1 ? { ...value, uri: matches[0].uri } : value;
  }
  function resolveUri(uri) {
    const parsed = vscode.Uri.parse(uri);
    if (parsed.scheme !== "file") return parsed;
    const matches = vscode.workspace.textDocuments.filter(document => localPath(document) === canonical(parsed.fsPath));
    return matches.length === 1 ? matches[0].uri : parsed;
  }
  function credential(state, target) {
    const values = Object.create(null);
    const source = environment();
    for (const name of target.environment_names) if (typeof source[name] === "string") values[name] = source[name];
    // Keep secret-derived fingerprints private; wire revisions are random capabilities.
    const fingerprint = createHash("sha256").update(JSON.stringify(values)).digest("hex");
    const previous = state.credentials.get(key(target));
    const revision = previous?.fingerprint === fingerprint ? previous.revision : randomUUID();
    state.credentials.set(key(target), { fingerprint, revision });
    return { values, revision };
  }
  function revisions(state) {
    return state.targets.map(target => ({ project: target.project, schema_name: target.schema_name, revision: credential(state, target).revision }));
  }
  async function capture(state) {
    const found = await vscode.workspace.findFiles(new vscode.RelativePattern(state.folder, "**/*.py"), null);
    const documents = vscode.workspace.textDocuments.filter(document => localPath(document));
    const candidates = new Set(found.filter(uri => uri.scheme === "file").map(uri => canonical(uri.fsPath)));
    for (const document of documents) {
      const filename = localPath(document);
      if (filename.endsWith(".py") && inside(state.root, filename)) candidates.add(filename);
    }
    const allowed = new Set(candidates);
    for (const filename of candidates) {
      if (!inside(state.root, filename)) throw new Error("Source escapes workspace");
      let parent = path.dirname(filename);
      while (true) {
        allowed.add(canonical(path.join(parent, "pyproject.toml")));
        if (parent === state.root) break;
        const next = path.dirname(parent);
        if (next === parent) throw new Error("Source escapes workspace");
        parent = next;
      }
    }
    const inventory = documents.map(document => ({ id: document.uri.toString(), path: localPath(document), version: document.version, is_dirty: document.isDirty }));
    inventory.sort((a, b) => a.id.localeCompare(b.id));
    const buffers = documents.filter(document => allowed.has(localPath(document))).map(document => ({ path: localPath(document), version: document.version, text: document.getText(), is_dirty: document.isDirty }));
    buffers.sort((a, b) => a.path.localeCompare(b.path));
    const request = { workspace: state.root, candidates: [...candidates].sort(), buffers, documents: inventory,
      editor_fallback: vscode.workspace.getConfiguration("graphqlDesk", state.folder.uri).get("project", null) };
    const texts = new Map(documents.map(document => [document.uri.toString(), { path: localPath(document), version: document.version, text: document.getText(), is_dirty: document.isDirty }]));
    return { request, texts, signature: JSON.stringify([request, [...texts]]) };
  }
  // One wire job and one latest pending capture; edit storms never build a promise queue.
  function enqueue(state, action, kind = "analyze") {
    if (state.running) {
      if (state.pending?.kind === "refresh" && kind !== "refresh") return Promise.resolve(null);
      if (state.pending?.kind === kind) { state.pending.action = action; return state.pending.promise; }
      if (state.pending) state.pending.resolve(null);
      let resolve;
      let reject;
      const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
      state.pending = { action, kind, promise, resolve, reject };
      return promise;
    }
    state.running = true;
    return Promise.resolve().then(action).finally(() => {
      state.running = false;
      const pending = state.pending;
      state.pending = null;
      if (pending) enqueue(state, pending.action, pending.kind).then(pending.resolve, pending.reject);
    });
  }
  function reconcileSchemaWatches(state, filenames) {
    for (const [filename, handles] of state.watches) if (!filenames.has(filename)) {
      handles.forEach(handle => handle.dispose());
      state.watches.delete(filename);
    }
    for (const filename of filenames) if (!state.watches.has(filename)) {
      const watcher = vscode.workspace.createFileSystemWatcher(new vscode.RelativePattern(path.dirname(filename), path.basename(filename)));
      state.watches.set(filename, [watcher, watcher.onDidChange(invalidate), watcher.onDidCreate(invalidate), watcher.onDidDelete(invalidate)]);
    }
  }
  function check(ticket, token, state) {
    if (!trusted() || epoch !== ticket || token?.isCancellationRequested || (state && states.get(state.root) !== state)) throw new Error("Superseded analysis");
  }
  async function build(state, token, refreshTarget) {
    const ticket = epoch;
    check(ticket, token, state);
    const captured = await capture(state);
    check(ticket, token, state);
    const wanted = revisions(state);
    const revisionSignature = JSON.stringify(wanted);
    if (!refreshTarget && state.cached?.capture.signature === captured.signature && state.cached.revisionSignature === revisionSignature) return state.cached;
    // shortcut: stale jobs finish without publishing to retain fetched schemas; refine if measured edit latency needs interruption.
    const abort = refreshTarget ? new AbortController() : null;
    const cancellation = abort && token?.onCancellationRequested?.(() => abort.abort());
    const requestWorker = message => state.worker.request(message, abort ? { signal: abort.signal } : undefined);
    try {
      const prepared = await requestWorker({ op: "prepare", ...captured.request, remote_revisions: wanted });
      check(ticket, token, state);
      state.targets = prepared.remote_schemas;
      state.revisions = prepared.remote_revisions;
      const schema_buffers = prepared.required_buffers.map(id => {
        if (!captured.texts.has(id)) throw new Error("Missing schema buffer");
        return captured.texts.get(id);
      });
      reconcileSchemaWatches(state, new Set(prepared.local_schemas.map(schema => schema.path)));
      let identity = prepared.id;
      let refreshStatus;
      if (refreshTarget) {
        const target = state.targets.find(item => key(item) === key(refreshTarget));
        if (!target) throw new Error("Remote target changed");
        const credentials = credential(state, target);
        const refreshed = await requestWorker({ op: "refresh", ...captured.request, id: identity, schema_buffers,
          project: target.project, schema_name: target.schema_name, credential_revision: credentials.revision, environment: credentials.values });
        identity = refreshed.id;
        state.revisions = state.revisions.filter(item => key(item) !== key(target));
        state.revisions.push({ project: target.project, schema_name: target.schema_name, revision: credentials.revision });
        state.revisions.sort((a, b) => key(a).localeCompare(key(b)));
        refreshStatus = refreshed.status;
      }
      const request = { ...captured.request, id: identity, schema_buffers, remote_revisions: state.revisions };
      const bundle = await requestWorker({ op: "analyze", ...request });
      check(ticket, token, state);
      const latest = await capture(state);
      check(ticket, token, state);
      if (latest.signature !== captured.signature) throw new Error("Capture changed");
      const latestRevisions = revisions(state);
      if (state.revisions.some(active => !latestRevisions.some(current => key(current) === key(active) && current.revision === active.revision))) throw new Error("Credentials changed during analysis");
      if (refreshStatus) report(refreshStatus === "ready" ? "Schema refreshed." : "Schema refresh failed; this schema is unavailable for analysis.");
      // Bind newly discovered targets before computing the credential freshness key.
      const result = { bundle, request, capture: captured, ticket, preferences: prepared.project_preferences,
        revisionSignature: JSON.stringify(latestRevisions) };
      state.cached = result;
      return result;
    } finally { cancellation?.dispose(); }
  }
  async function analyze(value, token) {
    if (!trusted() || token?.isCancellationRequested) return null;
    const state = await stateFor(value);
    if (!state || token?.isCancellationRequested) return null;
    try {
      const result = await enqueue(state, () => build(state));
      if (token?.isCancellationRequested || !trusted() || states.get(state.root) !== state) return null;
      if (result) status(result.bundle.unavailable_schemas.length ? "Schema unavailable" : null);
      return result?.bundle ?? null;
    }
    catch { state.cached = null; if (trusted()) status("Analysis unavailable"); return null; }
  }
  async function isCurrent(bundle, value, token) {
    if (!trusted() || token?.isCancellationRequested) return false;
    const state = await stateFor(value);
    if (!state || token?.isCancellationRequested) return false;
    try {
      const current = await enqueue(state, async () => {
        const cached = state.cached;
        if (!cached || cached.bundle !== bundle) return false;
        check(cached.ticket, undefined, state);
        const latest = await capture(state);
        check(cached.ticket, undefined, state);
        if (latest.signature !== cached.capture.signature || JSON.stringify(revisions(state)) !== cached.revisionSignature) { state.cached = null; return false; }
        const reply = await state.worker.request({ op: "current", ...cached.request });
        check(cached.ticket, undefined, state);
        const final = await capture(state);
        check(cached.ticket, undefined, state);
        const current = reply.current && final.signature === latest.signature && JSON.stringify(revisions(state)) === cached.revisionSignature;
        if (!current) state.cached = null;
        return current;
      }, "current");
      return Boolean(current) && !token?.isCancellationRequested && trusted() && state.cached?.bundle === bundle;
    } catch { state.cached = null; return false; }
  }
  async function refresh() {
    if (!trusted()) { report("Trust this workspace before refreshing a schema."); return; }
    const document = vscode.window.activeTextEditor?.document;
    const state = document && await stateFor(snapshot(document));
    if (!state) { report("Open a Python file and select a Python environment, or configure an absolute GraphQL Desk Python interpreter."); return; }
    try {
      await enqueue(state, () => build(state));
      const choices = state.targets.map(target => ({ label: target.schema_name, description: target.project, target }));
      const choice = choices.length === 1 ? choices[0] : await vscode.window.showQuickPick(choices, { placeHolder: "Select a remote GraphQL schema to refresh" });
      if (!choice) { if (!choices.length) report("No remote schema is configured for this project."); return; }
      const run = token => enqueue(state, () => build(state, token, choice.target), "refresh");
      if (vscode.window.withProgress) await vscode.window.withProgress({ location: vscode.ProgressLocation.Notification,
        title: "Refreshing GraphQL schema", cancellable: true }, (_progress, token) => run(token));
      else await run();
      changed();
    } catch { report("GraphQL Desk could not refresh this schema. Check configuration and the selected interpreter."); }
  }
  function clientDirectives(bundle, call) {
    for (const state of states.values()) if (state.cached?.bundle === bundle) return state.cached.preferences.find(item => item.project === call.identity.module.project)?.client_directives ?? [];
    return [];
  }
  function reconcileFolderWatches() {
    const folders = new Map((vscode.workspace.workspaceFolders ?? [])
      .filter(folder => folder.uri.scheme === "file").map(folder => [folder.uri.toString(), folder]));
    for (const [uri, handles] of folderWatches) if (!folders.has(uri)) {
      handles.forEach(handle => handle.dispose());
      folderWatches.delete(uri);
    }
    for (const [uri, folder] of folders) if (!folderWatches.has(uri)) {
      const watcher = vscode.workspace.createFileSystemWatcher(new vscode.RelativePattern(folder, "**/*"));
      folderWatches.set(uri, [watcher, watcher.onDidChange(invalidate), watcher.onDidCreate(invalidate), watcher.onDidDelete(invalidate)]);
    }
  }
  for (const event of ["onDidOpenTextDocument", "onDidChangeTextDocument", "onDidCloseTextDocument", "onDidSaveTextDocument", "onDidChangeConfiguration", "onDidChangeWorkspaceFolders", "onDidGrantWorkspaceTrust"]) {
    if (vscode.workspace[event]) subscriptions.push(vscode.workspace[event](change => {
      if (event === "onDidChangeConfiguration" && change?.affectsConfiguration && !change.affectsConfiguration("graphqlDesk")) return;
      if (event === "onDidChangeConfiguration") {
        for (const [root, state] of states) {
          const python = vscode.workspace.getConfiguration("graphqlDesk", state.folder.uri).get("pythonPath", "");
          if (python !== state.setting) { interpreters.delete(root); dropState(root); }
        }
      }
      if (event === "onDidChangeWorkspaceFolders") {
        for (const root of states.keys()) dropState(root);
        interpreters.clear();
        reconcileFolderWatches();
      }
      invalidate();
    }));
  }
  reconcileFolderWatches();
  return { analyze, isCurrent, isTrusted: trusted, snapshot, mapSnapshot, resolveUri, clientDirectives, refresh, invalidate,
    dispose() { disposed = true; epoch += 1; for (const root of states.keys()) dropState(root); interpreters.clear(); for (const handles of folderWatches.values()) handles.forEach(handle => handle.dispose()); folderWatches.clear(); subscriptions.forEach(item => item.dispose()); } };
}
module.exports = { createAnalysisController };

