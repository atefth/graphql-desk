"use strict";

const fs = require("node:fs");
const path = require("node:path");

const normalized = value => process.platform === "win32" ? path.resolve(value).toLowerCase() : path.resolve(value);
const inside = (root, value) => {
  const relative = path.relative(root, value);
  return relative === "" || (!path.isAbsolute(relative) && relative !== ".." && !relative.startsWith(`..${path.sep}`));
};

async function createManagedStubPath(vscode, folder) {
  if (!folder || folder.uri.scheme !== "file" || !path.isAbsolute(folder.uri.fsPath) || !vscode.workspace.isTrusted) throw new Error("Trusted file workspace required");
  const workspace = path.resolve(folder.uri.fsPath);
  const config = vscode.workspace.getConfiguration("python", folder.uri);
  const inspection = config.inspect?.("analysis.stubPath");
  const explicitlyConfigured = inspection && [inspection.globalValue, inspection.workspaceValue, inspection.workspaceFolderValue].some(value => value !== undefined);
  const configured = explicitlyConfigured
    ? config.get("analysis.stubPath", "") : "";
  let setting = typeof configured === "string" ? configured.trim() : "";
  if (!setting && explicitlyConfigured) {
    setting = "typings";
  } else if (!setting) {
    const target = path.join(workspace, ".vscode", "settings.json");
    const workspaceFile = vscode.workspace.workspaceFile?.scheme === "file" ? vscode.workspace.workspaceFile.fsPath : null;
    const dirty = vscode.workspace.textDocuments?.some(document => document.isDirty &&
      document.uri.scheme === "file" && document.uri.fsPath &&
      (normalized(document.uri.fsPath) === normalized(target) || (workspaceFile && normalized(document.uri.fsPath) === normalized(workspaceFile))));
    if (dirty) throw new Error("Dirty workspace settings; preserved");
    setting = ".graphql-desk/typings";
    const destination = vscode.ConfigurationTarget?.WorkspaceFolder;
    if (destination === undefined || typeof config.update !== "function") throw new Error("Cannot safely configure GraphQL Desk analysis stubs");
      await config.update("analysis.stubPath", setting, destination);
  }
  const root = path.resolve(workspace, setting);
  if (!inside(workspace, root)) throw new Error("Configured Pylance stubPath is outside the workspace; preserved");
  const created = [];
  try {
    const relative = path.relative(workspace, root);
    let current = workspace;
    const workspaceStat = fs.lstatSync(current);
    if (workspaceStat.isSymbolicLink() || !workspaceStat.isDirectory()) throw new Error("Workspace path is not a directory");
    for (const part of relative.split(path.sep).filter(Boolean)) {
      current = path.join(current, part);
      try {
        const stat = fs.lstatSync(current);
        if (stat.isSymbolicLink() || !stat.isDirectory()) throw new Error("Pylance stubPath is not a safe directory");
      } catch (error) {
        if (error.code !== "ENOENT") throw error;
        fs.mkdirSync(current);
        created.push([current, fs.lstatSync(current)]);
      }
    }
    return {
      root,
      cleanup() {
        for (const [directory, stat] of [...created].reverse()) {
          try {
            const currentStat = fs.lstatSync(directory);
            if (currentStat.isSymbolicLink() || currentStat.dev !== stat.dev || currentStat.ino !== stat.ino || fs.readdirSync(directory).length) continue;
            fs.rmdirSync(directory);
          } catch { /* Preserve paths that are no longer verified as ours. */ }
        }
      },
    };
  } catch (error) {
    for (const [directory, stat] of created.reverse()) {
      try {
        const currentStat = fs.lstatSync(directory);
        if (!currentStat.isSymbolicLink() && currentStat.dev === stat.dev && currentStat.ino === stat.ino && fs.readdirSync(directory).length === 0) fs.rmdirSync(directory);
      } catch { /* Preserve paths that are no longer verified as ours. */ }
    }
    throw error;
  }
}

module.exports = { createManagedStubPath };
