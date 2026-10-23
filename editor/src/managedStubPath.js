"use strict";

const fs = require("node:fs");
const path = require("node:path");
const { createHash } = require("node:crypto");

const normalized = value => process.platform === "win32" ? path.resolve(value).toLowerCase() : path.resolve(value);
const inside = (root, value) => {
  const relative = path.relative(root, value);
  return relative === "" || (!path.isAbsolute(relative) && relative !== ".." && !relative.startsWith(`..${path.sep}`));
};

async function createManagedStubPath(vscode, folder, workspaceState) {
  if (!folder || folder.uri.scheme !== "file" || !path.isAbsolute(folder.uri.fsPath) || !vscode.workspace.isTrusted) throw new Error("Trusted file workspace required");
  const workspace = path.resolve(folder.uri.fsPath);
  const config = vscode.workspace.getConfiguration("python", folder.uri);
  const inspection = config.inspect?.("analysis.stubPath");
  const explicitlyConfigured = inspection && [inspection.globalValue, inspection.workspaceValue, inspection.workspaceFolderValue].some(value => value !== undefined);
  const configured = explicitlyConfigured
    ? config.get("analysis.stubPath", "") : "";
  let setting = typeof configured === "string" ? configured.trim() : "";
  const stateKey = `managed-analysis-stub:${createHash("sha256").update(normalized(workspace)).digest("hex")}`;
  const previousOwner = workspaceState?.get?.(stateKey);
  const owned = Boolean(previousOwner?.base === ".graphql-desk/typings" && previousOwner.current === setting);
  let ownsSetting = owned;
  let baseSetting = owned ? previousOwner.base : setting;
  const ownsCurrentValue = expected => {
    const current = config.inspect?.("analysis.stubPath");
    if (current) {
      const configuredValues = [current.globalValue, current.workspaceValue, current.workspaceFolderValue]
        .filter(value => value !== undefined);
      if (expected === "") return configuredValues.length === 0;
      // VS Code may serialize a WorkspaceFolder target as workspaceValue for a
      // single-folder workspace. Ownership is the persisted value, not that layer.
      return configuredValues.length > 0 && configuredValues.every(value => value === expected);
    }
    return config.get("analysis.stubPath", "") === expected;
  };
  const ownershipError = () => `Pylance stubPath ownership changed; preserved (expected ${JSON.stringify(setting)}, actual ${JSON.stringify(config.inspect?.("analysis.stubPath"))})`;
  if (!setting && explicitlyConfigured) {
    setting = "typings";
  } else if (!setting) {
    const target = path.join(workspace, ".vscode", "settings.json");
    const workspaceFile = vscode.workspace.workspaceFile?.scheme === "file" ? vscode.workspace.workspaceFile.fsPath : null;
    const dirty = vscode.workspace.textDocuments?.some(document => document.isDirty &&
      document.uri.scheme === "file" && document.uri.fsPath &&
      (normalized(document.uri.fsPath) === normalized(target) || (workspaceFile && normalized(document.uri.fsPath) === normalized(workspaceFile))));
    if (dirty) throw new Error("Dirty workspace settings; preserved");
    setting = "";
    ownsSetting = true;
    baseSetting = ".graphql-desk/typings";
    await workspaceState?.update?.(stateKey, { base: baseSetting, current: setting });
  }
  if (ownsSetting && !inside(workspace, path.resolve(workspace, baseSetting))) throw new Error("Owned Pylance stubPath is outside the workspace; preserved");
  const root = path.resolve(workspace, ownsSetting ? baseSetting : setting);
  if (!inside(workspace, root)) throw new Error("Configured Pylance stubPath is outside the workspace; preserved");
  const created = [];
  let selectionQueue = Promise.resolve();
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
      ownsSetting,
      get selectedSetting() { return setting; },
      stillOwnsSetting() { return ownsSetting && ownsCurrentValue(setting); },
      selectGeneration(generationRoot, isCurrent = () => true, verification) {
        const select = async () => {
          if (!ownsSetting || typeof isCurrent !== "function" || isCurrent() !== true) return false;
          const target = path.resolve(generationRoot);
          const generations = path.join(root, "generations");
          if (!inside(generations, target) || target === generations) throw new Error("Invalid managed generation path");
          if (verification?.directory !== target || verification.manifest?.owner !== "graphql-desk-analysis-generation" ||
              verification.manifest.format !== 1 || verification.manifest.id !== path.basename(target) || typeof verification.verify !== "function") throw new Error("Managed generation is not fully verified");
          const verifyGeneration = () => {
            const current = verification.verify();
            if (current.directory !== target || current.manifest.id !== verification.manifest.id ||
                current.manifest.artifactFingerprint !== verification.manifest.artifactFingerprint) throw new Error("Managed generation changed; preserved");
          };
          verifyGeneration();
          let current = workspace;
          for (const part of path.relative(workspace, target).split(path.sep).filter(Boolean)) {
            current = path.join(current, part);
            const stat = fs.lstatSync(current);
            if (stat.isSymbolicLink() || !stat.isDirectory()) throw new Error("Managed generation is not a safe directory");
          }
          if (!ownsCurrentValue(setting)) throw new Error(ownershipError());
          const targetSetting = path.relative(workspace, target).split(path.sep).join("/");
          const settingsPath = path.join(workspace, ".vscode", "settings.json");
          const workspaceFile = vscode.workspace.workspaceFile?.scheme === "file" ? vscode.workspace.workspaceFile.fsPath : null;
          const dirty = vscode.workspace.textDocuments?.some(document => document.isDirty && document.uri.scheme === "file" && document.uri.fsPath &&
            (normalized(document.uri.fsPath) === normalized(settingsPath) || (workspaceFile && normalized(document.uri.fsPath) === normalized(workspaceFile))));
          if (dirty) throw new Error("Dirty workspace settings; preserved");
          const oldSetting = setting;
          setting = targetSetting;
          await workspaceState?.update?.(stateKey, { base: baseSetting, current: setting });
          try {
            verifyGeneration();
            if (!ownsCurrentValue(oldSetting)) throw new Error(ownershipError());
            await config.update("analysis.stubPath", targetSetting, vscode.ConfigurationTarget.WorkspaceFolder);
            verifyGeneration();
            if (isCurrent() !== true && ownsCurrentValue(targetSetting)) {
              setting = oldSetting;
              await config.update("analysis.stubPath", oldSetting || undefined, vscode.ConfigurationTarget.WorkspaceFolder);
              await workspaceState?.update?.(stateKey, { base: baseSetting, current: setting });
              return false;
            }
            return ownsCurrentValue(targetSetting);
          } catch (error) {
            if (ownsCurrentValue(targetSetting)) {
              await config.update("analysis.stubPath", oldSetting || undefined, vscode.ConfigurationTarget.WorkspaceFolder);
            }
            if (ownsCurrentValue(oldSetting)) {
              setting = oldSetting;
              await workspaceState?.update?.(stateKey, { base: baseSetting, current: setting });
            } else if (!ownsCurrentValue(targetSetting)) {
              // A third value belongs to the user; stop managing it and leave it intact.
              ownsSetting = false;
              setting = config.get("analysis.stubPath", "");
              await workspaceState?.update?.(stateKey, undefined);
            }
            throw error;
          }
        };
        const pending = selectionQueue.then(select, select);
        selectionQueue = pending.catch(() => {});
        return pending;
      },
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
