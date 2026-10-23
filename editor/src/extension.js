"use strict";

const path = require("node:path");
const { createAnalysisController } = require("./analysisController");
const { registerLanguageFeatures } = require("./languageFeatures");
const { registerHighlighting } = require("./highlighting");
const { createManagedStubPath } = require("./managedStubPath");
const { createOwnedAnalysisArtifacts } = require("./ownedAnalysisArtifacts");
const { createAnalysisGenerations } = require("./analysisGenerations");

async function reconfigureWorkspaceRoots({ roots, folders, create, retire }) {
  const errors = [];
  for (const [folderRoot, promise] of [...roots]) {
    let current;
    try { current = await promise; }
    catch (error) { if (roots.get(folderRoot) === promise) roots.delete(folderRoot); errors.push(error); continue; }
    const folder = folders.find(item => path.resolve(item.uri.fsPath) === folderRoot);
    if (!folder) {
      if (roots.get(folderRoot) === promise) roots.delete(folderRoot);
      try { await retire(folderRoot, current); } catch (error) { errors.push(error); }
      continue;
    }
    let next;
    try { next = await create(folder); }
    catch (error) {
      if (roots.get(folderRoot) === promise) roots.delete(folderRoot);
      try { await retire(folderRoot, current); } catch (cleanupError) { errors.push(cleanupError); }
      errors.push(error);
      continue;
    }
    if (next.root === current.root) { next.cleanup(); continue; }
    const replacement = Promise.resolve(next);
    if (roots.get(folderRoot) === promise) roots.set(folderRoot, replacement);
    try { await retire(folderRoot, current); }
    catch (error) {
      if (roots.get(folderRoot) === replacement) roots.delete(folderRoot);
      next.cleanup();
      errors.push(error);
    }
  }
  if (errors.length) throw new AggregateError(errors, "One or more workspace stub roots could not be reconfigured");
}

let active;
function activate(context) {
  const vscode = require("vscode");
  let language;
  let highlighting;
  let timer;
  let disposed = false;
  let generation = 0;
  let refreshCancellation;
  let lastSkipReason;
  const outputs = new Map();
  const generations = new Map();
  const roots = new Map();
  const output = vscode.window.createOutputChannel("GraphQL Desk");
  function traceSkip(reason) {
    if (reason === lastSkipReason) return;
    lastSkipReason = reason;
    output.appendLine(`Live typing skipped: ${reason}`);
  }
  function managedRoot(folder, folderRoot) {
    let promise = roots.get(folderRoot);
    if (!promise) {
      promise = createManagedStubPath(vscode, folder, context.workspaceState);
      roots.set(folderRoot, promise);
      promise.catch(() => { if (roots.get(folderRoot) === promise) roots.delete(folderRoot); });
    }
    return promise;
  }
  async function retireRoot(folderRoot, managed) {
    const prefix = `${folderRoot}\0${managed.root}\0`;
    for (const [key, handle] of outputs) if (key.startsWith(prefix)) {
      await handle.cleanup();
      outputs.delete(key);
    }
    managed.cleanup();
  }
  async function reconfigureRoots() {
    await reconfigureWorkspaceRoots({ roots, folders: vscode.workspace.workspaceFolders ?? [],
      create: folder => createManagedStubPath(vscode, folder, context.workspaceState), retire: retireRoot });
  }
  async function managedStubPathsAreCurrent() {
    if (!roots.size) return false;
    for (const [folderRoot, promise] of roots) {
      const folder = (vscode.workspace.workspaceFolders ?? []).find(item => path.resolve(item.uri.fsPath) === folderRoot);
      if (!folder) return false;
      try {
        const managed = await promise;
        if (!managed.stillOwnsSetting()) return false;
      } catch { return false; }
    }
    return true;
  }
  async function publishTypes(editor, ticket, token) {
    const document = editor.document;
    const snapshot = controller.snapshot(document);
    const result = await controller.artifacts(snapshot, token, traceSkip);
    if (!result) return;
    if (result.unavailable_schemas.length) { traceSkip("schema-unavailable"); return; }
    if (disposed || ticket !== generation || !result.isCurrent()) { traceSkip("analysis-snapshot-stale"); return; }
    const folder = vscode.workspace.getWorkspaceFolder(document.uri);
    if (!folder) { traceSkip("document-outside-workspace"); return; }
    const folderRoot = path.resolve(folder.uri.fsPath);
    let managed;
    try {
      const managedPromise = managedRoot(folder, folderRoot);
      managed = await managedPromise;
    } catch {
      traceSkip("managed-stub-root-unavailable");
      status.text = "GraphQL Desk: managed typing unavailable";
      status.show();
      return;
    }
    if (disposed || ticket !== generation || !result.isCurrent()) { traceSkip("analysis-snapshot-stale"); return; }
    try {
      const groups = new Map();
      for (const project of result.projects) {
        const files = project.files;
        if (!groups.has(project.generated_package)) groups.set(project.generated_package, []);
        groups.get(project.generated_package).push(...files);
      }
      let changedArtifacts = false;
      if (managed.ownsSetting) {
        const key = `${folderRoot}\0${managed.root}`;
        let publisher = generations.get(key);
        if (!publisher) { publisher = createAnalysisGenerations(vscode, folderRoot, managed.root); generations.set(key, publisher); }
        changedArtifacts = await publisher.recover(managed, result.isCurrent,
          result.input_fingerprint, result.saved_source_fingerprint);
        if (disposed || ticket !== generation || !result.isCurrent()) return;
        changedArtifacts = (await publisher.publish(groups, result.id, result.isCurrent, managed,
          result.input_fingerprint, result.saved_source_fingerprint)) || changedArtifacts;
      } else {
        for (const [key, handle] of outputs) if (key.startsWith(`${folderRoot}\0${managed.root}\0`) && !groups.has(key.split("\0")[2])) {
          changedArtifacts = (await handle.publish([], result.isCurrent)) || changedArtifacts;
        }
        for (const [packageName, files] of groups) {
          if (disposed || ticket !== generation || !result.isCurrent()) { traceSkip("analysis-snapshot-stale"); return; }
          const key = `${folderRoot}\0${managed.root}\0${packageName}`;
          let handle = outputs.get(key);
          if (!handle && files.length) {
            handle = createOwnedAnalysisArtifacts(vscode, folderRoot, managed.root, packageName, files);
            outputs.set(key, handle);
            changedArtifacts = true;
          } else if (handle) changedArtifacts = (await handle.publish(files, result.isCurrent)) || changedArtifacts;
        }
      }
      if (changedArtifacts && !managed.ownsSetting) await vscode.commands.executeCommand("python.analysis.restartLanguageServer");
      if (![...groups.values()].some(files => files.length)) traceSkip("no-generated-stub-files");
      else lastSkipReason = undefined;
    } catch (error) {
      output.appendLine(`Managed stub publication failed: ${String(error)}`);
      traceSkip("managed-stub-publish-failed");
      status.text = "GraphQL Desk: managed typing unavailable";
      status.show();
    }
  }
  const refreshVisible = () => {
    generation += 1;
    const ticket = generation;
    refreshCancellation?.cancel();
    refreshCancellation?.dispose();
    refreshCancellation = new vscode.CancellationTokenSource();
    const token = refreshCancellation.token;
    clearTimeout(timer);
    // Drop published semantic diagnostics immediately; rebuild after the edit settles.
    for (const document of vscode.workspace.textDocuments) language?.close(document.uri);
    timer = setTimeout(() => {
      if (disposed) return;
      for (const editor of vscode.window.visibleTextEditors) {
        if (editor.document.languageId !== "python") continue;
        void Promise.allSettled([language.refresh(editor.document), highlighting.refresh(editor)]).then(() => {
          if (!disposed && ticket === generation) return publishTypes(editor, ticket, token);
        });
      }
    }, 100);
  };
  const status = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Right, 0);
  status.name = "GraphQL Desk";
  status.command = "graphqlDesk.refreshSchema";
  status.tooltip = "GraphQL Desk: refresh a remote schema; positive schema_refresh_interval enables trusted background polling. Check configuration and interpreter settings if unavailable.";
  const controller = createAnalysisController(vscode, { changed: refreshVisible,
    status: message => {
      if (message) { status.text = `GraphQL Desk: ${message}`; status.show(); }
      else status.hide();
    }, report: message => vscode.window.showInformationMessage(message) });
  language = registerLanguageFeatures(vscode, controller);
  highlighting = registerHighlighting(vscode, controller);
  const visible = vscode.window.onDidChangeVisibleTextEditors(refreshVisible);
  const textChanged = vscode.workspace.onDidChangeTextDocument(event => {
    if (event.document.languageId === "python") refreshVisible();
  });
  const configChanged = vscode.workspace.onDidChangeConfiguration(event => {
    if (event.affectsConfiguration("python.analysis.stubPath")) {
      // Our own generation switch selects already complete immutable output. Treat a
      // mismatch as an external setting edit and rebuild the manager around it.
      void managedStubPathsAreCurrent().then(ownedSelection => {
        if (ownedSelection) return;
        generation += 1;
        return reconfigureRoots().then(refreshVisible, () => { status.text = "GraphQL Desk: managed typing unavailable"; status.show(); refreshVisible(); });
      });
    } else if (event.affectsConfiguration("graphqlDesk")) refreshVisible();
  });
  const foldersChanged = vscode.workspace.onDidChangeWorkspaceFolders(() => {
    generation += 1;
    void reconfigureRoots().then(refreshVisible, () => {
      status.text = "GraphQL Desk: managed typing unavailable";
      status.show();
      refreshVisible();
    });
  });
  const command = vscode.commands.registerCommand("graphqlDesk.refreshSchema", () => controller.refresh());
  const lifecycle = { async dispose() {
    if (disposed) return;
    disposed = true; generation += 1; refreshCancellation?.cancel(); refreshCancellation?.dispose(); clearTimeout(timer); command.dispose(); status.dispose(); visible.dispose(); foldersChanged.dispose();
    textChanged.dispose(); configChanged.dispose(); language.dispose(); highlighting.dispose(); controller.dispose(); output.dispose();
    await Promise.all([...outputs.values()].map(handle => handle.cleanup().catch(() => {})));
    outputs.clear();
    const managedRoots = await Promise.all([...roots.values()].map(promise => promise.catch(() => null)));
    for (const managed of managedRoots) managed?.cleanup();
    roots.clear();
    try { await vscode.commands.executeCommand("python.analysis.restartLanguageServer"); } catch { /* Pylance may already be deactivating. */ }
  } };
  context.subscriptions.push(lifecycle);
  active = lifecycle;
  refreshVisible();
}
async function deactivate() { await active?.dispose(); active = undefined; }
module.exports = { activate, deactivate, reconfigureWorkspaceRoots };
