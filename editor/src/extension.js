"use strict";

const path = require("node:path");
const { createAnalysisController } = require("./analysisController");
const { registerLanguageFeatures } = require("./languageFeatures");
const { registerHighlighting } = require("./highlighting");
const { createManagedStubPath } = require("./managedStubPath");
const { createOwnedAnalysisArtifacts } = require("./ownedAnalysisArtifacts");

async function reconfigureWorkspaceRoots({ roots, folders, create, retire }) {
  const errors = [];
  for (const [folderRoot, promise] of [...roots]) {
    let current;
    try { current = await promise; }
    catch (error) { if (roots.get(folderRoot) === promise) roots.delete(folderRoot); errors.push(error); continue; }
    const folder = folders.find(item => path.resolve(item.uri.fsPath) === folderRoot);
    if (!folder) continue;
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
  const outputs = new Map();
  const roots = new Map();
  function managedRoot(folder, folderRoot) {
    let promise = roots.get(folderRoot);
    if (!promise) {
      promise = createManagedStubPath(vscode, folder);
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
      create: folder => createManagedStubPath(vscode, folder), retire: retireRoot });
  }
  async function publishTypes(editor, ticket) {
    const document = editor.document;
    const snapshot = controller.snapshot(document);
    const result = await controller.artifacts(snapshot);
    if (!result || result.unavailable_schemas.length || disposed || ticket !== generation || !result.isCurrent()) return;
    const folder = vscode.workspace.getWorkspaceFolder(document.uri);
    if (!folder) return;
    try {
      const folderRoot = path.resolve(folder.uri.fsPath);
      const managedPromise = managedRoot(folder, folderRoot);
      const managed = await managedPromise;
      if (disposed || ticket !== generation || !result.isCurrent()) return;
      const groups = new Map();
      for (const project of result.projects) {
        const files = project.files.filter(file => file.relative_path.endsWith(".pyi"));
        if (!groups.has(project.generated_package)) groups.set(project.generated_package, []);
        groups.get(project.generated_package).push(...files);
      }
      let changedArtifacts = false;
      for (const [key, handle] of outputs) if (key.startsWith(`${folderRoot}\0${managed.root}\0`) && !groups.has(key.split("\0")[2])) {
        changedArtifacts = (await handle.publish([], result.isCurrent)) || changedArtifacts;
      }
      for (const [packageName, files] of groups) {
        if (disposed || ticket !== generation || !result.isCurrent()) return;
        const key = `${folderRoot}\0${managed.root}\0${packageName}`;
        let handle = outputs.get(key);
        if (!handle && files.length) {
          handle = createOwnedAnalysisArtifacts(vscode, folderRoot, managed.root, packageName, files);
          outputs.set(key, handle);
          changedArtifacts = true;
        } else if (handle) changedArtifacts = (await handle.publish(files, result.isCurrent)) || changedArtifacts;
      }
      if (changedArtifacts) await vscode.commands.executeCommand("python.analysis.restartLanguageServer");
    } catch {
      status.text = "GraphQL Desk: managed typing unavailable";
      status.show();
    }
  }
  const refreshVisible = () => {
    generation += 1;
    const ticket = generation;
    clearTimeout(timer);
    // Drop published semantic diagnostics immediately; rebuild after the edit settles.
    for (const document of vscode.workspace.textDocuments) language?.close(document.uri);
    timer = setTimeout(() => {
      if (disposed) return;
      for (const editor of vscode.window.visibleTextEditors) {
        if (editor.document.languageId !== "python") continue;
        void language.refresh(editor.document);
        void highlighting.refresh(editor);
        void publishTypes(editor, ticket);
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
      // Invalidate any publication paused in async root setup before retiring the old root.
      generation += 1;
      void reconfigureRoots().then(refreshVisible, () => { status.text = "GraphQL Desk: managed typing unavailable"; status.show(); refreshVisible(); });
    } else if (event.affectsConfiguration("graphqlDesk")) refreshVisible();
  });
  const command = vscode.commands.registerCommand("graphqlDesk.refreshSchema", () => controller.refresh());
  const lifecycle = { async dispose() {
    if (disposed) return;
    disposed = true; generation += 1; clearTimeout(timer); command.dispose(); status.dispose(); visible.dispose();
    textChanged.dispose(); configChanged.dispose(); language.dispose(); highlighting.dispose(); controller.dispose();
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
