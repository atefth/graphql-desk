"use strict";

const fs = require("node:fs");
const path = require("node:path");
const { randomUUID, createHash } = require("node:crypto");
const { parse, parseTree, modify, applyEdits } = require("jsonc-parser");

const MANAGED_PATH = ".graphql-desk/live-typings";
const MAX_CONFIG_BYTES = 1024 * 1024;
const normalized = value => process.platform === "win32" ? path.resolve(value).toLowerCase() : path.resolve(value);
const same = (left, right) => left.dev === right.dev && left.ino === right.ino;
const stateKey = workspace => `managed-pyright-extra-path:${createHash("sha256").update(normalized(workspace)).digest("hex")}`;

function safeIdentity(file, directory = false) {
  const stat = fs.lstatSync(file);
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
  return { found: true, stubPath: value.stubPath ?? null, extraPaths: [...extraPaths], hasExtraPaths: Object.hasOwn(value, "extraPaths"),
    executionEnvironments, hasExecutionEnvironments: executionEnvironments.length > 0 };
}

function modifyJsonConfig(text, action, managedPath = MANAGED_PATH, ownership = {}) {
  const inspected = parseJsonConfig(text);
  const indexes = inspected.extraPaths.flatMap((value, index) => value === managedPath ? [index] : []);
  if (indexes.length > 1) throw new Error("Duplicate GraphQL Desk Pyright extraPath; preserved");
  if (inspected.executionEnvironments.some(item => item.extraPaths.filter(value => value === managedPath).length > 1)) {
    throw new Error("Duplicate GraphQL Desk execution environment path; preserved");
  }
  if (action === "inspect") return { ...inspected, text };
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
        : apply(updated, ["extraPaths", indexes[0]], undefined);
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
        : apply(updated, ["executionEnvironments", index, "extraPaths", environmentIndexes[0]], undefined);
    }
    return { ...parseJsonConfig(updated), text: updated };
  }
  throw new Error("Invalid project Pyright config action");
}

function readFileSafe(vscode, workspace, file) {
  let current = workspace;
  const parts = path.relative(workspace, file).split(path.sep).filter(Boolean);
  if (!parts.length || path.isAbsolute(path.relative(workspace, file)) || path.relative(workspace, file).startsWith(`..${path.sep}`)) {
    throw new Error("Pyright config must be inside the workspace; preserved");
  }
  safeIdentity(current, true);
  for (const [index, part] of parts.entries()) {
    current = path.join(current, part);
    const stat = safeIdentity(current, index < parts.length - 1);
    if (index === parts.length - 1) {
      if (stat.size > MAX_CONFIG_BYTES) throw new Error("Pyright config is too large; preserved");
      if (vscode.workspace.textDocuments.some(document => document.isDirty && document.uri.scheme === "file" &&
          normalized(document.uri.fsPath) === normalized(current))) throw new Error("Dirty Pyright config buffer; preserved");
      const descriptor = fs.openSync(current, "r");
      try {
        const opened = fs.fstatSync(descriptor);
        if (!same(stat, opened) || !opened.isFile() || opened.size > MAX_CONFIG_BYTES) throw new Error("Pyright config changed while opening; preserved");
        const bytes = Buffer.alloc(opened.size);
        const count = fs.readSync(descriptor, bytes, 0, bytes.length, 0);
        if (count !== bytes.length || !same(opened, fs.lstatSync(current))) throw new Error("Pyright config changed while reading; preserved");
        return { text: bytes.toString("utf8"), identity: opened,
          hash: createHash("sha256").update(bytes).digest("hex"), filename: current };
      } finally { fs.closeSync(descriptor); }
    }
  }
}

function atomicReplace(vscode, workspace, file, text, expected, expectedHash) {
  const targetStat = safeIdentity(file);
  if (!same(targetStat, expected) || targetStat.size > MAX_CONFIG_BYTES ||
      createHash("sha256").update(fs.readFileSync(file)).digest("hex") !== expectedHash) {
    throw new Error("Pyright config changed concurrently; preserved");
  }
  if (vscode.workspace.textDocuments.some(document => document.isDirty && document.uri.scheme === "file" &&
      normalized(document.uri.fsPath) === normalized(file))) throw new Error("Dirty Pyright config buffer; preserved");
  const temporary = path.join(path.dirname(file), `.graphql-desk-${randomUUID()}.tmp`);
  let created;
  try {
    const descriptor = fs.openSync(temporary, "wx");
    try { created = fs.fstatSync(descriptor); fs.writeFileSync(descriptor, Buffer.from(text, "utf8")); fs.fsyncSync(descriptor); }
    finally { fs.closeSync(descriptor); }
    const current = safeIdentity(file);
    if (!same(expected, current) || createHash("sha256").update(fs.readFileSync(file)).digest("hex") !== expectedHash ||
        !same(created, fs.lstatSync(temporary))) throw new Error("Pyright config changed concurrently; preserved");
    const owner = vscode.workspace.getWorkspaceFolder(vscode.Uri.file(file));
    if (!owner || normalized(owner.uri.fsPath) !== normalized(workspace)) throw new Error("Pyright config workspace changed; preserved");
    fs.renameSync(temporary, file);
    const updated = safeIdentity(file);
    if (updated.size > MAX_CONFIG_BYTES || fs.readFileSync(file, "utf8") !== text) throw new Error("Pyright config update could not be verified");
  } catch (error) {
    try {
      const current = fs.lstatSync(temporary);
      if (created && !current.isSymbolicLink() && same(created, current)) fs.unlinkSync(temporary);
    } catch { /* Preserve any temporary path that is no longer verified as ours. */ }
    throw error;
  }
}

async function invokeToml(folder, text, action, worker, ownership = {}) {
  const result = await worker(folder, text, action, MANAGED_PATH, ownership);
  if (!result || result.ok !== true || typeof result.found !== "boolean" || typeof result.text !== "string" ||
      !(result.stub_path === null || typeof result.stub_path === "string") || !Array.isArray(result.extra_paths) ||
      !result.extra_paths.every(value => typeof value === "string") || typeof result.has_execution_environments !== "boolean" ||
      !Array.isArray(result.execution_environments) || !result.execution_environments.every(item => item && typeof item.root === "string" &&
        Array.isArray(item.extra_paths) && item.extra_paths.every(value => typeof value === "string") && typeof item.has_extra_paths === "boolean") ||
      action === "add" && (!result.ownership || typeof result.ownership.top_level !== "boolean" || !Array.isArray(result.ownership.execution_environment_roots))) {
    throw new Error("Malformed pyproject.toml Pyright result; preserved");
  }
  return { found: result.found, stubPath: result.stub_path, extraPaths: result.extra_paths,
    executionEnvironments: result.execution_environments.map(item => ({ root: item.root, extraPaths: item.extra_paths, hasExtraPaths: item.has_extra_paths })),
    hasExecutionEnvironments: result.has_execution_environments, ...(result.ownership ? { ownership: {
      topLevel: result.ownership.top_level, executionEnvironmentRoots: result.ownership.execution_environment_roots,
    } } : {}), text: result.text };
}

async function updateProjectConfig(vscode, folder, action, worker, ownership = {}) {
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
  if (action === "inspect") return { ...inspected, file, format, identity: snapshot.identity, workspace };
    const update = format === "jsonc" ? modifyJsonConfig(snapshot.text, action, MANAGED_PATH, ownership) :
    await invokeToml(folder, snapshot.text, action, worker, ownership);
  if (update.text !== snapshot.text) atomicReplace(vscode, workspace, file, update.text, snapshot.identity, snapshot.hash);
  return { ...update, file, format, workspace };
}

async function createManagedProjectConfig(vscode, folder, workspaceState, worker) {
  const inspected = await updateProjectConfig(vscode, folder, "inspect", worker);
  if (!inspected) return null;
  const workspace = inspected.workspace;
  const key = stateKey(workspace);
  const saved = workspaceState?.get?.(key);
  const fileKey = path.relative(workspace, inspected.file).split(path.sep).join("/");
  const configDirectory = path.dirname(inspected.file);
  const managedRoot = normalized(path.resolve(configDirectory, MANAGED_PATH));
  const configuredPaths = config => [config.stubPath ?? "typings", ...config.extraPaths,
    ...config.executionEnvironments.flatMap(item => item.extraPaths)];
  const hasManagedAlias = config => configuredPaths(config).some(value => value !== MANAGED_PATH &&
    normalized(path.resolve(configDirectory, value)) === managedRoot);
  const hasGlob = value => value.includes("*") || value.includes("?") || /[\[\]{}]/.test(value);
  if (configuredPaths(inspected).some(value => value !== MANAGED_PATH && hasGlob(value))) {
    throw new Error("Pyright stubPath/extraPaths globs cannot be checked for generated-package collisions; preserved");
  }
  if (hasManagedAlias(inspected)) {
    throw new Error("Pyright config aliases the reserved GraphQL Desk path; preserved");
  }
  const validOwnership = value => value && typeof value.topLevel === "boolean" &&
    Array.isArray(value.executionEnvironmentRoots) && value.executionEnvironmentRoots.every(root => typeof root === "string");
  if (saved && (saved.version !== 2 || saved.file !== fileKey || saved.path !== MANAGED_PATH || saved.format !== inspected.format ||
      !validOwnership(saved.ownership) || Boolean(saved.pending) === Boolean(saved.active))) {
    throw new Error("GraphQL Desk Pyright config ownership changed; preserved");
  }
  const hasManagedPath = config => config.extraPaths.includes(MANAGED_PATH) &&
    config.executionEnvironments.every(item => item.extraPaths.includes(MANAGED_PATH));
  const hasAnyManagedPath = config => config.extraPaths.includes(MANAGED_PATH) ||
    config.executionEnvironments.some(item => item.extraPaths.includes(MANAGED_PATH));
  if (saved?.active && !hasManagedPath(inspected)) {
    throw new Error("Active GraphQL Desk Pyright path is missing from project config; preserved");
  }
  if (saved?.pending && hasAnyManagedPath(inspected) && !hasManagedPath(inspected)) {
    throw new Error("Incomplete pending GraphQL Desk Pyright config update; preserved");
  }
  if (hasAnyManagedPath(inspected) && !saved?.active && !saved?.pending) {
    throw new Error("Pyright extraPaths already contains the reserved GraphQL Desk path; preserved");
  }
  const directory = ensureWorkspaceRoot(workspace, Boolean(saved?.active || saved?.pending));
  let active = Boolean(saved?.active);
  let configChanged = false;
  let current = inspected;
  try {
    if (!active) {
      const ownership = saved?.pending ? saved.ownership : {
        topLevel: !inspected.hasExtraPaths,
        executionEnvironmentRoots: inspected.executionEnvironments.filter(item => !item.hasExtraPaths).map(item => item.root),
      };
      await workspaceState?.update?.(key, { version: 2, file: fileKey, format: inspected.format, path: MANAGED_PATH,
        ownership, pending: true });
      if (saved?.pending && hasManagedPath(inspected)) current = { ...inspected, ownership };
      else current = await updateProjectConfig(vscode, folder, "add", worker, ownership);
      if (!hasManagedPath(current)) throw new Error("GraphQL Desk Pyright extraPath was not persisted everywhere");
      active = true;
      configChanged = current.text !== inspected.text;
      await workspaceState?.update?.(key, { version: 2, file: fileKey, format: inspected.format, path: MANAGED_PATH,
        ownership: current.ownership ?? ownership, active: true });
    }
  } catch (error) {
    if (active) {
      try { await updateProjectConfig(vscode, folder, "remove", worker, current.ownership); } catch { /* Preserve config if ownership cannot be revalidated. */ }
    }
    try { await workspaceState?.update?.(key, undefined); } catch { /* The pending owner record is safer than guessing. */ }
    cleanupCreated(directory.created);
    throw error;
  }
  const configHash = createHash("sha256").update(current.text, "utf8").digest("hex");
  const root = directory.root;
  const configRoots = () => {
    const base = path.dirname(inspected.file);
    const stubPath = current.stubPath ?? "typings";
    const values = [stubPath, ...current.extraPaths, ...current.executionEnvironments.flatMap(item => item.extraPaths)]
      .filter(value => value !== MANAGED_PATH);
    if (values.some(hasGlob)) throw new Error("Pyright stubPath/extraPaths globs cannot be checked for generated-package collisions; preserved");
    return values.map(value => path.resolve(base, value)).filter((value, index, values) => values.indexOf(value) === index);
  };
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
    const packagePath = path.join(rootPath, topPackage);
    const modulePath = `${packagePath}.py`;
    for (const candidate of [packagePath, modulePath]) {
      try {
        const stat = fs.lstatSync(candidate);
        if (stat.isSymbolicLink()) throw new Error("Configured Pyright path contains a symbolic link; preserved");
        if (stat.isDirectory() || stat.isFile()) return true;
      } catch (error) { if (error.code !== "ENOENT") throw error; }
    }
    return false;
  };
  return {
    root,
    ownsSetting: false,
    projectConfig: true,
    needsInitialRestart: configChanged,
    get selectedSetting() { return MANAGED_PATH; },
    stillOwnsSetting() {
      try {
        const stat = safeIdentity(inspected.file);
        if (stat.size > MAX_CONFIG_BYTES) return false;
        return createHash("sha256").update(fs.readFileSync(inspected.file)).digest("hex") === configHash;
      } catch { return false; }
    },
    validateGeneratedPackages(packages) {
      if (!this.stillOwnsSetting()) throw new Error("Pyright config changed after GraphQL Desk setup; preserved");
      const roots = configRoots();
      const names = new Set(["graphql_desk-stubs", ...packages.map(value => value.split(".")[0])]);
      for (const rootPath of roots) for (const name of names) {
        if (packageRootExists(rootPath, name)) throw new Error(`Configured Pyright path shadows GraphQL Desk's generated package ${name}; preserved`);
      }
      return true;
    },
    cleanup() { cleanupCreated(directory.created); },
    async release() {
      if (!active) return;
      const latest = await updateProjectConfig(vscode, folder, "inspect", worker);
      if (!latest || path.resolve(latest.file) !== path.resolve(inspected.file)) throw new Error("Active Pyright config changed; preserved");
      if (hasManagedAlias(latest)) throw new Error("Pyright config aliases the reserved GraphQL Desk path; ownership preserved");
      if (hasAnyManagedPath(latest)) {
        const released = await updateProjectConfig(vscode, folder, "remove", worker, saved?.ownership ?? {});
        if (hasAnyManagedPath(released)) throw new Error("GraphQL Desk Pyright paths remain after release; ownership preserved");
      }
      await workspaceState?.update?.(key, undefined);
      active = false;
    },
  };
}

module.exports = { MANAGED_PATH, parseJsonConfig, modifyJsonConfig, updateProjectConfig, createManagedProjectConfig, safeIdentity, normalized, stateKey };
