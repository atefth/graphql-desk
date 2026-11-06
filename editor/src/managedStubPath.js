"use strict";

const fs = require("node:fs");
const path = require("node:path");
const { createHash } = require("node:crypto");

const normalized = value => process.platform === "win32" ? path.resolve(value).toLowerCase() : path.resolve(value);
const inside = (root, value) => {
  const relative = path.relative(root, value);
  return relative === "" || (!path.isAbsolute(relative) && relative !== ".." && !relative.startsWith(`..${path.sep}`));
};
const stateUpdateQueues = new WeakMap();

function serializeStateUpdate(workspaceState, key, update) {
  if (!workspaceState || (typeof workspaceState !== "object" && typeof workspaceState !== "function")) return Promise.resolve().then(update);
  let queues = stateUpdateQueues.get(workspaceState);
  if (!queues) stateUpdateQueues.set(workspaceState, queues = new Map());
  const pending = (queues.get(key) ?? Promise.resolve()).then(update, update);
  queues.set(key, pending.catch(() => {}));
  return pending;
}

function updateManagedState(workspaceState, key, value) {
  return serializeStateUpdate(workspaceState, key, () => workspaceState?.update?.(key, value));
}

function isManagedGeneration(workspace, setting, baseSetting = ".graphql-desk/typings") {
  const relative = setting.replaceAll("\\", "/");
  const prefix = baseSetting.replaceAll("\\", "/").replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const match = new RegExp(`^${prefix}/generations/(g-\\d+-[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12})$`).exec(relative);
  if (!match) return false;
  const directory = path.resolve(workspace, ...relative.split("/"));
  if (!inside(path.join(workspace, ...baseSetting.split("/").concat("generations")), directory)) return false;
  let current = workspace;
  try {
    for (const part of path.relative(workspace, directory).split(path.sep)) {
      current = path.join(current, part);
      const stat = fs.lstatSync(current);
      if (stat.isSymbolicLink() || !stat.isDirectory()) return false;
    }
    const manifestPath = path.join(directory, "complete.json");
    const stat = fs.lstatSync(manifestPath);
    if (stat.isSymbolicLink() || !stat.isFile() || stat.size > 18 * 1024 * 1024) return false;
    const manifest = JSON.parse(fs.readFileSync(manifestPath, "utf8"));
    return manifest?.owner === "graphql-desk-analysis-generation" && manifest.format === 2 &&
      manifest.id === match[1] && Number.isSafeInteger(manifest.sequence) && Array.isArray(manifest.files) &&
      Array.isArray(manifest.operationManifest) && /^[a-f0-9]{64}$/.test(manifest.inputFingerprint) &&
      /^[a-f0-9]{64}$/.test(manifest.artifactFingerprint);
  } catch { return false; }
}

async function createManagedStubPath(vscode, folder, workspaceState, options = {}) {
  if (!folder || folder.uri.scheme !== "file" || !path.isAbsolute(folder.uri.fsPath) || !vscode.workspace.isTrusted) throw new Error("Trusted file workspace required");
  const workspace = path.resolve(folder.uri.fsPath);
  const config = options.config ?? vscode.workspace.getConfiguration("python", folder.uri);
  const managedBase = options.baseSetting ?? ".graphql-desk/typings";
  const suffix = options.stateSuffix ?? "";
  const inspection = config.inspect?.("analysis.stubPath");
  const explicitlyConfigured = inspection && [inspection.globalValue, inspection.workspaceValue, inspection.workspaceFolderValue].some(value => value !== undefined);
  const configured = explicitlyConfigured
    ? config.get("analysis.stubPath", "") : "";
  let setting = typeof configured === "string" ? configured.trim() : "";
  const keys = managedStateKeys(workspace, suffix);
  const { stateKey, generationStateKey, stagingStateKey } = keys;
  const previousOwner = workspaceState?.get?.(stateKey);
  const inspectedSettings = [inspection?.globalValue, inspection?.workspaceValue, inspection?.workspaceFolderValue]
    .filter(value => value !== undefined);
  const selectedWorkspaceSetting = (inspection?.workspaceValue === setting || inspection?.workspaceFolderValue === setting) &&
    inspectedSettings.every(value => value === setting);
  const owned = Boolean(previousOwner?.base === managedBase &&
    (previousOwner.current === setting || (previousOwner.pending === true && previousOwner.previous === setting)) ||
    !previousOwner && selectedWorkspaceSetting && isManagedGeneration(workspace, setting, managedBase));
  let settingPresent = ownedSettingPresent(previousOwner, setting, explicitlyConfigured);
  if (options.forceOwned === true && previousOwner && !owned) throw new Error("Pyright stubPath ownership changed; preserved");
  let ownsSetting = options.forceOwned === true || owned;
  let baseSetting = owned ? previousOwner?.base ?? managedBase : ownsSetting ? managedBase : setting;
  const ownsCurrentValue = expected => {
    const current = config.inspect?.("analysis.stubPath");
    if (current) {
      // VS Code may serialize a WorkspaceFolder target as workspaceValue for a
      // single-folder workspace. Compare the effective explicit layer so an owned
      // empty override remains distinct from an inherited nonempty setting.
      const value = current.workspaceFolderValue !== undefined ? current.workspaceFolderValue :
        current.workspaceValue !== undefined ? current.workspaceValue : current.globalValue ?? "";
      return value === expected;
    }
    return config.get("analysis.stubPath", "") === expected;
  };
  const ownershipError = () => `Pylance stubPath ownership changed; preserved (expected ${JSON.stringify(setting)}, actual ${JSON.stringify(config.inspect?.("analysis.stubPath"))})`;
  if (!setting && explicitlyConfigured && options.forceOwned !== true) {
    setting = "typings";
  } else if (!setting && options.forceOwned !== true) {
    const target = path.join(workspace, ".vscode", "settings.json");
    const workspaceFile = vscode.workspace.workspaceFile?.scheme === "file" ? vscode.workspace.workspaceFile.fsPath : null;
    const dirty = vscode.workspace.textDocuments?.some(document => document.isDirty &&
      document.uri.scheme === "file" && document.uri.fsPath &&
      (normalized(document.uri.fsPath) === normalized(target) || (workspaceFile && normalized(document.uri.fsPath) === normalized(workspaceFile))));
    if (dirty) throw new Error("Dirty workspace settings; preserved");
    setting = "";
    ownsSetting = true;
    baseSetting = ".graphql-desk/typings";
    settingPresent = false;
    await workspaceState?.update?.(stateKey, { base: baseSetting, current: setting, currentPresent: false });
  }
  if (options.forceOwned === true && !previousOwner) {
    const dirty = options.isDirty?.();
    if (dirty) throw new Error("Dirty Pyright config buffer; preserved");
    await workspaceState?.update?.(stateKey, { base: baseSetting, current: setting, currentPresent: settingPresent });
  }
  if (ownsSetting && !inside(workspace, path.resolve(workspace, baseSetting))) throw new Error("Owned Pylance stubPath is outside the workspace; preserved");
  const root = path.resolve(workspace, ownsSetting ? baseSetting : setting);
  if (!inside(workspace, root)) throw new Error("Configured Pylance stubPath is outside the workspace; preserved");
  const created = [];
  let selectionQueue = Promise.resolve();
  const serializeSelection = operation => {
    const pending = selectionQueue.then(operation, operation);
    selectionQueue = pending.catch(() => {});
    return pending;
  };
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
      get ownsSetting() { return ownsSetting; },
      get selectedSetting() { return setting; },
      waitForGenerationSelection() { return selectionQueue; },
      checkSettingOwnership(check = () => true) {
        return serializeSelection(() => ownsSetting && ownsCurrentValue(setting) && check());
      },
      stillOwnsSetting() { return ownsSetting && ownsCurrentValue(setting); },
      ownsGeneration(directory, manifestFingerprint) {
        const target = path.resolve(directory);
        const generations = path.join(root, "generations");
        if (!ownsSetting || !inside(generations, target) || target === generations || !/^[a-f0-9]{64}$/.test(manifestFingerprint ?? "")) return false;
        const records = workspaceState?.get?.(generationStateKey);
        return records?.[path.basename(target)] === manifestFingerprint;
      },
      recordGeneration(directory, manifestFingerprint) {
        const target = path.resolve(directory);
        const generations = path.join(root, "generations");
        if (!ownsSetting || !ownsCurrentValue(setting) || !inside(generations, target) || target === generations ||
            !/^g-[0-9]+-[0-9a-f-]{36}$/i.test(path.basename(target)) || !/^[a-f0-9]{64}$/.test(manifestFingerprint ?? "")) return Promise.resolve(false);
        const record = async () => {
          const current = workspaceState?.get?.(generationStateKey);
          const records = current && typeof current === "object" && !Array.isArray(current) ? current : {};
          await workspaceState?.update?.(generationStateKey, { ...records, [path.basename(target)]: manifestFingerprint });
          return this.ownsGeneration(target, manifestFingerprint);
        };
        return serializeStateUpdate(workspaceState, generationStateKey, record);
      },
      forgetGeneration(directory, manifestFingerprint) {
        const target = path.resolve(directory);
        const current = workspaceState?.get?.(generationStateKey);
        if (!ownsSetting || !inside(path.join(root, "generations"), target) || current?.[path.basename(target)] !== manifestFingerprint) return Promise.resolve(false);
        const forget = async () => {
          const records = workspaceState?.get?.(generationStateKey);
          if (records?.[path.basename(target)] !== manifestFingerprint) return false;
          const next = { ...records };
          delete next[path.basename(target)];
          await workspaceState?.update?.(generationStateKey, next);
          return true;
        };
        return serializeStateUpdate(workspaceState, generationStateKey, forget);
      },
      ownsStaging(directory, markerFingerprint) {
        const target = path.resolve(directory);
        const generations = path.join(root, "generations");
        if (!ownsSetting || !inside(generations, target) || target === generations || !/^[a-f0-9]{64}$/.test(markerFingerprint ?? "")) return false;
        const records = workspaceState?.get?.(stagingStateKey);
        return records?.[path.basename(target)]?.fingerprint === markerFingerprint;
      },
      stagingEntries() {
        const records = workspaceState?.get?.(stagingStateKey);
        return records && typeof records === "object" && !Array.isArray(records)
          ? Object.fromEntries(Object.entries(records).map(([id, entry]) => [id, entry && typeof entry === "object" ? { ...entry } : entry]))
          : {};
      },
      recordStaging(directory, markerFingerprint, markerTemporary) {
        const target = path.resolve(directory);
        const generations = path.join(root, "generations");
        if (!inside(generations, target) || target === generations ||
            !/^g-[0-9]+-[0-9a-f-]{36}$/i.test(path.basename(target)) || !/^[a-f0-9]{64}$/.test(markerFingerprint ?? "") ||
            !/^\.graphql-desk-[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}-marker-[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}\.tmp$/i.test(markerTemporary ?? "")) return Promise.resolve(false);
        const record = async () => {
          if (!ownsSetting) throw new Error("Managed stubPath is not owned; preserved");
          if (!ownsCurrentValue(setting)) throw new Error(ownershipError());
          const current = workspaceState?.get?.(stagingStateKey);
          const records = current && typeof current === "object" && !Array.isArray(current) ? current : {};
          await workspaceState?.update?.(stagingStateKey, { ...records, [path.basename(target)]: { fingerprint: markerFingerprint, markerTemporary } });
          if (!this.ownsStaging(target, markerFingerprint)) throw new Error("Staging ownership record did not persist; preserved");
          return true;
        };
        return serializeSelection(() => serializeStateUpdate(workspaceState, stagingStateKey, record));
      },
      forgetStaging(directory, markerFingerprint) {
        const target = path.resolve(directory);
        const current = workspaceState?.get?.(stagingStateKey);
        if (!ownsSetting || !inside(path.join(root, "generations"), target) || current?.[path.basename(target)]?.fingerprint !== markerFingerprint) return Promise.resolve(false);
        const forget = async () => {
          const records = workspaceState?.get?.(stagingStateKey);
          if (records?.[path.basename(target)]?.fingerprint !== markerFingerprint) return false;
          const next = { ...records };
          delete next[path.basename(target)];
          await workspaceState?.update?.(stagingStateKey, next);
          return true;
        };
        return serializeStateUpdate(workspaceState, stagingStateKey, forget);
      },
      selectGeneration(generationRoot, isCurrent = () => true, verification) {
        const select = async () => {
          if (!ownsSetting || typeof isCurrent !== "function" || isCurrent() !== true) return false;
          const target = path.resolve(generationRoot);
          const generations = path.join(root, "generations");
          if (!inside(generations, target) || target === generations) throw new Error("Invalid managed generation path");
          if (verification?.directory !== target || verification.manifest?.owner !== "graphql-desk-analysis-generation" ||
              verification.manifest.format !== 2 || verification.manifest.id !== path.basename(target) ||
              !this.ownsGeneration(target, verification.manifestFingerprint) || typeof verification.verify !== "function") throw new Error("Managed generation is not fully verified");
          const verifyGeneration = () => {
            const current = verification.verify();
            if (current.directory !== target || current.manifest.id !== verification.manifest.id ||
                current.manifest.artifactFingerprint !== verification.manifest.artifactFingerprint ||
                current.manifestFingerprint !== verification.manifestFingerprint || !this.ownsGeneration(target, current.manifestFingerprint)) throw new Error("Managed generation changed; preserved");
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
            (normalized(document.uri.fsPath) === normalized(settingsPath) || (workspaceFile && normalized(document.uri.fsPath) === normalized(workspaceFile)))) ||
            options.isDirty?.();
          if (dirty) throw new Error("Dirty workspace settings; preserved");
          const oldSetting = setting;
          const oldSettingPresent = settingPresent;
          try {
            setting = targetSetting;
            settingPresent = true;
            await workspaceState?.update?.(stateKey, { base: baseSetting, current: setting,
              previous: oldSetting, previousPresent: oldSettingPresent, pending: true });
            verifyGeneration();
            if (!ownsCurrentValue(oldSetting)) throw new Error(ownershipError());
            verifyGeneration();
            if (options.config) {
              await config.update("analysis.stubPath", targetSetting, vscode.ConfigurationTarget.WorkspaceFolder,
                () => { verifyGeneration(); return isCurrent() === true; });
            } else {
              await config.update("analysis.stubPath", targetSetting, vscode.ConfigurationTarget.WorkspaceFolder);
            }
            verifyGeneration();
            await workspaceState?.update?.(stateKey, { base: baseSetting, current: setting, currentPresent: true });
            settingPresent = true;
            if (isCurrent() !== true && ownsCurrentValue(targetSetting)) {
              setting = oldSetting;
              await config.update("analysis.stubPath", oldSettingPresent ? oldSetting : undefined, vscode.ConfigurationTarget.WorkspaceFolder);
              settingPresent = oldSettingPresent;
              await workspaceState?.update?.(stateKey, { base: baseSetting, current: setting, currentPresent: settingPresent });
              return false;
            }
            if (!ownsCurrentValue(targetSetting)) throw new Error(ownershipError());
            return true;
          } catch (error) {
            if (ownsCurrentValue(targetSetting)) {
              await config.update("analysis.stubPath", oldSettingPresent ? oldSetting : undefined, vscode.ConfigurationTarget.WorkspaceFolder);
            }
            if (ownsCurrentValue(oldSetting)) {
              setting = oldSetting;
              settingPresent = oldSettingPresent;
              try { await workspaceState?.update?.(stateKey, { base: baseSetting, current: setting, currentPresent: settingPresent }); }
              catch { /* A failed state write before the setting change leaves the prior record authoritative. */ }
            } else if (!ownsCurrentValue(targetSetting)) {
              // A third value belongs to the user; stop managing it and leave it intact.
              ownsSetting = false;
              setting = config.get("analysis.stubPath", "");
              await workspaceState?.update?.(stateKey, undefined);
            }
            throw error;
          }
        };
        return serializeSelection(select);
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
      async release() {
        if (options.restoreOnRelease !== true || !ownsSetting) return;
        if (!ownsCurrentValue(setting)) throw new Error(ownershipError());
        const restored = options.restoreSetting;
        await config.update("analysis.stubPath", restored, vscode.ConfigurationTarget.WorkspaceFolder);
        if (!ownsCurrentValue(restored ?? "")) throw new Error("Original Pyright stubPath restoration could not be verified; preserved");
        setting = restored ?? "";
        ownsSetting = false;
        await workspaceState?.update?.(stateKey, undefined);
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

function ownedSettingPresent(previousOwner, setting, explicitlyConfigured) {
  if (!previousOwner || (previousOwner.current !== setting &&
      !(previousOwner.pending === true && previousOwner.previous === setting))) return explicitlyConfigured;
  if (previousOwner.pending === true && previousOwner.previous === setting) {
    return previousOwner.previousPresent ?? (setting !== "");
  }
  return previousOwner.currentPresent ?? (setting !== "");
}

function managedStateKeys(workspace, suffix = "") {
  const digest = createHash("sha256").update(normalized(workspace)).digest("hex");
  return {
    stateKey: `managed-analysis-stub:${digest}${suffix}`,
    generationStateKey: `managed-analysis-generations:${digest}${suffix}`,
    stagingStateKey: `managed-analysis-staging:${digest}${suffix}`,
  };
}

module.exports = { createManagedStubPath, managedStateKeys, isManagedGeneration, updateManagedState };
