"use strict";

const path = require("node:path");
const fs = require("node:fs");
const { createHash, randomUUID } = require("node:crypto");
const { createAnalysisWorker } = require("./analysisWorker");

const key = target => JSON.stringify([target.project, target.schema_name]);
const canonical = value => {
  let result;
  try { result = fs.realpathSync.native(value); } catch { result = path.resolve(value); }
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
  function stateFor(snapshot) {
    const uri = vscode.Uri.parse(snapshot.uri);
    const folder = vscode.workspace.getWorkspaceFolder(uri);
    if (!folder || folder.uri.scheme !== "file") return null;
    const root = canonical(folder.uri.fsPath);
    const python = vscode.workspace.getConfiguration("graphqlDesk", folder.uri).get("pythonPath", "");
    let state = states.get(root);
    if (!path.isAbsolute(python)) {
      if (state) { state.worker.dispose(); states.delete(root); }
      return null;
    }
    if (state && state.python !== python) { state.worker.dispose(); states.delete(root); state = null; }
    if (!state) {
      state = { root, folder, python, worker: workerFactory({ python, workspace: root, isTrusted: trusted }),
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
  function watch(state, filename) {
    if (state.watches.has(filename)) return;
    const watcher = vscode.workspace.createFileSystemWatcher(new vscode.RelativePattern(path.dirname(filename), path.basename(filename)));
    state.watches.set(filename, watcher);
    subscriptions.push(watcher, watcher.onDidChange(invalidate), watcher.onDidCreate(invalidate), watcher.onDidDelete(invalidate));
  }
  function check(ticket, token) {
    if (!trusted() || epoch !== ticket || token?.isCancellationRequested) throw new Error("Superseded analysis");
  }
  async function build(state, token, refreshTarget) {
    const ticket = epoch;
    check(ticket, token);
    const captured = await capture(state);
    check(ticket, token);
    const wanted = revisions(state);
    const revisionSignature = JSON.stringify(wanted);
    if (!refreshTarget && state.cached?.capture.signature === captured.signature && state.cached.revisionSignature === revisionSignature) return state.cached;
    // shortcut: stale jobs finish without publishing to retain fetched schemas; refine if measured edit latency needs interruption.
    const abort = refreshTarget ? new AbortController() : null;
    const cancellation = abort && token?.onCancellationRequested?.(() => abort.abort());
    const requestWorker = message => state.worker.request(message, abort ? { signal: abort.signal } : undefined);
    try {
      const prepared = await requestWorker({ op: "prepare", ...captured.request, remote_revisions: wanted });
      check(ticket, token);
      state.targets = prepared.remote_schemas;
      state.revisions = prepared.remote_revisions;
      const schema_buffers = prepared.required_buffers.map(id => {
        if (!captured.texts.has(id)) throw new Error("Missing schema buffer");
        return captured.texts.get(id);
      });
      for (const schema of prepared.local_schemas) watch(state, schema.path);
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
      check(ticket, token);
      const latest = await capture(state);
      check(ticket, token);
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
    const state = stateFor(value);
    if (!state) return null;
    try {
      const result = await enqueue(state, () => build(state));
      if (token?.isCancellationRequested || !trusted()) return null;
      if (result) status(result.bundle.unavailable_schemas.length ? "Schema unavailable" : null);
      return result?.bundle ?? null;
    }
    catch { state.cached = null; if (trusted()) status("Analysis unavailable"); return null; }
  }
  async function isCurrent(bundle, value, token) {
    if (!trusted() || token?.isCancellationRequested) return false;
    const state = stateFor(value);
    if (!state) return false;
    try {
      const current = await enqueue(state, async () => {
        const cached = state.cached;
        if (!cached || cached.bundle !== bundle) return false;
        check(cached.ticket);
        const latest = await capture(state);
        check(cached.ticket);
        if (latest.signature !== cached.capture.signature || JSON.stringify(revisions(state)) !== cached.revisionSignature) { state.cached = null; return false; }
        const reply = await state.worker.request({ op: "current", ...cached.request });
        check(cached.ticket);
        const final = await capture(state);
        check(cached.ticket);
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
    const state = document && stateFor(snapshot(document));
    if (!state) { report("Open a Python file and configure an absolute GraphQL Desk Python interpreter."); return; }
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
  for (const event of ["onDidOpenTextDocument", "onDidChangeTextDocument", "onDidCloseTextDocument", "onDidSaveTextDocument", "onDidChangeConfiguration", "onDidChangeWorkspaceFolders", "onDidGrantWorkspaceTrust"]) {
    if (vscode.workspace[event]) subscriptions.push(vscode.workspace[event](change => {
      if (event === "onDidChangeConfiguration" && change?.affectsConfiguration && !change.affectsConfiguration("graphqlDesk")) return;
      if (event === "onDidChangeConfiguration") {
        for (const [root, state] of states) {
          const python = vscode.workspace.getConfiguration("graphqlDesk", state.folder.uri).get("pythonPath", "");
          if (python !== state.python) { state.worker.dispose(); states.delete(root); }
        }
      }
      if (event === "onDidChangeWorkspaceFolders") {
        for (const state of states.values()) state.worker.dispose();
        states.clear();
      }
      invalidate();
    }));
  }
  for (const folder of vscode.workspace.workspaceFolders ?? []) if (folder.uri.scheme === "file") {
    const watcher = vscode.workspace.createFileSystemWatcher(new vscode.RelativePattern(folder, "**/*"));
    subscriptions.push(watcher, watcher.onDidChange(invalidate), watcher.onDidCreate(invalidate), watcher.onDidDelete(invalidate));
  }
  return { analyze, isCurrent, isTrusted: trusted, snapshot, mapSnapshot, resolveUri, clientDirectives, refresh, invalidate,
    dispose() { disposed = true; epoch += 1; for (const state of states.values()) state.worker.dispose(); states.clear(); subscriptions.forEach(item => item.dispose()); } };
}
module.exports = { createAnalysisController };

