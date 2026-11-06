"use strict";

const fs = require("node:fs");
const path = require("node:path");
const { randomUUID, createHash } = require("node:crypto");
const { TextDecoder } = require("node:util");
const { parse, parseTree, modify, applyEdits } = require("jsonc-parser");
const { createManagedStubPath, managedStateKeys, isManagedGeneration, updateManagedState } = require("./managedStubPath");

const MANAGED_PATH = ".graphql-desk/live-typings";
const OWNER_FILE = "project-config-owner.json";
const MAX_CONFIG_BYTES = 1024 * 1024;
const normalized = value => process.platform === "win32" ? path.resolve(value).toLowerCase() : path.resolve(value);
const same = (left, right) => left.dev === right.dev && left.ino === right.ino;
const stateKey = workspace => `managed-pyright-extra-path:${createHash("sha256").update(normalized(workspace)).digest("hex")}`;
const pendingConfigWriteKey = workspace => `${stateKey(workspace)}:pending-config-write`;
const activeConfigWrites = new Set();
const validToken = value => typeof value === "string" && /^[0-9a-f]{32}$/.test(value);
const validHash = value => typeof value === "string" && /^[0-9a-f]{64}$/.test(value);
const validReceipt = value => typeof value === "string" && value.length > 0 && value.length <= 32768;
const sameConfigIdentity = (left, right, published = false) => same(left, right) &&
  ["size", "mtimeNs", "mode", "uid", "gid", ...(published ? [] : ["ctimeNs"])].every(key => left[key] === right[key]);
const sameDirectory = (left, right) => same(left, right) && ["mode", "uid", "gid"].every(key => left[key] === right[key]);
const dirtyFile = (vscode, file) => vscode.workspace.textDocuments?.some(document => document.isDirty &&
  document.uri.scheme === "file" && normalized(document.uri.fsPath) === normalized(file)) === true;

function checkConfigWorkspace(vscode, folder, workspace, file) {
  const owner = vscode.workspace.getWorkspaceFolder(vscode.Uri.file(file));
  if (!vscode.workspace.isTrusted || !owner || folder.uri.scheme !== "file" ||
      normalized(owner.uri.fsPath) !== normalized(workspace) || normalized(folder.uri.fsPath) !== normalized(workspace)) {
    throw new Error("Trusted Pyright config workspace changed; preserved");
  }
  if (dirtyFile(vscode, file)) throw new Error("Dirty Pyright config buffer; preserved");
}

async function configPhase(configFile, folder, action, payload, token) {
  const reply = await configFile(folder, action, payload);
  if (!reply || reply.ok !== true || !validToken(reply.token) || token && reply.token !== token) {
    throw new Error("Pyright config permission proof could not be verified; preserved");
  }
  return reply;
}

async function discardOwnedConfigStage(vscode, folder, workspace, temporary, hash, persistDiscarding, phase, checkRecord = () => {}) {
  let journalCurrent = () => true;
  const check = () => {
    checkRecord();
    if (journalCurrent() !== true) throw new Error("Pyright config write journal changed; preserved");
    checkConfigWorkspace(vscode, folder, workspace, temporary);
  };
  check();
  const directory = safeIdentity(workspace, true, true);
  let staged;
  try { staged = readFileSafe(vscode, workspace, temporary); }
  catch (error) { if (error.code !== "ENOENT") throw error; }
  if (staged && staged.hash !== hash) throw new Error("Pyright config stage changed before cleanup; preserved");
  const saved = await persistDiscarding();
  if (typeof saved === "function") journalCurrent = saved;
  else if (saved !== true) throw new Error("Pyright config cleanup receipt could not be saved; preserved");
  check();
  await phase("verify_discard");
  // The native check cannot see dirty editor buffers. No await before unlink.
  check();
  if (!sameDirectory(directory, safeIdentity(workspace, true, true))) throw new Error("Pyright config workspace changed during cleanup; preserved");
  if (staged) {
    const current = readFileSafe(vscode, workspace, temporary);
    if (!sameConfigIdentity(staged.identity, current.identity) || current.hash !== staged.hash) throw new Error("Pyright config stage changed during cleanup; preserved");
    fs.unlinkSync(temporary);
  } else {
    try { fs.lstatSync(temporary, { bigint: true }); }
    catch (error) { if (error.code !== "ENOENT") throw error; await phase("discard"); check(); return; }
    throw new Error("Pyright config stage appeared during cleanup; preserved");
  }
  await phase("discard");
  check();
}

async function recoverPendingConfigWrite(vscode, folder, workspace, workspaceState, configFile) {
  const key = pendingConfigWriteKey(workspace);
  const pending = workspaceState?.get?.(key);
  if (pending === undefined) return;
  const lock = normalized(workspace);
  if (activeConfigWrites.has(lock)) throw new Error("Another Pyright config write is pending; preserved");
  if (!pending || typeof pending !== "object" || Array.isArray(pending)) {
    throw new Error("Pending Pyright config write needs manual recovery; preserved");
  }
  const temporary = typeof pending.temporaryPath === "string" ? path.resolve(pending.temporaryPath) : "";
  const target = path.join(workspace, typeof pending.file === "string" ? pending.file : "");
  if (pending.version !== 2 || !["pyproject.toml", "pyrightconfig.json"].includes(pending.file) ||
      path.dirname(temporary) !== path.dirname(target) || !/^\.graphql-desk-[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}\.tmp$/i.test(path.basename(temporary)) ||
      !validHash(pending.hash) || !validHash(pending.previousHash) || !validReceipt(pending.receipt) || !validToken(pending.token) ||
      Object.hasOwn(pending, "discarding") && typeof pending.discarding !== "boolean" ||
      typeof configFile !== "function") {
    throw new Error("Pending Pyright config write needs manual recovery; permission proof unavailable; preserved");
  }
  const check = () => {
    checkConfigWorkspace(vscode, folder, workspace, target);
    checkConfigWorkspace(vscode, folder, workspace, temporary);
    if (JSON.stringify(workspaceState.get(key)) !== saved) throw new Error("Pyright recovery record changed; preserved");
  };
  let saved = JSON.stringify(pending);
  activeConfigWrites.add(lock);
  try {
    check();
    let present = true;
    try { fs.lstatSync(temporary, { bigint: true }); }
    catch (error) { if (error.code !== "ENOENT") throw error; present = false; }
    const phase = async action => {
      try { await configPhase(configFile, folder, action, { token: pending.token }, pending.token); }
      catch {
        check();
        await configPhase(configFile, folder, "resume", { receipt: pending.receipt }, pending.token);
        check();
        await configPhase(configFile, folder, action, { token: pending.token }, pending.token);
      }
    };
    if (pending.discarding === true || present) {
      await discardOwnedConfigStage(vscode, folder, workspace, temporary, pending.hash, async () => {
        check();
        const next = { ...pending, discarding: true };
        await workspaceState.update(key, next);
        saved = JSON.stringify(next);
        check();
        return true;
      }, phase, check);
    } else await phase("finish");
    check();
    if (JSON.stringify(workspaceState.get(key)) !== saved) throw new Error("Pyright recovery record changed; preserved");
    await workspaceState.update(key, undefined);
  } finally { activeConfigWrites.delete(lock); }
}

async function acknowledgePendingConfigWrite(vscode, folder, workspaceState, invalidate) {
  if (!folder || folder.uri.scheme !== "file" || !path.isAbsolute(folder.uri.fsPath) || typeof invalidate !== "function") {
    throw new Error("Trusted file workspace required");
  }
  const workspace = path.resolve(folder.uri.fsPath);
  const key = pendingConfigWriteKey(workspace);
  const pending = workspaceState?.get?.(key);
  if (pending === undefined) return false;
  const expectedKeys = pending?.version === 1 ? ["version", "file", "temporaryPath", "hash"] :
    ["version", "file", "temporaryPath", "hash", "previousHash", "receipt", "token", ...(pending && Object.hasOwn(pending, "discarding") ? ["discarding"] : [])];
  if (!pending || typeof pending !== "object" || Array.isArray(pending) || ![1, 2].includes(pending.version) ||
      Object.keys(pending).sort().join(",") !== expectedKeys.sort().join(",") || !validHash(pending.hash) ||
      pending.version === 2 && (!validHash(pending.previousHash) || !(pending.receipt === null || validReceipt(pending.receipt)) ||
        !(pending.token === null || validToken(pending.token)) || Object.hasOwn(pending, "discarding") && typeof pending.discarding !== "boolean") ||
      !["pyproject.toml", "pyrightconfig.json"].includes(pending.file) ||
      typeof pending.temporaryPath !== "string" || !path.isAbsolute(pending.temporaryPath) ||
      path.dirname(path.resolve(pending.temporaryPath)) !== workspace ||
      !/^\.graphql-desk-[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}\.tmp$/i.test(path.basename(pending.temporaryPath)) ||
      typeof workspaceState.update !== "function") throw new Error("Unrecognized Pyright recovery record; preserved");
  const file = path.join(workspace, pending.file);
  const saved = JSON.stringify(pending);
  const directory = safeIdentity(workspace, true, true);
  const lock = normalized(workspace);
  if (activeConfigWrites.has(lock)) throw new Error("Another Pyright config write is pending; preserved");
  activeConfigWrites.add(lock);
  try {
    checkConfigWorkspace(vscode, folder, workspace, file);
    const choice = await vscode.window.showWarningMessage("Review the interrupted GraphQL Desk config write", {
      modal: true,
      detail: `Review these files first:\n${file}\n${pending.temporaryPath}\n\nForget only this pending write record and keep both files. New updates will run the normal permission and ownership checks again.`,
    }, "I reviewed the files; forget this record");
    if (choice !== "I reviewed the files; forget this record") return false;
    checkConfigWorkspace(vscode, folder, workspace, file);
    if (!sameDirectory(directory, safeIdentity(workspace, true, true)) || JSON.stringify(workspaceState.get(key)) !== saved) {
      throw new Error("Pyright recovery record or workspace changed; preserved");
    }
    invalidate();
    await workspaceState.update(key, undefined);
    return true;
  } finally { activeConfigWrites.delete(lock); }
}

function pythonStubSettings(vscode, folder, workspace) {
  const configuration = vscode.workspace.getConfiguration("python", folder.uri);
  const setting = "analysis.stubPath";
  const workspaceFile = vscode.workspace.workspaceFile?.scheme === "file" ? vscode.workspace.workspaceFile.fsPath : null;
  const singleFolderDirectory = (vscode.workspace.workspaceFolders?.length ?? 1) === 1 && !workspaceFile;
  const snapshot = () => {
    const value = configuration.inspect?.(setting) ?? {};
    return {
      folderPresent: value.workspaceFolderValue !== undefined,
      folderValue: value.workspaceFolderValue ?? null,
      workspacePresent: value.workspaceValue !== undefined,
      workspaceValue: value.workspaceValue ?? null,
      globalPresent: value.globalValue !== undefined,
      globalValue: value.globalValue ?? null,
      singleFolderDirectory,
    };
  };
  const effective = () => {
    const value = configuration.inspect?.(setting) ?? {};
    if (value.workspaceFolderValue !== undefined) return value.workspaceFolderValue;
    if (value.workspaceValue !== undefined) return value.workspaceValue;
    if (value.globalValue !== undefined) return value.globalValue;
    return "";
  };
  const dirty = () => vscode.workspace.textDocuments?.some(document => document.isDirty && document.uri.scheme === "file" &&
    (normalized(document.uri.fsPath) === normalized(path.join(workspace, ".vscode", "settings.json")) ||
      workspaceFile && normalized(document.uri.fsPath) === normalized(workspaceFile))) === true;
  const update = async value => configuration.update(setting, value === undefined || value === null ? undefined : value, vscode.ConfigurationTarget.WorkspaceFolder);
  const restore = async previous => {
    if (previous.folderPresent) await update(previous.folderValue);
    else if (previous.singleFolderDirectory && previous.workspacePresent) await update(previous.workspaceValue);
    else await update(undefined);
  };
  return { snapshot, effective, dirty, update, restore };
}

function validatePythonStubPath(settings, expected) {
  const current = settings.effective();
  if (current !== expected) throw new Error("Pylance python.analysis.stubPath changed outside GraphQL Desk; preserved");
}

function safeIdentity(file, directory = false, bigint = false) {
  const stat = fs.lstatSync(file, { bigint });
  if (stat.isSymbolicLink() || (directory ? !stat.isDirectory() : !stat.isFile())) throw new Error("Pyright config path is not a safe file");
  return stat;
}

function ensureWorkspaceRoot(workspace, owned) {
  const created = [];
  safeIdentity(workspace, true);
  let current = workspace;
  for (const part of MANAGED_PATH.split("/")) {
    current = path.join(current, part);
    try {
      const stat = safeIdentity(current, true);
      if (current === path.join(workspace, MANAGED_PATH) && !owned && fs.readdirSync(current).length) {
        throw new Error("GraphQL Desk live typing directory already contains unowned files; preserved");
      }
    } catch (error) {
      if (error.code !== "ENOENT") throw error;
      fs.mkdirSync(current);
      created.push([current, fs.lstatSync(current)]);
    }
  }
  return { root: current, created };
}

function cleanupCreated(created) {
  for (const [directory, stat] of [...created].reverse()) {
    try {
      const current = fs.lstatSync(directory);
      if (current.isSymbolicLink() || !same(stat, current) || !current.isDirectory() || fs.readdirSync(directory).length) continue;
      fs.rmdirSync(directory);
    } catch { /* Preserve any directory that is no longer verified as ours. */ }
  }
}

function ownerFile(workspace) {
  return path.join(workspace, ...MANAGED_PATH.split("/"), OWNER_FILE);
}

function readPersistentOwner(workspace) {
  const file = ownerFile(workspace);
  let current = workspace;
  try {
    for (const part of path.relative(workspace, file).split(path.sep)) {
      current = path.join(current, part);
      const stat = fs.lstatSync(current);
      if (stat.isSymbolicLink() || (current === file ? !stat.isFile() : !stat.isDirectory())) {
        throw new Error("GraphQL Desk Pyright ownership record is not a safe file; preserved");
      }
      if (current === file) {
        if (stat.size > 64 * 1024) throw new Error("GraphQL Desk Pyright ownership record is too large; preserved");
        const text = fs.readFileSync(file, "utf8");
        const value = JSON.parse(text);
        if (!value || Object.getPrototypeOf(value) !== Object.prototype) throw new Error("Malformed GraphQL Desk Pyright ownership record; preserved");
        return { value, hash: createHash("sha256").update(text).digest("hex") };
      }
    }
  } catch (error) {
    if (error.code === "ENOENT") return null;
    throw error;
  }
  return null;
}

function writePersistentOwner(workspace, value, expectedHash, createdDirectories) {
  const { root, created: directoriesCreated } = ensureWorkspaceRoot(workspace, true);
  createdDirectories.push(...directoriesCreated);
  const file = path.join(root, OWNER_FILE);
  const serialized = `${JSON.stringify(value, null, 2)}\n`;
  let currentHash = null;
  try {
    const stat = safeIdentity(file);
    if (stat.size > 64 * 1024) throw new Error("GraphQL Desk Pyright ownership record is too large; preserved");
    currentHash = createHash("sha256").update(fs.readFileSync(file)).digest("hex");
  } catch (error) { if (error.code !== "ENOENT") throw error; }
  if (currentHash !== expectedHash) throw new Error("GraphQL Desk Pyright ownership record changed concurrently; preserved");
  const temporary = path.join(root, `.graphql-desk-owner-${randomUUID()}.tmp`);
  let created;
  try {
    const descriptor = fs.openSync(temporary, "wx");
    try { created = fs.fstatSync(descriptor); fs.writeFileSync(descriptor, serialized, "utf8"); fs.fsyncSync(descriptor); }
    finally { fs.closeSync(descriptor); }
    const observed = readPersistentOwner(workspace);
    if ((observed?.hash ?? null) !== expectedHash || !same(created, fs.lstatSync(temporary))) {
      throw new Error("GraphQL Desk Pyright ownership record changed during update; preserved");
    }
    fs.renameSync(temporary, file);
    if (fs.readFileSync(file, "utf8") !== serialized) throw new Error("GraphQL Desk Pyright ownership record update could not be verified");
    return createHash("sha256").update(serialized).digest("hex");
  } catch (error) {
    try { const stat = fs.lstatSync(temporary); if (created && !stat.isSymbolicLink() && same(created, stat)) fs.unlinkSync(temporary); }
    catch { /* Preserve any temporary path that is no longer verified as ours. */ }
    throw error;
  }
}

function removePersistentOwner(workspace, expectedHash) {
  const file = ownerFile(workspace);
  const observed = readPersistentOwner(workspace);
  if (!observed) return;
  if (observed.hash !== expectedHash) throw new Error("GraphQL Desk Pyright ownership record changed during cleanup; preserved");
  const stat = safeIdentity(file);
  if (createHash("sha256").update(fs.readFileSync(file)).digest("hex") !== expectedHash || !same(stat, fs.lstatSync(file))) {
    throw new Error("GraphQL Desk Pyright ownership record changed during cleanup; preserved");
  }
  fs.unlinkSync(file);
}

function parseJsonConfig(text) {
  const errors = [];
  const value = parse(text, errors, { allowTrailingComma: true, disallowComments: false });
  if (errors.length || !value || Object.getPrototypeOf(value) !== Object.prototype) throw new Error("Malformed pyrightconfig.json; preserved");
  const tree = parseTree(text, errors, { allowTrailingComma: true, disallowComments: false });
  if (errors.length || !tree) throw new Error("Malformed pyrightconfig.json; preserved");
  const checkDuplicates = node => {
    if (node.type === "object") {
      const keys = new Set();
      for (const property of node.children ?? []) {
        const key = property.children?.[0]?.value;
        if (typeof key !== "string" || keys.has(key)) throw new Error("Duplicate JSONC config key; preserved");
        keys.add(key);
        if (property.children?.[1]) checkDuplicates(property.children[1]);
      }
    } else for (const child of node.children ?? []) checkDuplicates(child);
  };
  checkDuplicates(tree);
  const extraPaths = value.extraPaths ?? [];
  if (!Array.isArray(extraPaths) || extraPaths.some(item => typeof item !== "string")) throw new Error("Invalid Pyright extraPaths; preserved");
  if (value.stubPath !== undefined && typeof value.stubPath !== "string") throw new Error("Invalid Pyright stubPath; preserved");
  if (value.executionEnvironments !== undefined && !Array.isArray(value.executionEnvironments)) throw new Error("Invalid Pyright executionEnvironments; preserved");
  const roots = new Set();
  const executionEnvironments = (value.executionEnvironments ?? []).map(environment => {
    if (!environment || Object.getPrototypeOf(environment) !== Object.prototype || typeof environment.root !== "string" || !environment.root) {
      throw new Error("Invalid Pyright execution environment root; preserved");
    }
    if (roots.has(environment.root)) throw new Error("Duplicate Pyright execution environment root; preserved");
    roots.add(environment.root);
    const environmentPaths = environment.extraPaths ?? [];
    if (!Array.isArray(environmentPaths) || environmentPaths.some(item => typeof item !== "string")) {
      throw new Error("Invalid execution environment extraPaths; preserved");
    }
    return { root: environment.root, extraPaths: [...environmentPaths], hasExtraPaths: Object.hasOwn(environment, "extraPaths") };
  });
  return { found: true, stubPath: value.stubPath ?? null, hasStubPath: Object.hasOwn(value, "stubPath"), extraPaths: [...extraPaths], hasExtraPaths: Object.hasOwn(value, "extraPaths"),
    executionEnvironments, hasExecutionEnvironments: executionEnvironments.length > 0 };
}

function removeJsonArrayItem(text, arrayPath, index) {
  const errors = [];
  const tree = parseTree(text, errors, { allowTrailingComma: true, disallowComments: false });
  if (errors.length || !tree) throw new Error("Malformed pyrightconfig.json; preserved");
  let array = tree;
  for (const part of arrayPath) {
    if (typeof part === "number" && array.type === "array") array = array.children?.[part];
    else if (typeof part === "string" && array.type === "object") {
      const property = array.children?.find(item => item.type === "property" && item.children?.[0]?.value === part);
      array = property?.children?.[1];
    } else array = undefined;
    if (!array) throw new Error("Pyright config array changed during edit; preserved");
  }
  if (array.type !== "array" || !Array.isArray(array.children) || index < 0 || index >= array.children.length) {
    throw new Error("Pyright config array changed during edit; preserved");
  }
  const item = array.children[index];
  const previous = array.children[index - 1];
  const next = array.children[index + 1];
  const start = next ? item.offset : previous ? previous.offset + previous.length : item.offset;
  const end = next ? next.offset : item.offset + item.length;
  if (/\/\/|\/\*/.test(text.slice(start, end))) throw new Error("Commented Pyright array entry cannot be safely removed; preserved");
  return applyEdits(text, [{ offset: start, length: end - start, content: "" }]);
}

function modifyJsonConfig(text, action, managedPath = MANAGED_PATH, ownership = {}) {
  const inspected = parseJsonConfig(text);
  const indexes = inspected.extraPaths.flatMap((value, index) => value === managedPath ? [index] : []);
  if (indexes.length > 1) throw new Error("Duplicate GraphQL Desk Pyright extraPath; preserved");
  if (inspected.executionEnvironments.some(item => item.extraPaths.filter(value => value === managedPath).length > 1)) {
    throw new Error("Duplicate GraphQL Desk execution environment path; preserved");
  }
  if (action === "inspect") return { ...inspected, text };
  const assertExpectedStubPath = () => {
    if (ownership.expectedStubPathPresent !== inspected.hasStubPath ||
        ownership.expectedStubPathPresent && ownership.expectedStubPath !== inspected.stubPath) {
      throw new Error("Pyright stubPath changed before GraphQL Desk could update it; preserved");
    }
  };
  const customEnvironmentRoots = ownership.executionEnvironmentRoots ?? [];
  if (["prepare_stub_path", "release_stub_path"].includes(action) &&
      (!Array.isArray(customEnvironmentRoots) || new Set(customEnvironmentRoots).size !== customEnvironmentRoots.length ||
        !customEnvironmentRoots.every(root => typeof root === "string" && root))) {
    throw new Error("Invalid custom stub execution environment ownership; preserved");
  }
  if (action === "prepare_stub_path") {
    assertExpectedStubPath();
    let updated = text;
    if (ownership.removeManagedPaths === true) updated = modifyJsonConfig(updated, "remove", managedPath, {
      topLevel: ownership.legacyTopLevel === true,
      executionEnvironmentRoots: Array.isArray(ownership.legacyExecutionEnvironmentRoots) ? ownership.legacyExecutionEnvironmentRoots : [],
    }).text;
    let current = parseJsonConfig(updated);
    if (ownership.customStubPathAdded === true) {
      if (current.extraPaths.includes(ownership.customStubPath)) throw new Error("GraphQL Desk custom stub root appeared during setup; preserved");
      updated = applyEdits(updated, modify(updated, current.hasExtraPaths ? ["extraPaths", current.extraPaths.length] : ["extraPaths"],
        current.hasExtraPaths ? ownership.customStubPath : [ownership.customStubPath],
        { formattingOptions: { insertSpaces: true, tabSize: 2, eol: updated.includes("\r\n") ? "\r\n" : "\n" } }));
      current = parseJsonConfig(updated);
    }
    for (const root of customEnvironmentRoots) {
      current = parseJsonConfig(updated);
      const index = current.executionEnvironments.findIndex(item => item.root === root);
      const environment = current.executionEnvironments[index];
      if (!environment?.hasExtraPaths || environment.extraPaths.includes(ownership.customStubPath)) {
        throw new Error("Custom stub execution environment ownership changed; preserved");
      }
      updated = applyEdits(updated, modify(updated, ["executionEnvironments", index, "extraPaths", environment.extraPaths.length],
        ownership.customStubPath, { formattingOptions: { insertSpaces: true, tabSize: 2, eol: updated.includes("\r\n") ? "\r\n" : "\n" } }));
    }
    updated = applyEdits(updated, modify(updated, ["stubPath"], managedPath,
      { formattingOptions: { insertSpaces: true, tabSize: 2, eol: updated.includes("\r\n") ? "\r\n" : "\n" } }));
    return { ...parseJsonConfig(updated), text: updated };
  }
  if (action === "release_stub_path") {
    assertExpectedStubPath();
    let updated = text;
    if (ownership.customStubPathAdded === true) {
      const current = parseJsonConfig(updated);
      const indexes = current.extraPaths.flatMap((value, index) => value === ownership.customStubPath ? [index] : []);
      if (indexes.length !== 1) throw new Error("GraphQL Desk custom stub root ownership changed; preserved");
      if (ownership.removeExtraPathsKey === true && current.extraPaths.length === 1) updated = applyEdits(updated, modify(updated, ["extraPaths"], undefined,
        { formattingOptions: { insertSpaces: true, tabSize: 2, eol: updated.includes("\r\n") ? "\r\n" : "\n" } }));
      else updated = removeJsonArrayItem(updated, ["extraPaths"], indexes[0]);
    }
    for (const root of customEnvironmentRoots) {
      const current = parseJsonConfig(updated);
      const index = current.executionEnvironments.findIndex(item => item.root === root);
      const environment = current.executionEnvironments[index];
      const indexes = environment?.extraPaths.flatMap((value, index) => value === ownership.customStubPath ? [index] : []) ?? [];
      if (!environment?.hasExtraPaths || indexes.length !== 1) throw new Error("Custom stub execution environment ownership changed; preserved");
      updated = removeJsonArrayItem(updated, ["executionEnvironments", index, "extraPaths"], indexes[0]);
    }
    const restore = ownership.originalStubPathPresent === true ? ownership.originalStubPath : undefined;
    updated = applyEdits(updated, modify(updated, ["stubPath"], restore,
      { formattingOptions: { insertSpaces: true, tabSize: 2, eol: updated.includes("\r\n") ? "\r\n" : "\n" } }));
    return { ...parseJsonConfig(updated), text: updated };
  }
  if (action === "set_stub_path" || action === "restore_stub_path") {
    assertExpectedStubPath();
    if (action === "set_stub_path" && (typeof managedPath !== "string" || !managedPath || path.isAbsolute(managedPath))) {
      throw new Error("Invalid managed Pyright stubPath");
    }
    if (action === "restore_stub_path" && (typeof ownership.restoreStubPathPresent !== "boolean" ||
        ownership.restoreStubPathPresent && typeof ownership.restoreStubPath !== "string")) {
      throw new Error("Invalid original Pyright stubPath; preserved");
    }
    const value = action === "set_stub_path" ? managedPath :
      ownership.restoreStubPathPresent ? ownership.restoreStubPath : undefined;
    const updated = applyEdits(text, modify(text, ["stubPath"], value,
      { formattingOptions: { insertSpaces: true, tabSize: 2, eol: text.includes("\r\n") ? "\r\n" : "\n" } }));
    return { ...parseJsonConfig(updated), text: updated };
  }
  if (action === "add") {
    let updated = text;
    const created = { topLevel: !inspected.hasExtraPaths, executionEnvironmentRoots: inspected.executionEnvironments.filter(item => !item.hasExtraPaths).map(item => item.root) };
    const apply = (source, key, value) => applyEdits(source, modify(source, key, value,
      { formattingOptions: { insertSpaces: true, tabSize: 2, eol: source.includes("\r\n") ? "\r\n" : "\n" } }));
    if (!indexes.length) updated = apply(updated, inspected.hasExtraPaths ? ["extraPaths", inspected.extraPaths.length] : ["extraPaths"],
      inspected.hasExtraPaths ? managedPath : [managedPath]);
    for (let index = 0; index < inspected.executionEnvironments.length; index += 1) {
      const environment = parseJsonConfig(updated).executionEnvironments[index];
      if (!environment.extraPaths.includes(managedPath)) updated = apply(updated,
        environment.hasExtraPaths ? ["executionEnvironments", index, "extraPaths", environment.extraPaths.length] : ["executionEnvironments", index, "extraPaths"],
        environment.hasExtraPaths ? managedPath : [managedPath]);
    }
    return { ...parseJsonConfig(updated), text: updated, ownership: created };
  }
  if (action === "remove") {
    let updated = text;
    const apply = (source, key, value) => applyEdits(source, modify(source, key, value,
      { formattingOptions: { insertSpaces: true, tabSize: 2, eol: source.includes("\r\n") ? "\r\n" : "\n" } }));
    if (indexes.length) {
      const paths = [...inspected.extraPaths]; paths.splice(indexes[0], 1);
      updated = ownership.topLevel === true && paths.length === 0
        ? apply(updated, ["extraPaths"], undefined)
        : removeJsonArrayItem(updated, ["extraPaths"], indexes[0]);
    }
    for (let index = 0; index < inspected.executionEnvironments.length; index += 1) {
      const environment = parseJsonConfig(updated).executionEnvironments[index];
      const environmentIndexes = environment.extraPaths.flatMap((value, pathIndex) => value === managedPath ? [pathIndex] : []);
      if (environmentIndexes.length > 1) throw new Error("Duplicate GraphQL Desk execution environment path; preserved");
      if (!environmentIndexes.length) continue;
      const paths = [...environment.extraPaths]; paths.splice(environmentIndexes[0], 1);
      const created = ownership.executionEnvironmentRoots?.includes(environment.root) === true && paths.length === 0;
      updated = created
        ? apply(updated, ["executionEnvironments", index, "extraPaths"], undefined)
        : removeJsonArrayItem(updated, ["executionEnvironments", index, "extraPaths"], environmentIndexes[0]);
    }
    return { ...parseJsonConfig(updated), text: updated };
  }
  throw new Error("Invalid project Pyright config action");
}

function removeStaleGenerationSearchPaths(text, workspace) {
  let updated = text;
  let inspected = parseJsonConfig(updated);
  const indexes = values => values.flatMap((value, index) => {
    const relative = path.relative(workspace, path.resolve(workspace, value)).split(path.sep).join("/");
    return relative.startsWith(`${MANAGED_PATH}/generations/`) && isManagedGeneration(workspace, relative, MANAGED_PATH) ? [index] : [];
  });
  for (const index of indexes(inspected.extraPaths).reverse()) updated = removeJsonArrayItem(updated, ["extraPaths"], index);
  inspected = parseJsonConfig(updated);
  for (let environment = inspected.executionEnvironments.length - 1; environment >= 0; environment -= 1) {
    const values = inspected.executionEnvironments[environment].extraPaths;
    for (const index of indexes(values).reverse()) updated = removeJsonArrayItem(updated, ["executionEnvironments", environment, "extraPaths"], index);
  }
  return updated;
}

function readFileSafe(vscode, workspace, file) {
  let current = workspace;
  const parts = path.relative(workspace, file).split(path.sep).filter(Boolean);
  if (!parts.length || path.isAbsolute(path.relative(workspace, file)) || path.relative(workspace, file).startsWith(`..${path.sep}`)) {
    throw new Error("Pyright config must be inside the workspace; preserved");
  }
  const workspaceIdentity = safeIdentity(current, true, true);
  for (const [index, part] of parts.entries()) {
    current = path.join(current, part);
    const stat = safeIdentity(current, index < parts.length - 1, true);
    if (index === parts.length - 1) {
      if (stat.size > MAX_CONFIG_BYTES) throw new Error("Pyright config is too large; preserved");
      if (dirtyFile(vscode, current)) throw new Error("Dirty Pyright config buffer; preserved");
      const descriptor = fs.openSync(current, "r");
      try {
        const opened = fs.fstatSync(descriptor, { bigint: true });
        if (!sameConfigIdentity(stat, opened) || !opened.isFile() || opened.size > MAX_CONFIG_BYTES) throw new Error("Pyright config changed while opening; preserved");
        const bytes = Buffer.alloc(Number(opened.size));
        const count = fs.readSync(descriptor, bytes, 0, bytes.length, 0);
        if (count !== bytes.length || !sameConfigIdentity(opened, fs.fstatSync(descriptor, { bigint: true })) ||
            !sameConfigIdentity(opened, safeIdentity(current, false, true)) ||
            !sameDirectory(workspaceIdentity, safeIdentity(workspace, true, true))) throw new Error("Pyright config changed while reading; preserved");
        let text;
        try { text = new TextDecoder("utf-8", { fatal: true, ignoreBOM: true }).decode(bytes); }
        catch { throw new Error("Invalid UTF-8 Pyright config; preserved"); }
        return { text, identity: opened, workspaceIdentity,
          hash: createHash("sha256").update(bytes).digest("hex"), filename: current };
      } finally { fs.closeSync(descriptor); }
    }
  }
}

async function atomicReplaceOwnedConfig(vscode, folder, workspace, file, text, snapshot, onOwnedWrite, configFile, isCurrent) {
  if (typeof configFile !== "function" || typeof onOwnedWrite !== "function") {
    throw new Error("Permission-preserving Pyright config writer and durable journal required; preserved");
  }
  const key = normalized(workspace);
  if (activeConfigWrites.has(key)) throw new Error("Another Pyright config write is pending; preserved");
  activeConfigWrites.add(key);
  const temporary = path.join(path.dirname(file), `.graphql-desk-${randomUUID()}.tmp`);
  const nextHash = createHash("sha256").update(text, "utf8").digest("hex");
  let proof;
  let committed = false;
  let began = false;
  let stageAttempted = false;
  const check = () => {
    if (isCurrent() !== true) throw new Error("Pyright config generation is stale; preserved");
    checkConfigWorkspace(vscode, folder, workspace, file);
    checkConfigWorkspace(vscode, folder, workspace, temporary);
    if (!sameDirectory(snapshot.workspaceIdentity, safeIdentity(workspace, true, true))) {
      throw new Error("Pyright config workspace changed; preserved");
    }
    const current = readFileSafe(vscode, workspace, file);
    if (!sameConfigIdentity(snapshot.identity, current.identity) || current.hash !== snapshot.hash) {
      throw new Error("Pyright config changed concurrently; preserved");
    }
  };
  try {
    check();
    if (await onOwnedWrite(file, nextHash, { phase: "begin", previousHash: snapshot.hash, temporaryPath: temporary }) !== true) {
      throw new Error("GraphQL Desk could not durably record Pyright config write ownership; preserved");
    }
    began = true;
    check();
    stageAttempted = true;
    proof = await configPhase(configFile, folder, "stage", {
      file: path.basename(file), temporary, expected_hash: snapshot.hash, text,
    });
    if (!validReceipt(proof.receipt) || proof.original_hash !== snapshot.hash || proof.staged_hash !== nextHash) {
      proof = undefined;
      throw new Error("Pyright config staging permission proof is incomplete; manual recovery required; preserved");
    }
    const stagedIdentity = safeIdentity(temporary, false, true);
    if (await onOwnedWrite(file, nextHash, { phase: "staged", temporaryPath: temporary,
      token: proof.token, receipt: proof.receipt }) !== true) {
      throw new Error("Pyright config staging permission proof could not be saved; preserved");
    }
    for (let attempt = 0; ; attempt += 1) {
      check();
      await configPhase(configFile, folder, "verify", { token: proof.token }, proof.token);
      // No await between these final freshness guards and the atomic rename.
      check();
      const staged = readFileSafe(vscode, workspace, temporary);
      if (!sameConfigIdentity(stagedIdentity, staged.identity) || staged.hash !== nextHash) {
        throw new Error("Pyright config stage changed concurrently; preserved");
      }
      try { fs.renameSync(temporary, file); committed = true; break; }
      catch (error) {
        if (attempt === 3 || !["EPERM", "EACCES", "EBUSY"].includes(error.code)) throw error;
        await new Promise(resolve => setTimeout(resolve, 50 * (attempt + 1)));
      }
    }
    checkConfigWorkspace(vscode, folder, workspace, file);
    const published = readFileSafe(vscode, workspace, file);
    if (!sameConfigIdentity(stagedIdentity, published.identity, true) || published.hash !== nextHash) {
      throw new Error("Pyright config update could not be verified; preserved");
    }
    await configPhase(configFile, folder, "finish", { token: proof.token }, proof.token);
    await onOwnedWrite(file, nextHash, { phase: "commit", temporaryPath: temporary,
      token: proof.token, receipt: proof.receipt, clearPending: true });
  } catch (error) {
    let clearPending = began && !stageAttempted;
    if (proof && !committed) {
      try {
        await discardOwnedConfigStage(vscode, folder, workspace, temporary, nextHash, async () => {
          return onOwnedWrite(file, nextHash, { phase: "discarding", temporaryPath: temporary,
            token: proof.token, receipt: proof.receipt });
        }, action => configPhase(configFile, folder, action, { token: proof.token }, proof.token));
        clearPending = true;
      } catch { /* Keep the receipt and any uncertain or user-edited stage. */ }
    }
    if (began) {
      try { await onOwnedWrite(file, null, { phase: "cancel", temporaryPath: temporary,
        token: proof?.token, receipt: proof?.receipt, clearPending }); }
      catch { /* Keep durable recovery state if its cleanup cannot be saved. */ }
    }
    throw error;
  } finally { activeConfigWrites.delete(key); }
}

async function invokeToml(folder, text, action, worker, ownership = {}, managedPath = MANAGED_PATH) {
  const result = await worker(folder, text, action, managedPath, ownership);
  if (!result || result.ok !== true || typeof result.found !== "boolean" || typeof result.text !== "string" ||
      !(result.stub_path === null || typeof result.stub_path === "string") || !Array.isArray(result.extra_paths) ||
      typeof result.has_stub_path !== "boolean" ||
      !result.extra_paths.every(value => typeof value === "string") || typeof result.has_execution_environments !== "boolean" ||
      !Array.isArray(result.execution_environments) || !result.execution_environments.every(item => item && typeof item.root === "string" &&
        Array.isArray(item.extra_paths) && item.extra_paths.every(value => typeof value === "string") && typeof item.has_extra_paths === "boolean") ||
      action === "add" && (!result.ownership || typeof result.ownership.top_level !== "boolean" || !Array.isArray(result.ownership.execution_environment_roots))) {
    throw new Error("Malformed pyproject.toml Pyright result; preserved");
  }
  return { found: result.found, stubPath: result.stub_path, hasStubPath: result.has_stub_path, extraPaths: result.extra_paths,
    executionEnvironments: result.execution_environments.map(item => ({ root: item.root, extraPaths: item.extra_paths, hasExtraPaths: item.has_extra_paths })),
    hasExecutionEnvironments: result.has_execution_environments, ...(result.ownership ? { ownership: {
      topLevel: result.ownership.top_level, executionEnvironmentRoots: result.ownership.execution_environment_roots,
    } } : {}), text: result.text };
}

async function updateProjectConfig(vscode, folder, action, worker, ownership = {}, managedPath = MANAGED_PATH, onOwnedWrite, configFile, isCurrent = () => true) {
  if (!folder || folder.uri.scheme !== "file" || !path.isAbsolute(folder.uri.fsPath) || !vscode.workspace.isTrusted) {
    throw new Error("Trusted file workspace required");
  }
  const workspace = path.resolve(folder.uri.fsPath);
  const jsonFile = path.join(workspace, "pyrightconfig.json");
  const tomlFile = path.join(workspace, "pyproject.toml");
  const jsonExists = fs.existsSync(jsonFile);
  const tomlExists = fs.existsSync(tomlFile);
  if (!jsonExists && !tomlExists) return null;
  const file = jsonExists ? jsonFile : tomlFile;
  const snapshot = readFileSafe(vscode, workspace, file);
  const format = jsonExists ? "jsonc" : "toml";
  const inspected = format === "jsonc" ? modifyJsonConfig(snapshot.text, "inspect") : await invokeToml(folder, snapshot.text, "inspect", worker);
  if (!inspected.found) return null;
  if (action === "inspect") return { ...inspected, file, format, identity: snapshot.identity, workspace, hash: snapshot.hash };
  if (action === "remove_stale_generations" && format === "jsonc") {
    const updated = removeStaleGenerationSearchPaths(snapshot.text, workspace);
    const result = modifyJsonConfig(updated, "inspect");
    if (updated !== snapshot.text) {
      await atomicReplaceOwnedConfig(vscode, folder, workspace, file, updated, snapshot, onOwnedWrite, configFile, isCurrent);
    }
    return { ...result, text: updated, file, format, workspace };
  }
    const update = format === "jsonc" ? modifyJsonConfig(snapshot.text, action, managedPath, ownership) :
    await invokeToml(folder, snapshot.text, action, worker, ownership, managedPath);
  if (action === "prepare_stub_path" && update.executionEnvironments.some(environment => environment.hasExtraPaths &&
      !environment.extraPaths.some(value => normalized(path.resolve(path.dirname(file), value)) ===
        normalized(path.resolve(path.dirname(file), ownership.customStubPath))))) {
    throw new Error("Custom stub execution environment coverage changed during setup; preserved");
  }
  if (update.text !== snapshot.text) {
    await atomicReplaceOwnedConfig(vscode, folder, workspace, file, update.text, snapshot, onOwnedWrite, configFile, isCurrent);
  }
  return { ...update, file, format, workspace, hash: createHash("sha256").update(update.text, "utf8").digest("hex") };
}

async function createManagedProjectConfig(vscode, folder, workspaceState, worker, { onOwnedWrite = () => {}, onOwnership = () => {}, configFile } = {}) {
  if (!folder || folder.uri.scheme !== "file" || !path.isAbsolute(folder.uri.fsPath) || !vscode.workspace.isTrusted) {
    throw new Error("Trusted file workspace required");
  }
  await recoverPendingConfigWrite(vscode, folder, path.resolve(folder.uri.fsPath), workspaceState, configFile);
  const inspected = await updateProjectConfig(vscode, folder, "inspect", worker);
  if (!inspected) return null;
  const workspace = inspected.workspace;
  const originalOnOwnedWrite = onOwnedWrite;
  let activeConfigHash = inspected.hash;
  onOwnedWrite = async (filename, fingerprint, operation) => {
    if (!operation) return originalOnOwnedWrite(filename, fingerprint);
    const key = pendingConfigWriteKey(workspace);
    if (operation.phase === "begin") {
      if (workspaceState?.get?.(key) !== undefined || typeof workspaceState?.update !== "function") {
        throw new Error("Pending Pyright config write needs recovery before another write; preserved");
      }
      if (await originalOnOwnedWrite(filename, fingerprint, operation) === false) return false;
      try {
        await workspaceState.update(key, { version: 2, file: path.relative(workspace, filename).split(path.sep).join("/"),
          temporaryPath: operation.temporaryPath, hash: fingerprint, previousHash: operation.previousHash, receipt: null, token: null });
        return true;
      } catch (error) {
        originalOnOwnedWrite(filename, null, { phase: "cancel" });
        throw error;
      }
    }
    const pending = workspaceState.get(key);
    if (!pending || pending.version !== 2 || pending.temporaryPath !== operation.temporaryPath ||
        pending.file !== path.relative(workspace, filename).split(path.sep).join("/") ||
        fingerprint !== null && pending.hash !== fingerprint) {
      throw new Error("Pyright config write journal changed; preserved");
    }
    if (["staged", "discarding"].includes(operation.phase)) {
      if (pending.hash !== fingerprint || !validToken(operation.token) || !validReceipt(operation.receipt)) {
        throw new Error("Pyright config permission receipt changed; preserved");
      }
      if (pending.receipt !== null && (pending.receipt !== operation.receipt || pending.token !== operation.token)) {
        throw new Error("Pyright config recovery receipt changed; preserved");
      }
      const next = { ...pending, token: operation.token, receipt: operation.receipt,
        ...(operation.phase === "discarding" ? { discarding: true } : {}) };
      await workspaceState.update(key, next);
      if (JSON.stringify(workspaceState.get(key)) !== JSON.stringify(next)) throw new Error("Pyright config write journal changed; preserved");
      return operation.phase === "discarding" ? () => JSON.stringify(workspaceState.get(key)) === JSON.stringify(next) : true;
    }
    if (operation.clearPending === true) {
      if (pending.receipt !== null && (pending.receipt !== operation.receipt || pending.token !== operation.token)) {
        throw new Error("Pyright config recovery receipt changed; preserved");
      }
      await workspaceState.update(key, undefined);
    }
    const result = await originalOnOwnedWrite(filename, fingerprint, operation);
    if (operation.phase === "commit" && path.resolve(filename) === path.resolve(inspected.file)) activeConfigHash = fingerprint;
    return result;
  };
  const key = stateKey(workspace);
  const persisted = readPersistentOwner(workspace);
  let ownerHash = persisted?.hash ?? null;
  const ownerCreatedDirectories = [];
  let saved = workspaceState?.get?.(key) ?? persisted?.value;
  const fileKey = path.relative(workspace, inspected.file).split(path.sep).join("/");
  const configDirectory = path.dirname(inspected.file);
  const managedRoot = normalized(path.resolve(configDirectory, MANAGED_PATH));
  const stateSuffix = `:project-${createHash("sha256").update(fileKey).digest("hex").slice(0, 16)}`;
  const generationKeys = managedStateKeys(workspace, stateSuffix);
  const pythonSettings = pythonStubSettings(vscode, folder, workspace);
  const configPaths = config => [config.stubPath ?? "typings", ...config.extraPaths,
    ...config.executionEnvironments.flatMap(item => item.extraPaths)];
  const resolvesToManagedRoot = value => normalized(path.resolve(configDirectory, value)) === managedRoot;
  const hasGlob = value => value.includes("*") || value.includes("?") || /[\[\]{}]/.test(value);
  if (configPaths(inspected).some(hasGlob)) {
    throw new Error("Pyright stubPath/extraPaths globs cannot be checked for generated-package collisions; preserved");
  }
  if (configPaths(inspected).some(resolvesToManagedRoot) && !saved) {
    throw new Error("Pyright config aliases the reserved GraphQL Desk path; preserved");
  }
  const validOwnership = value => value && typeof value.topLevel === "boolean" &&
    Array.isArray(value.executionEnvironmentRoots) && value.executionEnvironmentRoots.every(root => typeof root === "string");
  const validProjectOwner = value => value && value.version === 3 && value.file === fileKey &&
    value.path === MANAGED_PATH && value.format === inspected.format && typeof value.originalStubPathPresent === "boolean" &&
    (value.originalStubPath === null || typeof value.originalStubPath === "string") && typeof value.customStubPath === "string" &&
    typeof value.customStubPathAdded === "boolean" && typeof value.removeExtraPathsKey === "boolean" &&
    (value.customStubPathEnvironmentRoots === undefined || Array.isArray(value.customStubPathEnvironmentRoots) &&
      value.customStubPathEnvironmentRoots.every(root => typeof root === "string" && root) &&
      new Set(value.customStubPathEnvironmentRoots).size === value.customStubPathEnvironmentRoots.length) &&
    typeof value.currentStubPath === "string" && ["prepare", "active", "release", "rotate"].includes(value.phase) &&
    (value.pythonStubPath === undefined || value.pythonStubPath && typeof value.pythonStubPath.current === "string" &&
      value.pythonStubPath.snapshot && typeof value.pythonStubPath.snapshot === "object") &&
    (value.legacyOwnership === null || validOwnership(value.legacyOwnership));
  if (saved?.version === 2 && (saved.file !== fileKey || saved.path !== MANAGED_PATH || saved.format !== inspected.format || !validOwnership(saved.ownership))) {
    throw new Error("Legacy GraphQL Desk Pyright config ownership changed; preserved");
  }
  if (saved?.version === 3 && !validProjectOwner(saved)) throw new Error("GraphQL Desk Pyright config ownership changed; preserved");
  if (saved && ![2, 3].includes(saved.version)) throw new Error("GraphQL Desk Pyright config ownership changed; preserved");
  const persistOwner = async value => {
    ownerHash = writePersistentOwner(workspace, value, ownerHash, ownerCreatedDirectories);
    await workspaceState?.update?.(key, value);
  };
  const clearOwner = async () => {
    removePersistentOwner(workspace, ownerHash);
    ownerHash = null;
    await workspaceState?.update?.(key, undefined);
  };

  const customStubPath = saved?.version === 3 ? saved.customStubPath : inspected.stubPath ?? "typings";
  const customStubPathAdded = saved?.version === 3 ? saved.customStubPathAdded :
    !inspected.extraPaths.some(value => normalized(path.resolve(configDirectory, value)) === normalized(path.resolve(configDirectory, customStubPath)));
  const removeExtraPathsKey = saved?.version === 3 ? saved.removeExtraPathsKey : saved?.version === 2
    ? saved.ownership.topLevel : !inspected.hasExtraPaths;
  const originalStubPathPresent = saved?.version === 3 ? saved.originalStubPathPresent : inspected.hasStubPath;
  const originalStubPath = saved?.version === 3 ? saved.originalStubPath : inspected.stubPath;
  const hasCustomPath = values => values.some(value => normalized(path.resolve(configDirectory, value)) ===
    normalized(path.resolve(configDirectory, customStubPath)));
  const legacyEnvironmentRoots = saved?.version === 2 ? saved.ownership.executionEnvironmentRoots : [];
  const customStubPathEnvironmentRoots = saved?.version === 3 ? saved.customStubPathEnvironmentRoots ?? [] :
    inspected.executionEnvironments.filter(environment => environment.hasExtraPaths && !hasCustomPath(environment.extraPaths) &&
      !(legacyEnvironmentRoots.includes(environment.root) && environment.extraPaths.length === 1 && environment.extraPaths[0] === MANAGED_PATH))
      .map(environment => environment.root);
  if (saved?.version === 3 && saved.customStubPathEnvironmentRoots === undefined && saved.phase !== "release" &&
      inspected.executionEnvironments.some(environment => environment.hasExtraPaths && !hasCustomPath(environment.extraPaths))) {
    throw new Error("Legacy GraphQL Desk execution-environment ownership is missing; restore the original config and clear GraphQL Desk ownership before enabling again");
  }
  if (saved?.version === 3 && (!saved.pythonStubPath || saved.customStubPathEnvironmentRoots === undefined)) {
    const current = pythonSettings.effective();
    saved = { ...saved, customStubPathEnvironmentRoots,
      pythonStubPath: saved.pythonStubPath ?? { snapshot: pythonSettings.snapshot(), current: current || "" } };
    await persistOwner(saved);
  }
  let pythonOwner = saved?.pythonStubPath ?? { snapshot: pythonSettings.snapshot(), current: pythonSettings.effective() || "" };
  const configurationCurrent = () => {
    if (pythonSettings.dirty()) throw new Error("Dirty workspace settings; preserved");
    validatePythonStubPath(pythonSettings, pythonOwner.current || "");
    return true;
  };
  const setPythonStubPath = async (value, expected = pythonOwner.current) => {
    if (pythonSettings.dirty()) throw new Error("Dirty workspace settings; preserved");
    validatePythonStubPath(pythonSettings, expected);
    const previous = pythonOwner;
    pythonOwner = { ...pythonOwner, current: value };
    try {
      await pythonSettings.update(value);
      validatePythonStubPath(pythonSettings, value);
    } catch (error) {
      pythonOwner = previous;
      throw error;
    }
  };
  const restorePythonStubPath = async () => {
    const original = pythonOwner.snapshot;
    const effectiveOriginal = original.folderPresent ? original.folderValue : original.workspacePresent ? original.workspaceValue : original.globalPresent ? original.globalValue : "";
    const current = pythonSettings.effective() || "";
    if (current !== (pythonOwner.current || "") && current !== (effectiveOriginal || "")) {
      throw new Error("Pylance python.analysis.stubPath changed during GraphQL Desk cleanup; preserved");
    }
    if (current !== (effectiveOriginal || "")) {
      if (pythonSettings.dirty()) throw new Error("Dirty workspace settings; preserved");
      await pythonSettings.restore(original);
    }
    validatePythonStubPath(pythonSettings, effectiveOriginal || "");
  };
  const legacyOwnership = saved?.version === 2 ? saved.ownership : saved?.version === 3 ? saved.legacyOwnership : null;
  const restoreOwnership = { customStubPath, customStubPathAdded, removeExtraPathsKey,
    originalStubPathPresent, originalStubPath,
    ...(customStubPathEnvironmentRoots.length ? { executionEnvironmentRoots: customStubPathEnvironmentRoots } : {}) };
  const managedAliases = config => configPaths(config).some(value => resolvesToManagedRoot(value));
  const managedSearchAliases = config => [...config.extraPaths, ...config.executionEnvironments.flatMap(item => item.extraPaths)]
    .some(value => value !== MANAGED_PATH && (resolvesToManagedRoot(value) || typeof value === "string" &&
      normalized(path.resolve(configDirectory, value)).startsWith(`${managedRoot}${path.sep}`) &&
      isManagedGeneration(workspace, path.relative(workspace, path.resolve(configDirectory, value)).split(path.sep).join("/"), MANAGED_PATH)));
  const inspectLatest = async () => {
    const latest = await updateProjectConfig(vscode, folder, "inspect", worker);
    if (!latest || path.resolve(latest.file) !== path.resolve(inspected.file)) {
      throw new Error("Effective Pyright config changed while GraphQL Desk owned stubPath; preserved");
    }
    return latest;
  };
  const customPathPresent = config => config.extraPaths.includes(customStubPath);
  const customEnvironmentPathsPresent = (config, requireCoverage = true) => (!requireCoverage || config.executionEnvironments.every(environment =>
    !environment.hasExtraPaths || hasCustomPath(environment.extraPaths))) && customStubPathEnvironmentRoots.every(root => {
    const environment = config.executionEnvironments.find(item => item.root === root);
    return environment?.hasExtraPaths && environment.extraPaths.filter(value => value === customStubPath).length === 1;
  });
  const customEnvironmentPathsRemain = config => customStubPathEnvironmentRoots.some(root =>
    config.executionEnvironments.find(item => item.root === root)?.extraPaths.includes(customStubPath));
  const isManagedSelection = value => value === MANAGED_PATH || typeof value === "string" &&
    normalized(path.resolve(configDirectory, value)).startsWith(`${managedRoot}${path.sep}`);
  const genericOwner = workspaceState?.get?.(generationKeys.stateKey);

  if (saved?.version === 3 && saved.phase === "release") {
    const latest = await inspectLatest();
    const restored = latest.hasStubPath === saved.originalStubPathPresent &&
      (!saved.originalStubPathPresent || latest.stubPath === saved.originalStubPath);
    if (restored && (saved.customStubPathAdded && customPathPresent(latest) || customEnvironmentPathsRemain(latest))) {
      throw new Error("Pyright config changed during GraphQL Desk cleanup; preserved");
    }
    if (!restored && (!latest.hasStubPath || latest.stubPath !== saved.currentStubPath)) {
      throw new Error("Pyright stubPath changed during GraphQL Desk cleanup; preserved");
    }
    if (!restored) {
      const released = await updateProjectConfig(vscode, folder, "release_stub_path", worker, {
        ...restoreOwnership, expectedStubPathPresent: latest.hasStubPath, expectedStubPath: latest.stubPath,
      }, MANAGED_PATH, onOwnedWrite, configFile, configurationCurrent);
      if (released.hasStubPath !== saved.originalStubPathPresent ||
          saved.originalStubPathPresent && released.stubPath !== saved.originalStubPath ||
          saved.customStubPathAdded && customPathPresent(released) || customEnvironmentPathsRemain(released)) {
        throw new Error("GraphQL Desk Pyright config cleanup could not be verified; preserved");
      }
    }
    await restorePythonStubPath();
    await clearOwner();
    await workspaceState?.update?.(generationKeys.stateKey, undefined);
    await updateManagedState(workspaceState, generationKeys.generationStateKey, undefined);
    await updateManagedState(workspaceState, generationKeys.stagingStateKey, undefined);
    return createManagedProjectConfig(vscode, folder, workspaceState, worker, { onOwnedWrite: originalOnOwnedWrite, onOwnership, configFile });
  }

  if (saved?.version === 3 && saved.phase === "rotate") {
    const pending = workspaceState?.get?.(generationKeys.stateKey);
    if (!pending?.pending || !isManagedGeneration(workspace, pending.current, MANAGED_PATH)) {
      throw new Error("Interrupted Pylance generation rotation is incomplete; preserved");
    }
    const latest = await inspectLatest();
    if (![pending.previous, pending.current].includes(latest.stubPath)) {
      throw new Error("Pyright stubPath changed during interrupted rotation; preserved");
    }
    const pythonCurrent = pythonSettings.effective() || "";
    if (![saved.pythonStubPath.current, saved.pythonStubPath.pending, pending.current].includes(pythonCurrent)) {
      throw new Error("Pylance stubPath changed during interrupted rotation; preserved");
    }
    const target = pending.current;
    if (latest.stubPath !== target) await updateProjectConfig(vscode, folder, "set_stub_path", worker,
      { expectedStubPathPresent: latest.hasStubPath, expectedStubPath: latest.stubPath }, target, onOwnedWrite, configFile, configurationCurrent);
    if (pythonCurrent !== target) await setPythonStubPath(target, pythonCurrent);
    saved = { ...saved, phase: "active", currentStubPath: target,
      pythonStubPath: { ...saved.pythonStubPath, current: target, pending: undefined } };
    pythonOwner = saved.pythonStubPath;
    await persistOwner(saved);
  }

  if (saved?.version === 3 && saved.phase === "active") {
    let latest = await inspectLatest();
    if (managedSearchAliases(latest)) {
      latest = await updateProjectConfig(vscode, folder, "remove_stale_generations", worker, {}, MANAGED_PATH, onOwnedWrite, configFile, configurationCurrent);
    }
    if (latest.stubPath !== saved.currentStubPath || !latest.hasStubPath ||
        saved.customStubPathAdded && !customPathPresent(latest) || !customEnvironmentPathsPresent(latest) ||
        managedAliases(latest) && !isManagedSelection(latest.stubPath) || managedSearchAliases(latest)) {
      const pending = workspaceState?.get?.(generationKeys.stateKey);
      if (pending?.pending === true && pending.current === latest.stubPath && pending.previous === saved.currentStubPath && isManagedSelection(latest.stubPath)) {
        saved = { ...saved, currentStubPath: latest.stubPath };
        await persistOwner(saved);
      } else if (pending?.release === true && latest.stubPath === (saved.originalStubPath ?? "")) {
        saved = { ...saved, phase: "release", currentStubPath: latest.stubPath };
        await persistOwner(saved);
        return createManagedProjectConfig(vscode, folder, workspaceState, worker, { onOwnedWrite: originalOnOwnedWrite, onOwnership, configFile });
      } else {
        throw new Error("GraphQL Desk Pyright stubPath ownership changed; preserved");
      }
    }
  }

  if (!saved || saved.version === 2 || saved.phase === "prepare") {
    if (saved?.version === 3) {
      const latest = await inspectLatest();
      if (latest.stubPath === MANAGED_PATH && (!saved.customStubPathAdded || customPathPresent(latest)) && customEnvironmentPathsPresent(latest)) {
        saved = { ...saved, phase: "active", currentStubPath: MANAGED_PATH };
        await persistOwner(saved);
      } else if ((latest.hasStubPath === saved.originalStubPathPresent) &&
          (!latest.hasStubPath || latest.stubPath === saved.originalStubPath)) {
        inspected.stubPath = latest.stubPath;
        inspected.hasStubPath = latest.hasStubPath;
        inspected.extraPaths = latest.extraPaths;
      } else throw new Error("Incomplete GraphQL Desk Pyright config setup; preserved");
    }
    if (!saved || saved.version === 2 || saved.phase === "prepare") {
      const current = saved?.version === 3 ? await inspectLatest() : inspected;
      if (!saved && managedAliases(current)) throw new Error("Pyright config aliases the reserved GraphQL Desk path; preserved");
      const legacy = saved?.version === 2 ? saved.ownership : saved?.version === 3 ? saved.legacyOwnership : null;
      const owner = saved?.version === 3 ? saved : {
        version: 3, file: fileKey, format: inspected.format, path: MANAGED_PATH,
        originalStubPathPresent, originalStubPath: originalStubPath ?? null,
        customStubPath, customStubPathAdded, customStubPathEnvironmentRoots, removeExtraPathsKey,
        currentStubPath: current.hasStubPath ? current.stubPath : "", phase: "prepare",
        pythonStubPath: { snapshot: pythonSettings.snapshot(), current: pythonSettings.effective() || "" },
        legacyOwnership: legacy,
      };
      await persistOwner({ ...owner, phase: "prepare" });
      const prepared = current.stubPath === MANAGED_PATH && current.hasStubPath
        ? current
        : await updateProjectConfig(vscode, folder, "prepare_stub_path", worker, {
          expectedStubPathPresent: current.hasStubPath, expectedStubPath: current.stubPath,
          customStubPath, customStubPathAdded, removeExtraPathsKey,
          ...(customStubPathEnvironmentRoots.length ? { executionEnvironmentRoots: customStubPathEnvironmentRoots } : {}),
          removeManagedPaths: saved?.version === 2 || Boolean(saved?.version === 3 && legacy),
          legacyTopLevel: legacy?.topLevel === true,
          legacyExecutionEnvironmentRoots: Array.isArray(legacy?.executionEnvironmentRoots) ? legacy.executionEnvironmentRoots : [],
        }, MANAGED_PATH, onOwnedWrite, configFile, configurationCurrent);
      if (prepared.stubPath !== MANAGED_PATH || !prepared.hasStubPath ||
          customStubPathAdded && !prepared.extraPaths.includes(customStubPath) || !customEnvironmentPathsPresent(prepared)) {
        throw new Error("GraphQL Desk managed Pyright stubPath could not be verified; preserved");
      }
      const pythonCurrent = pythonSettings.effective() || "";
      if (pythonCurrent !== MANAGED_PATH) await setPythonStubPath(MANAGED_PATH, pythonCurrent);
      saved = { ...owner, currentStubPath: MANAGED_PATH, phase: "active",
        pythonStubPath: { ...pythonOwner, current: MANAGED_PATH } };
      await persistOwner(saved);
    }
  }

  if (!saved || saved.version !== 3 || saved.phase !== "active") throw new Error("GraphQL Desk Pyright stubPath setup is incomplete; preserved");
  const activeConfig = await inspectLatest();
  activeConfigHash = activeConfig.hash;
  const preflight = ensureWorkspaceRoot(workspace, Boolean(saved?.version === 2 || saved?.version === 3));
  let currentSetting = saved.currentStubPath;
  let currentPresent = true;
  // Publish ownership before awaiting the generation root. The controller may
  // check an in-flight snapshot while the root is being initialized.
  onOwnership(inspected.format === "toml" ? inspected.file : null, currentSetting,
    saved.originalStubPath, saved.originalStubPathPresent);
  const assertEffectiveOwner = async (requireCustomCoverage = true) => {
    const latest = await inspectLatest();
    if (managedSearchAliases(latest)) throw new Error("Pyright search paths alias the reserved GraphQL Desk path; ownership preserved");
    if (!latest.hasStubPath || latest.stubPath !== currentSetting ||
        saved.customStubPathAdded && !customPathPresent(latest) || !customEnvironmentPathsPresent(latest, requireCustomCoverage) ||
        managedAliases(latest) && !isManagedSelection(latest.stubPath)) {
      throw new Error("GraphQL Desk Pyright stubPath ownership changed; preserved");
    }
    return latest;
  };
  const config = {
    get: (_key, fallback) => currentPresent ? currentSetting : fallback,
    inspect: () => currentPresent ? { workspaceValue: currentSetting } : {},
    async update(_key, value, _target, generationCurrent = () => true) {
      const latest = await assertEffectiveOwner();
      const restoring = saved.originalStubPathPresent ? value === saved.originalStubPath : value === undefined;
      const target = typeof value === "string" ? value : MANAGED_PATH;
      const restoringManagedBase = target === MANAGED_PATH;
      const ownership = { expectedStubPathPresent: latest.hasStubPath, expectedStubPath: latest.stubPath,
        ...(restoringManagedBase ? { restoreStubPathPresent: true, restoreStubPath: MANAGED_PATH } : {}) };
      if (restoring) throw new Error("Project Pyright stubPath restoration is handled by GraphQL Desk cleanup; preserved");
      if (pythonSettings.dirty()) throw new Error("Dirty workspace settings; preserved");
      validatePythonStubPath(pythonSettings, pythonOwner.current || "");
      saved = { ...saved, phase: "rotate", pythonStubPath: { ...pythonOwner, pending: target } };
      await persistOwner(saved);
      const previousSetting = currentSetting;
      const updated = await updateProjectConfig(vscode, folder, restoringManagedBase ? "restore_stub_path" : "set_stub_path",
        worker, ownership, target, onOwnedWrite, configFile,
        () => configurationCurrent() && generationCurrent() === true);
      currentSetting = updated.hasStubPath ? updated.stubPath : "";
      currentPresent = updated.hasStubPath;
      activeConfigHash = updated.hash;
      if ((pythonSettings.effective() || "") !== target) await setPythonStubPath(target, pythonOwner.current || "");
      validatePythonStubPath(pythonSettings, target);
      pythonOwner = { ...pythonOwner, current: target, pending: undefined };
      const next = { ...saved, currentStubPath: currentSetting, phase: "active", pythonStubPath: pythonOwner };
      await persistOwner(next);
      saved = next;
      onOwnership(inspected.format === "toml" ? inspected.file : null, currentSetting,
        previousSetting, true);
    },
  };
  const configStillEffective = () => {
    if (inspected.format === "jsonc" ? !fs.existsSync(inspected.file) :
      fs.existsSync(path.join(workspace, "pyrightconfig.json")) || !fs.existsSync(inspected.file)) return false;
    try { return readFileSafe(vscode, workspace, inspected.file).hash === activeConfigHash; }
    catch { return false; }
  };
  const managed = await createManagedStubPath(vscode, folder, workspaceState, {
    config, baseSetting: MANAGED_PATH, forceOwned: true, stateSuffix,
    restoreOnRelease: false,
    isDirty: () => vscode.workspace.textDocuments?.some(document => document.isDirty && document.uri.scheme === "file" &&
      normalized(document.uri.fsPath) === normalized(inspected.file)),
  });
  const packageRootExists = (rootPath, topPackage) => {
    let currentPath = path.parse(rootPath).root;
    const relative = path.relative(currentPath, rootPath);
    for (const part of relative.split(path.sep).filter(Boolean)) {
      currentPath = path.join(currentPath, part);
      let stat;
      try { stat = fs.lstatSync(currentPath); }
      catch (error) { if (error.code === "ENOENT") return false; throw error; }
      if (stat.isSymbolicLink() || !stat.isDirectory()) throw new Error("Configured Pyright stub path contains a symbolic link or file; preserved");
    }
    for (const candidate of [path.join(rootPath, topPackage), `${path.join(rootPath, topPackage)}.py`]) {
      try {
        const stat = fs.lstatSync(candidate);
        if (stat.isSymbolicLink()) throw new Error("Configured Pyright path contains a symbolic link; preserved");
        if (stat.isDirectory() || stat.isFile()) return true;
      } catch (error) { if (error.code !== "ENOENT") throw error; }
    }
    return false;
  };
  return {
    ...managed,
    get ownsSetting() { return managed.ownsSetting; },
    get selectedSetting() { return managed.selectedSetting; },
    checkSettingOwnership() {
      return managed.checkSettingOwnership(() => configStillEffective() &&
        (pythonSettings.effective() || "") === (pythonOwner.current || ""));
    },
    stillOwnsSetting() {
      return configStillEffective() && managed.stillOwnsSetting() &&
        (pythonSettings.effective() || "") === (pythonOwner.current || "");
    },
    ownershipStatus() {
      return { configCurrent: configStillEffective(), managerCurrent: managed.stillOwnsSetting(),
        managerOwned: managed.ownsSetting, managerSelection: managed.selectedSetting, projectSelection: currentSetting,
        pythonExpected: pythonOwner.current || "", pythonActual: pythonSettings.effective() || "",
        pythonCurrent: (pythonSettings.effective() || "") === (pythonOwner.current || ""), phase: saved.phase,
        rotationPending: pythonOwner.pending !== undefined };
    },
    cleanup() { managed.cleanup(); cleanupCreated([...preflight.created, ...ownerCreatedDirectories]); },
    projectConfig: true,
    async validateGeneratedPackages(packages) {
      const owned = await managed.checkSettingOwnership(async () => {
        const latest = await assertEffectiveOwner();
        const roots = [saved.customStubPathAdded ? saved.customStubPath : null, ...latest.extraPaths,
          ...latest.executionEnvironments.flatMap(item => item.extraPaths)].filter(Boolean)
          .map(value => path.resolve(configDirectory, value)).filter(value => !isManagedSelection(path.relative(configDirectory, value).split(path.sep).join("/")));
        const names = new Set(["graphql_desk-stubs", ...packages.map(value => value.split(".")[0])]);
        for (const rootPath of new Set(roots)) for (const name of names) {
          if (packageRootExists(rootPath, name)) throw new Error(`Configured Pyright path shadows GraphQL Desk's generated package ${name}; preserved`);
        }
        return true;
      });
      if (!owned) throw new Error("GraphQL Desk Pyright stubPath ownership changed; preserved");
      return true;
    },
    async release() {
      if (!managed.ownsSetting) return;
      await assertEffectiveOwner(false);
      saved = { ...saved, phase: "release" };
      await persistOwner(saved);
      await managed.release();
      const latest = await inspectLatest();
      if (!latest.hasStubPath || latest.stubPath !== currentSetting) throw new Error("Pyright stubPath changed during GraphQL Desk cleanup; preserved");
      const released = await updateProjectConfig(vscode, folder, "release_stub_path", worker, {
        expectedStubPathPresent: latest.hasStubPath, expectedStubPath: latest.stubPath, ...restoreOwnership,
      }, MANAGED_PATH, onOwnedWrite, configFile, configurationCurrent);
      if (released.hasStubPath !== originalStubPathPresent || originalStubPathPresent && released.stubPath !== originalStubPath ||
          customStubPathAdded && released.extraPaths.includes(customStubPath) || customEnvironmentPathsRemain(released)) {
        throw new Error("GraphQL Desk Pyright config cleanup could not be verified; preserved");
      }
      await restorePythonStubPath();
      await clearOwner();
      saved = undefined;
      onOwnership(null, null);
    },
  };
}

module.exports = { MANAGED_PATH, parseJsonConfig, modifyJsonConfig, updateProjectConfig, createManagedProjectConfig, acknowledgePendingConfigWrite, safeIdentity, normalized, stateKey };
