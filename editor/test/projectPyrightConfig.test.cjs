"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { createHash, randomUUID } = require("node:crypto");
const test = require("node:test");
const { createAnalysisWorker } = require("../src/analysisWorker");
const { parseJsonConfig, modifyJsonConfig, updateProjectConfig, createManagedProjectConfig, MANAGED_PATH, stateKey } = require("../src/projectPyrightConfig");
const { managedStateKeys } = require("../src/managedStubPath");

function stateStore() {
  const values = new Map();
  return { get: key => values.get(key), update: async (key, value) => value === undefined ? values.delete(key) : values.set(key, value) };
}

function fixture(t, { json, toml, dirty = false, pythonStubPath: initialPythonStubPath, inheritedPythonStubPath } = {}) {
  const localTemp = path.join(__dirname, "..", ".cache", "issue-329-unit");
  fs.mkdirSync(localTemp, { recursive: true });
  const root = fs.mkdtempSync(path.join(localTemp, "pyright-config-"));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const folder = { uri: { scheme: "file", fsPath: root, toString: () => `file://${root.replaceAll("\\", "/")}` } };
  if (json !== undefined) fs.writeFileSync(path.join(root, "pyrightconfig.json"), json);
  if (toml !== undefined) fs.writeFileSync(path.join(root, "pyproject.toml"), toml);
  let pythonStubPath = initialPythonStubPath;
  const configurationListeners = new Set();
  let onPythonSettingChange;
  const pythonConfiguration = {
    get: (_name, fallback) => pythonStubPath ?? inheritedPythonStubPath ?? fallback,
    inspect: () => ({ workspaceFolderValue: pythonStubPath, workspaceValue: inheritedPythonStubPath }),
    update: async (_name, value) => {
      pythonStubPath = value;
      if (onPythonSettingChange) await onPythonSettingChange(value);
      for (const listener of configurationListeners) listener({ affectsConfiguration: name => name === "python.analysis.stubPath" });
    },
  };
  const vscode = {
    workspace: {
      isTrusted: true,
      workspaceFolders: [folder],
      getConfiguration: () => pythonConfiguration,
      onDidChangeConfiguration(listener) { configurationListeners.add(listener); return { dispose: () => configurationListeners.delete(listener) }; },
      textDocuments: dirty ? [{ isDirty: true, uri: { scheme: "file", fsPath: path.join(root, "pyrightconfig.json") } }] : [],
      getWorkspaceFolder(uri) { return path.resolve(uri.fsPath).startsWith(path.resolve(root)) ? folder : undefined; },
    },
    Uri: { file: fsPath => ({ scheme: "file", fsPath }) },
    ConfigurationTarget: { WorkspaceFolder: 2 },
  };
  const worker = async (_folder, text, action, managedPath, ownership = {}) => {
    const start = text.indexOf("[tool.pyright]");
    if (start < 0) return { ok: true, found: false, text, stub_path: null, has_stub_path: false, extra_paths: [], has_execution_environments: false,
      execution_environments: [], ownership: { top_level: true, execution_environment_roots: [] } };
    const section = text.slice(start).split("\n").filter(line => !line.trimStart().startsWith("#"));
    const stubMatch = /^stubPath\s*=\s*"([^"]*)"/m.exec(section.join("\n"));
    const stub = stubMatch?.[1] ?? null;
    const hasStub = /^stubPath\s*=/m.test(section.join("\n"));
    const pathLine = /^extraPaths\s*=\s*\[([^\]]*)\]/m.exec(section.join("\n"));
    const extra = [...(pathLine?.[1].matchAll(/"([^"]*)"/g) ?? [])].map(match => match[1]);
    const hasEnvs = /^executionEnvironments\s*=\s*\[/m.test(section.join("\n"));
    let output = text;
    const writeExtra = values => {
      const rendered = `extraPaths = [${values.map(item => JSON.stringify(item)).join(", ")}]`;
      if (pathLine) output = output.replace(pathLine[0], rendered);
      else output = `${output.trimEnd()}\n${rendered}\n`;
    };
    const writeStub = value => {
      const rendered = value === undefined ? null : `stubPath = ${JSON.stringify(value)}`;
      if (rendered === null) output = output.replace(/^stubPath\s*=.*\r?\n?/m, "");
      else if (hasStub) output = output.replace(/^stubPath\s*=.*$/m, rendered);
      else output = `${output.trimEnd()}\n${rendered}\n`;
    };
    if (["set_stub_path", "restore_stub_path", "prepare_stub_path", "release_stub_path"].includes(action) &&
        (ownership.expectedStubPathPresent !== hasStub || hasStub && ownership.expectedStubPath !== stub)) {
      throw new Error("Pyright stubPath changed concurrently");
    }
    if (action === "add" && !extra.includes(managedPath)) {
      const line = pathLine?.[0];
      output = line
        ? text.replace(line, line.replace(/\]\s*$/, `${extra.length ? ", " : ""}\"${managedPath}\"]`))
        : `${text.trimEnd()}\nextraPaths = [\"${managedPath}\"]\n`;
    }
    if (action === "set_stub_path") writeStub(managedPath);
    if (action === "restore_stub_path") writeStub(ownership.restoreStubPathPresent ? ownership.restoreStubPath : undefined);
    if (action === "prepare_stub_path") {
      let next = ownership.removeManagedPaths ? extra.filter(item => item !== managedPath) : [...extra];
      if (ownership.customStubPathAdded) next.push(ownership.customStubPath);
      if (next.join("\0") !== extra.join("\0")) writeExtra(next);
      writeStub(managedPath);
    }
    if (action === "release_stub_path") {
      let next = [...extra];
      if (ownership.customStubPathAdded) {
        const index = next.indexOf(ownership.customStubPath);
        if (index < 0) throw new Error("custom stub path ownership changed");
        next.splice(index, 1);
        if (ownership.removeExtraPathsKey && next.length === 0 && pathLine) output = output.replace(/^extraPaths\s*=.*\r?\n?/m, "");
        else writeExtra(next);
      }
      writeStub(ownership.originalStubPathPresent ? ownership.originalStubPath : undefined);
    }
    const nextExtra = action === "add" ? [...extra, ...(extra.includes(managedPath) ? [] : [managedPath])] :
      action === "remove" ? extra.filter(item => item !== managedPath) :
      action === "prepare_stub_path" ? [...(ownership.removeManagedPaths ? extra.filter(item => item !== managedPath) : extra), ...(ownership.customStubPathAdded ? [ownership.customStubPath] : [])] :
      action === "release_stub_path" && ownership.customStubPathAdded ? extra.filter(item => item !== ownership.customStubPath) : extra;
    if (action === "remove" && extra.includes(managedPath)) {
      if (ownership.topLevel === true && nextExtra.length === 0) {
        output = text.replace(/^[ \t]*extraPaths\s*=\s*\[[^\]]*\][ \t]*(?:\r?\n|$)/m, "");
      } else {
        const line = pathLine[0].replace(new RegExp(`,?\\s*\\"${managedPath.replace(/[.*+?^${}()|[\\]\\\\]/g, "\\\\$&")}\\"`), "");
        output = text.replace(pathLine[0], line);
      }
    }
    const nextStub = ["set_stub_path", "prepare_stub_path"].includes(action) ? managedPath :
      action === "restore_stub_path" ? ownership.restoreStubPathPresent ? ownership.restoreStubPath : null :
      action === "release_stub_path" ? ownership.originalStubPathPresent ? ownership.originalStubPath : null : stub;
    const nextHasStub = ["set_stub_path", "prepare_stub_path"].includes(action) ? true :
      action === "restore_stub_path" ? ownership.restoreStubPathPresent :
      action === "release_stub_path" ? ownership.originalStubPathPresent : hasStub;
    return { ok: true, found: true, text: output, stub_path: nextStub, has_stub_path: nextHasStub, extra_paths: nextExtra, has_execution_environments: hasEnvs,
      execution_environments: [], ownership: { top_level: !pathLine, execution_environment_roots: [] } };
  };
  return { root, folder, vscode, worker, get pythonStubPath() { return pythonStubPath; }, onPythonSettingChange(callback) { onPythonSettingChange = callback; } };
}

test("JSONC edit preserves comments, trailing commas, and unrelated settings", () => {
  const original = `// project-specific\n{\n  "include": ["src",], // keep\n  "extraPaths": ["vendor",],\n  "stubPath": "stubs",\n}\n`;
  const added = modifyJsonConfig(original, "add");
  assert.deepEqual(added.extraPaths, ["vendor", MANAGED_PATH]);
  assert.match(added.text, /\/\/ project-specific/);
  assert.match(added.text, /\/\/ keep/);
  assert.match(added.text, /"include": \["src",\]/);
  assert.match(added.text, /"stubPath": "stubs"/);
  const removed = modifyJsonConfig(added.text, "remove");
  assert.deepEqual(removed.extraPaths, ["vendor"]);
  assert.match(removed.text, /\/\/ project-specific/);
  assert.match(removed.text, /\/\/ keep/);
});

test("JSONC edit adds and removes the managed path when extraPaths is absent", () => {
  const added = modifyJsonConfig('{\n  "typeCheckingMode": "strict"\n}\n', "add");
  assert.deepEqual(added.extraPaths, [MANAGED_PATH]);
  const removed = modifyJsonConfig(added.text, "remove");
  assert.deepEqual(removed.extraPaths, []);
  assert.match(removed.text, /"typeCheckingMode": "strict"/);
});

test("duplicate JSONC keys are rejected before any config update", async t => {
  const text = '{"extraPaths":["vendor"],"extraPaths":["shadow"]}';
  assert.throws(() => modifyJsonConfig(text, "add"), /Duplicate JSONC config key/);
  const f = fixture(t, { json: text });
  await assert.rejects(updateProjectConfig(f.vscode, f.folder, "add", f.worker), /Duplicate JSONC config key/);
  assert.equal(fs.readFileSync(path.join(f.root, "pyrightconfig.json"), "utf8"), text);
});

test("JSONC appends to every execution environment and removes only settings it created", () => {
  const original = `{
  "extraPaths": ["vendor"],
  "executionEnvironments": [
    { "root": ".", "extraPaths": ["env-vendor"] },
    { "root": "src", /* retain */ "pythonVersion": "3.12"
    }
  ]
}\n`;
  const added = modifyJsonConfig(original, "add");
  assert.deepEqual(added.extraPaths, ["vendor", MANAGED_PATH]);
  assert.deepEqual(added.executionEnvironments.map(item => item.extraPaths), [
    ["env-vendor", MANAGED_PATH], [MANAGED_PATH],
  ]);
  assert.deepEqual(added.ownership, { topLevel: false, executionEnvironmentRoots: ["src"] });
  const removed = modifyJsonConfig(added.text, "remove", MANAGED_PATH, added.ownership);
  assert.deepEqual(removed.extraPaths, ["vendor"]);
  assert.deepEqual(removed.executionEnvironments.map(item => item.extraPaths), [["env-vendor"], []]);
  assert.equal(removed.executionEnvironments[1].hasExtraPaths, false);
  assert.match(removed.text, /\/\* retain \*\//);
});

test("project pyrightconfig.json takes precedence over pyproject.toml", async t => {
  const f = fixture(t, { json: '{"stubPath":"json-stubs"}', toml: '[tool.pyright]\nstubPath = "toml-stubs"\n' });
  const result = await updateProjectConfig(f.vscode, f.folder, "inspect", f.worker);
  assert.equal(path.basename(result.file), "pyrightconfig.json");
  assert.equal(result.stubPath, "json-stubs");
});

test("pyproject configuration is selected when pyrightconfig.json is absent", async t => {
  const text = '[tool.pyright]\nstubPath = "stubs"\nextraPaths = ["vendor"]\n';
  const f = fixture(t, { toml: text });
  const result = await updateProjectConfig(f.vscode, f.folder, "add", f.worker);
  assert.equal(path.basename(result.file), "pyproject.toml");
  assert.equal(result.stubPath, "stubs");
  assert.deepEqual(result.extraPaths, ["vendor", MANAGED_PATH]);
  assert.match(fs.readFileSync(result.file, "utf8"), /stubPath = "stubs"/);
});

test("execution environments get managed paths and dirty config buffers fail closed", async t => {
  const config = '{"stubPath":"stubs","extraPaths":["vendor"],"executionEnvironments":[{"root":"src"}]}';
  const f = fixture(t, { json: config });
  const result = await updateProjectConfig(f.vscode, f.folder, "add", f.worker);
  assert.deepEqual(result.executionEnvironments[0].extraPaths, [MANAGED_PATH]);
  assert.deepEqual(result.ownership, { topLevel: false, executionEnvironmentRoots: ["src"] });
  const removed = await updateProjectConfig(f.vscode, f.folder, "remove", f.worker, result.ownership);
  assert.equal(removed.executionEnvironments[0].hasExtraPaths, false);
  assert.deepEqual(parseJsonConfig(fs.readFileSync(removed.file, "utf8")).extraPaths, ["vendor"]);
  const dirty = fixture(t, { json: '{"stubPath":"stubs"}', dirty: true });
  await assert.rejects(updateProjectConfig(dirty.vscode, dirty.folder, "add", dirty.worker), /Dirty Pyright config buffer/);
  assert.equal(fs.readFileSync(path.join(dirty.root, "pyrightconfig.json"), "utf8"), '{"stubPath":"stubs"}');
});

test("an in-place TOML edit during the async worker call is preserved", async t => {
  const original = '[tool.pyright]\nstubPath = "stubs"\n';
  const f = fixture(t, { toml: original });
  const file = path.join(f.root, "pyproject.toml");
  const before = fs.statSync(file);
  const worker = async (...args) => {
    const result = await f.worker(...args);
    if (args[2] === "add") fs.writeFileSync(file, `${original}# concurrent user edit\n`);
    return result;
  };
  await assert.rejects(updateProjectConfig(f.vscode, f.folder, "add", worker), /changed concurrently/);
  const after = fs.statSync(file);
  assert.equal(after.ino, before.ino, "the test mutates the same config inode");
  assert.equal(fs.readFileSync(file, "utf8"), `${original}# concurrent user edit\n`);
});

test("pending ownership recovers before and after the atomic config edit", async t => {
  for (const statePoint of ["before-edit", "after-edit"]) {
    const original = '{"extraPaths":["vendor"],"executionEnvironments":[{"root":"src"}]}';
    const f = fixture(t, { json: original });
    const state = stateStore();
    const file = path.join(f.root, "pyrightconfig.json");
    const intent = modifyJsonConfig(original, "add");
    await state.update(stateKey(f.root), { version: 2, file: "pyrightconfig.json", format: "jsonc", path: MANAGED_PATH,
      ownership: intent.ownership, pending: true });
    if (statePoint === "after-edit") fs.writeFileSync(file, intent.text);
    const managed = await createManagedProjectConfig(f.vscode, f.folder, state, f.worker);
    assert.equal(state.get(stateKey(f.root)).phase, "active");
    assert.equal(parseJsonConfig(fs.readFileSync(file, "utf8")).stubPath, MANAGED_PATH);
    await managed.release();
    managed.cleanup();
    const restored = parseJsonConfig(fs.readFileSync(file, "utf8"));
    assert.deepEqual(restored.extraPaths, ["vendor"]);
    assert.equal(restored.executionEnvironments[0].hasExtraPaths, false);
  }
});

test("restart removes only the recorded atomic Pyright config temporary", async t => {
  const f = fixture(t, { json: '{"extraPaths":["vendor"]}' });
  const state = stateStore();
  const config = path.join(f.root, "pyrightconfig.json");
  const temporary = path.join(f.root, `.graphql-desk-${randomUUID()}.tmp`);
  const unrelated = path.join(f.root, `.graphql-desk-${randomUUID()}.tmp`);
  const pendingText = '{"extraPaths":["vendor","typings"]}';
  fs.writeFileSync(temporary, pendingText);
  fs.writeFileSync(unrelated, "leave this unowned file alone");
  await state.update(`${stateKey(f.root)}:pending-config-write`, {
    version: 1, file: "pyrightconfig.json", temporaryPath: temporary,
    hash: createHash("sha256").update(pendingText).digest("hex"),
  });

  const managed = await createManagedProjectConfig(f.vscode, f.folder, state, f.worker);
  assert.equal(fs.existsSync(temporary), false);
  assert.equal(fs.existsSync(unrelated), true);
  assert.equal(state.get(`${stateKey(f.root)}:pending-config-write`), undefined);
  await managed.release();
  managed.cleanup();
});

test("pending TOML recovery carries its saved ownership through the worker bridge", async t => {
  const f = fixture(t, { toml: '[tool.pyright]\nextraPaths = ["vendor"]\n' });
  const state = stateStore();
  const ownership = { topLevel: false, executionEnvironmentRoots: [] };
  await state.update(stateKey(f.root), { version: 2, file: "pyproject.toml", format: "toml", path: MANAGED_PATH,
    ownership, pending: true });
  const observed = [];
  const worker = async (...args) => { observed.push({ action: args[2], ownership: args[4] }); return f.worker(...args); };
  const managed = await createManagedProjectConfig(f.vscode, f.folder, state, worker);
  assert.deepEqual(observed.find(item => item.action === "prepare_stub_path")?.ownership, { expectedStubPathPresent: false, expectedStubPath: null, customStubPath: "typings",
    customStubPathAdded: true, removeExtraPathsKey: false, removeManagedPaths: true, legacyTopLevel: false, legacyExecutionEnvironmentRoots: [] });
  assert.equal(state.get(stateKey(f.root)).phase, "active");
  await managed.release();
  managed.cleanup();
});

test("restart recovers durable config ownership and removes only verified generation aliases", async t => {
  const f = fixture(t, { json: '{"stubPath":"custom-stubs","extraPaths":["vendor"]}', pythonStubPath: "python-custom-stubs" });
  const firstState = stateStore();
  const first = await createManagedProjectConfig(f.vscode, f.folder, firstState, f.worker);
  const id = `g-1-${"a".repeat(8)}-${"b".repeat(4)}-${"c".repeat(4)}-${"d".repeat(4)}-${"e".repeat(12)}`;
  const directory = path.join(first.root, "generations", id);
  fs.mkdirSync(directory, { recursive: true });
  const manifest = { owner: "graphql-desk-analysis-generation", format: 2, id, sequence: 1, files: [], operationManifest: [],
    inputFingerprint: "a".repeat(64), artifactFingerprint: "b".repeat(64) };
  fs.writeFileSync(path.join(directory, "complete.json"), JSON.stringify(manifest));
  const fingerprint = "b".repeat(64);
  const keys = managedStateKeys(f.root, `:project-${require("node:crypto").createHash("sha256").update("pyrightconfig.json").digest("hex").slice(0, 16)}`);
  await firstState.update(keys.generationStateKey, { [id]: fingerprint });
  const generation = await first.selectGeneration(directory, () => true, {
    directory, manifest, manifestFingerprint: fingerprint, verify: () => ({ directory, manifest, manifestFingerprint: fingerprint }),
  });
  assert.equal(generation, true);
  const selected = path.relative(f.root, directory).split(path.sep).join("/");
  const configFile = path.join(f.root, "pyrightconfig.json");
  fs.writeFileSync(configFile, JSON.stringify({ stubPath: selected, extraPaths: ["vendor", "custom-stubs", selected] }));

  const afterRestart = await createManagedProjectConfig(f.vscode, f.folder, stateStore(), f.worker);
  const recovered = parseJsonConfig(fs.readFileSync(configFile, "utf8"));
  assert.equal(recovered.stubPath, selected);
  assert.deepEqual(recovered.extraPaths, ["vendor", "custom-stubs"]);
  await afterRestart.release();
  afterRestart.cleanup();
  const restored = parseJsonConfig(fs.readFileSync(configFile, "utf8"));
  assert.equal(restored.stubPath, "custom-stubs");
  assert.deepEqual(restored.extraPaths, ["vendor"]);
  assert.equal(f.pythonStubPath, "python-custom-stubs");
  assert.equal(fs.existsSync(path.join(f.root, ...MANAGED_PATH.split("/"), "project-config-owner.json")), false);
});

test("real worker accepts rollback from the first generation to the managed base", async t => {
  const f = fixture(t, { toml: '[tool.pyright]\nstubPath = "custom-stubs"\n' });
  const python = process.env.GRAPHQL_DESK_PYTHON ?? path.join(path.resolve(__dirname, "../.."), ".venv",
    process.platform === "win32" ? "Scripts/python.exe" : "bin/python");
  const realWorker = createAnalysisWorker({ python, workspace: f.root, isTrusted: () => true });
  t.after(() => realWorker.dispose());
  const actions = [];
  const worker = (folder, text, action, managedPath, ownership = {}) => {
    actions.push({ action, managedPath });
    return realWorker.request({ op: "pyright_config",
      workspace: folder.uri.fsPath, text, action, managed_path: managedPath, ownership: {
        topLevel: ownership.topLevel ?? false, executionEnvironmentRoots: ownership.executionEnvironmentRoots ?? [],
        ...(ownership.expectedStubPathPresent === undefined ? {} : { expected_stub_path_present: ownership.expectedStubPathPresent }),
        ...(ownership.expectedStubPath === undefined ? {} : { expected_stub_path: ownership.expectedStubPath }),
        ...(ownership.restoreStubPathPresent === undefined ? {} : { restore_stub_path_present: ownership.restoreStubPathPresent }),
        ...(ownership.restoreStubPath === undefined ? {} : { restore_stub_path: ownership.restoreStubPath }),
        ...(ownership.customStubPath === undefined ? {} : { custom_stub_path: ownership.customStubPath }),
        ...(ownership.customStubPathAdded === undefined ? {} : { custom_stub_path_added: ownership.customStubPathAdded }),
        ...(ownership.removeExtraPathsKey === undefined ? {} : { remove_extra_paths_key: ownership.removeExtraPathsKey }),
        ...(ownership.originalStubPathPresent === undefined ? {} : { original_stub_path_present: ownership.originalStubPathPresent }),
        ...(ownership.originalStubPath === undefined ? {} : { original_stub_path: ownership.originalStubPath }),
        ...(ownership.removeManagedPaths === undefined ? {} : { remove_managed_paths: ownership.removeManagedPaths }),
        ...(ownership.legacyTopLevel === undefined ? {} : { legacy_top_level: ownership.legacyTopLevel }),
        ...(ownership.legacyExecutionEnvironmentRoots === undefined ? {} : { legacy_execution_environment_roots: ownership.legacyExecutionEnvironmentRoots }),
      } });
  };
  const state = stateStore();
  const managed = await createManagedProjectConfig(f.vscode, f.folder, state, worker);
  const id = `g-1-${"a".repeat(8)}-${"b".repeat(4)}-${"c".repeat(4)}-${"d".repeat(4)}-${"e".repeat(12)}`;
  const directory = path.join(managed.root, "generations", id);
  fs.mkdirSync(directory, { recursive: true });
  const manifest = { owner: "graphql-desk-analysis-generation", format: 2, id, sequence: 1, files: [], operationManifest: [],
    inputFingerprint: "a".repeat(64), artifactFingerprint: "b".repeat(64) };
  const fingerprint = "b".repeat(64);
  const keys = managedStateKeys(f.root, `:project-${createHash("sha256").update("pyproject.toml").digest("hex").slice(0, 16)}`);
  await state.update(keys.generationStateKey, { [id]: fingerprint });

  let currentChecks = 0;
  assert.equal(await managed.selectGeneration(directory, () => ++currentChecks === 1, {
    directory, manifest, manifestFingerprint: fingerprint,
    verify: () => ({ directory, manifest, manifestFingerprint: fingerprint }),
  }), false);
  assert.equal(currentChecks, 2);
  assert.ok(actions.some(item => item.action === "set_stub_path" && item.managedPath.endsWith(id)),
    "the generation must be selected before the stale result triggers rollback");
  assert.ok(actions.some(item => item.action === "restore_stub_path" && item.managedPath === MANAGED_PATH),
    "the base path must use the worker's explicit restore action");
  assert.equal((await realWorker.request({ op: "pyright_config", workspace: f.root,
    text: fs.readFileSync(path.join(f.root, "pyproject.toml"), "utf8"), action: "inspect", managed_path: MANAGED_PATH,
    ownership: { topLevel: false, executionEnvironmentRoots: [] } })).stub_path, MANAGED_PATH);
  assert.equal(state.get(stateKey(f.root)).phase, "active");
  await managed.release();
  managed.cleanup();
});

test("active ownership fails closed if the managed path was removed before restart", async t => {
  const original = '{"extraPaths":["vendor"]}';
  const f = fixture(t, { json: original });
  const state = stateStore();
  const managed = await createManagedProjectConfig(f.vscode, f.folder, state, f.worker);
  const file = path.join(f.root, "pyrightconfig.json");
  const current = fs.readFileSync(file, "utf8");
  fs.writeFileSync(file, JSON.stringify({ ...parseJsonConfig(current), stubPath: "custom-user-path" }));
  await assert.rejects(createManagedProjectConfig(f.vscode, f.folder, state, f.worker), /stubPath ownership changed/);
  managed.cleanup();
});

test("release removes remaining owned environment entries after the top-level path is removed", async t => {
  const f = fixture(t, { json: '{"extraPaths":["vendor"],"executionEnvironments":[{"root":"src"}]}' });
  const state = stateStore();
  const managed = await createManagedProjectConfig(f.vscode, f.folder, state, f.worker);
  const file = path.join(f.root, "pyrightconfig.json");
  fs.writeFileSync(file, JSON.stringify({ stubPath: MANAGED_PATH, extraPaths: ["vendor", "typings"], executionEnvironments: [
    { root: "src", extraPaths: ["environment-vendor"] },
  ] }));
  await managed.release();
  managed.cleanup();
  assert.equal(state.get(stateKey(f.root)), undefined);
  const released = parseJsonConfig(fs.readFileSync(file, "utf8"));
  assert.deepEqual(released.extraPaths, ["vendor"]);
  assert.deepEqual(released.executionEnvironments[0].extraPaths, ["environment-vendor"]);
  const reopened = await createManagedProjectConfig(f.vscode, f.folder, state, f.worker);
  assert.equal(state.get(stateKey(f.root)).phase, "active");
  await reopened.release();
  reopened.cleanup();
});

test("fresh JSON config release removes the extraPaths key created by GraphQL Desk", async t => {
  const original = '{"stubPath":"custom-stubs"}';
  const f = fixture(t, { json: original });
  const state = stateStore();
  const managed = await createManagedProjectConfig(f.vscode, f.folder, state, f.worker);
  await managed.release();
  managed.cleanup();
  const restored = parseJsonConfig(fs.readFileSync(path.join(f.root, "pyrightconfig.json"), "utf8"));
  assert.equal(restored.stubPath, "custom-stubs");
  assert.equal(restored.hasExtraPaths, false);
  assert.equal(state.get(stateKey(f.root)), undefined);
});

test("project config release restores an explicit empty folder stubPath over an inherited value", async t => {
  const f = fixture(t, { json: '{"stubPath":"custom-stubs"}', pythonStubPath: "", inheritedPythonStubPath: "inherited-stubs" });
  const managed = await createManagedProjectConfig(f.vscode, f.folder, stateStore(), f.worker);
  await managed.release();
  assert.equal(f.pythonStubPath, "");
  assert.equal(f.vscode.workspace.getConfiguration("python", f.folder.uri).get("analysis.stubPath", "fallback"), "");
  managed.cleanup();
});

test("fresh TOML config release removes the extraPaths key created by GraphQL Desk", async t => {
  const original = '[tool.pyright]\nstubPath = "custom-stubs"\n';
  const f = fixture(t, { toml: original });
  const state = stateStore();
  const managed = await createManagedProjectConfig(f.vscode, f.folder, state, f.worker);
  await managed.release();
  managed.cleanup();
  assert.equal(fs.readFileSync(path.join(f.root, "pyproject.toml"), "utf8"), original);
  assert.equal(state.get(stateKey(f.root)), undefined);
});

test("a JSON config appearing after TOML ownership changes precedence and is left untouched", async t => {
  const f = fixture(t, { toml: '[tool.pyright]\nstubPath = "custom-stubs"\n' });
  const state = stateStore();
  const managed = await createManagedProjectConfig(f.vscode, f.folder, state, f.worker);
  const json = '{"stubPath":"json-user-stubs","extraPaths":["json-vendor"]}';
  fs.writeFileSync(path.join(f.root, "pyrightconfig.json"), json);
  await assert.rejects(managed.release(), /Effective Pyright config changed/);
  assert.equal(fs.readFileSync(path.join(f.root, "pyrightconfig.json"), "utf8"), json);
  assert.match(fs.readFileSync(path.join(f.root, "pyproject.toml"), "utf8"), /stubPath = "\.graphql-desk\/live-typings"/);
  managed.cleanup();
});

test("release recovery removes only the recorded custom stub root after a restart", async t => {
  const original = '{"stubPath":"custom-stubs","extraPaths":["vendor"]}';
  const f = fixture(t, { json: original });
  const state = stateStore();
  const managed = await createManagedProjectConfig(f.vscode, f.folder, state, f.worker);
  const owner = state.get(stateKey(f.root));
  fs.writeFileSync(path.join(f.root, "pyrightconfig.json"), JSON.stringify({
    stubPath: MANAGED_PATH, extraPaths: ["vendor", "custom-stubs"],
  }));
  await state.update(stateKey(f.root), { ...owner, phase: "release" });
  managed.cleanup();

  const reopened = await createManagedProjectConfig(f.vscode, f.folder, state, f.worker);
  const active = parseJsonConfig(fs.readFileSync(path.join(f.root, "pyrightconfig.json"), "utf8"));
  assert.equal(active.stubPath, MANAGED_PATH);
  assert.equal(active.extraPaths.filter(item => item === "custom-stubs").length, 1);
  assert.ok(active.extraPaths.includes("vendor"));
  await reopened.release();
  reopened.cleanup();
  const restored = parseJsonConfig(fs.readFileSync(path.join(f.root, "pyrightconfig.json"), "utf8"));
  assert.equal(restored.stubPath, "custom-stubs");
  assert.deepEqual(restored.extraPaths, ["vendor"]);
});

test("release recovery accepts a project config restored before restart", async t => {
  const original = '{"stubPath":"custom-stubs","extraPaths":["vendor"]}';
  const f = fixture(t, { json: original });
  const state = stateStore();
  const managed = await createManagedProjectConfig(f.vscode, f.folder, state, f.worker);
  const owner = state.get(stateKey(f.root));
  fs.writeFileSync(path.join(f.root, "pyrightconfig.json"), original);
  await state.update(stateKey(f.root), { ...owner, phase: "release" });
  managed.cleanup();

  const reopened = await createManagedProjectConfig(f.vscode, f.folder, state, f.worker);
  const active = parseJsonConfig(fs.readFileSync(path.join(f.root, "pyrightconfig.json"), "utf8"));
  assert.equal(active.stubPath, MANAGED_PATH);
  assert.ok(active.extraPaths.includes("vendor"));
  assert.equal(active.extraPaths.filter(item => item === "custom-stubs").length, 1);
  await reopened.release();
  reopened.cleanup();
  const restored = parseJsonConfig(fs.readFileSync(path.join(f.root, "pyrightconfig.json"), "utf8"));
  assert.equal(restored.stubPath, "custom-stubs");
  assert.deepEqual(restored.extraPaths, ["vendor"]);
});

test("release recovery preserves a re-added custom stub path", async t => {
  const original = '{"stubPath":"custom-stubs","extraPaths":["vendor"]}';
  const f = fixture(t, { json: original });
  const state = stateStore();
  const managed = await createManagedProjectConfig(f.vscode, f.folder, state, f.worker);
  const owner = state.get(stateKey(f.root));
  const changed = '{"stubPath":"custom-stubs","extraPaths":["vendor","custom-stubs"]}';
  fs.writeFileSync(path.join(f.root, "pyrightconfig.json"), changed);
  await state.update(stateKey(f.root), { ...owner, phase: "release" });
  managed.cleanup();

  await assert.rejects(createManagedProjectConfig(f.vscode, f.folder, state, f.worker), /changed during GraphQL Desk cleanup/);
  assert.equal(fs.readFileSync(path.join(f.root, "pyrightconfig.json"), "utf8"), changed);
  assert.equal(state.get(stateKey(f.root)).phase, "release");
});

test("an extraPaths alias of the reserved managed directory is rejected before config edits", async t => {
  const original = '{"extraPaths":["./.graphql-desk/live-typings"]}';
  const f = fixture(t, { json: original });
  await assert.rejects(createManagedProjectConfig(f.vscode, f.folder, stateStore(), f.worker), /aliases the reserved GraphQL Desk path/);
  assert.equal(fs.readFileSync(path.join(f.root, "pyrightconfig.json"), "utf8"), original);
});

test("release preserves ownership when a managed path was rewritten to an alias", async t => {
  const f = fixture(t, { json: '{"extraPaths":["vendor"]}' });
  const state = stateStore();
  const managed = await createManagedProjectConfig(f.vscode, f.folder, state, f.worker);
  const file = path.join(f.root, "pyrightconfig.json");
  fs.writeFileSync(file, '{"stubPath":".graphql-desk/live-typings","extraPaths":["vendor","typings","./.graphql-desk/live-typings"]}');
  await assert.rejects(managed.release(), /search paths alias the reserved GraphQL Desk path/);
  assert.equal(state.get(stateKey(f.root)).phase, "active");
  assert.match(fs.readFileSync(file, "utf8"), /\.\/\.graphql-desk\/live-typings/);
  managed.cleanup();
});

test("globbed Pyright search paths fail closed before modifying the config", async t => {
  const original = '{"extraPaths":["vendor/*"]}';
  const f = fixture(t, { json: original });
  await assert.rejects(createManagedProjectConfig(f.vscode, f.folder, stateStore(), f.worker), /globs cannot be checked/);
  assert.equal(fs.readFileSync(path.join(f.root, "pyrightconfig.json"), "utf8"), original);
});

test("malformed Pyright configs are preserved", async t => {
  const config = '{"stubPath":';
  const f = fixture(t, { json: config });
  assert.throws(() => parseJsonConfig(config), /Malformed/);
  await assert.rejects(updateProjectConfig(f.vscode, f.folder, "add", f.worker), /Malformed/);
  assert.equal(fs.readFileSync(path.join(f.root, "pyrightconfig.json"), "utf8"), config);
});

test("managed config owns one stable path and restores only its exact addition", async t => {
  const original = `// keep\n{\n  "stubPath": "custom-stubs",\n  "extraPaths": ["vendor"],\n  "pythonVersion": "3.10"\n}\n`;
  const f = fixture(t, { json: original });
  const state = stateStore();
  const managed = await createManagedProjectConfig(f.vscode, f.folder, state, f.worker);
  assert.ok(managed);
  assert.equal(managed.root, path.join(f.root, ...MANAGED_PATH.split("/")));
  assert.equal(managed.projectConfig, true);
  const activeConfig = parseJsonConfig(fs.readFileSync(path.join(f.root, "pyrightconfig.json"), "utf8"));
  assert.equal(activeConfig.stubPath, MANAGED_PATH);
  assert.deepEqual(activeConfig.extraPaths, ["vendor", "custom-stubs"]);
  assert.equal(managed.stillOwnsSetting(), true);
  assert.equal(await managed.validateGeneratedPackages(["graphql_desk_generated"]), true);
  await managed.release();
  managed.cleanup();
  const restored = fs.readFileSync(path.join(f.root, "pyrightconfig.json"), "utf8");
  assert.deepEqual(parseJsonConfig(restored).extraPaths, ["vendor"]);
  assert.match(restored, /\/\/ keep/);
  assert.match(restored, /"stubPath": "custom-stubs"/);
  assert.match(restored, /"pythonVersion": "3.10"/);
  assert.equal(state.get(stateKey(f.root)), undefined);
  assert.equal(fs.existsSync(managed.root), false);
});

test("a newly effective JSON config invalidates the managed TOML owner", async t => {
  const f = fixture(t, { toml: '[tool.pyright]\nstubPath = "custom-stubs"\n' });
  const managed = await createManagedProjectConfig(f.vscode, f.folder, stateStore(), f.worker);
  fs.writeFileSync(path.join(f.root, "pyrightconfig.json"), '{"stubPath":"another-stub-root"}');

  assert.equal(managed.stillOwnsSetting(), false);
  managed.cleanup();
});

test("editing an existing TOML config invalidates its managed owner", async t => {
  const file = "[tool.pyright]\nstubPath = \"custom-stubs\"\npythonVersion = \"3.12\"\n";
  const f = fixture(t, { toml: file });
  const managed = await createManagedProjectConfig(f.vscode, f.folder, stateStore(), f.worker);
  fs.writeFileSync(path.join(f.root, "pyproject.toml"), file.replace("custom-stubs", "user-stubs"));

  assert.equal(managed.stillOwnsSetting(), false);
  assert.equal(await managed.checkSettingOwnership(), false);
  managed.cleanup();
});

test("a managed generation update is owned before the workspace setting change event fires", async t => {
  const f = fixture(t, { json: '{"stubPath":"custom-stubs","extraPaths":["vendor"]}', pythonStubPath: "python-custom" });
  const state = stateStore();
  const managed = await createManagedProjectConfig(f.vscode, f.folder, state, f.worker);
  const generationId = `g-1-${"a".repeat(8)}-${"b".repeat(4)}-${"c".repeat(4)}-${"d".repeat(4)}-${"e".repeat(12)}`;
  const generationDirectory = path.join(managed.root, "generations", generationId);
  fs.mkdirSync(generationDirectory, { recursive: true });
  const fingerprint = "a".repeat(64);
  const stateSuffix = `:project-${require("node:crypto").createHash("sha256").update("pyrightconfig.json").digest("hex").slice(0, 16)}`;
  const keys = managedStateKeys(f.root, stateSuffix);
  await state.update(keys.generationStateKey, { [generationId]: fingerprint });
  const manifest = { owner: "graphql-desk-analysis-generation", format: 2, id: generationId,
    artifactFingerprint: fingerprint };
  const observed = [];
  f.onPythonSettingChange(async value => {
    if (!value?.includes("/generations/")) return;
    const config = JSON.parse(fs.readFileSync(path.join(f.root, "pyrightconfig.json"), "utf8"));
    observed.push({ owned: managed.stillOwnsSetting(), value, projectStubPath: config.stubPath });
  });
  const selected = await managed.selectGeneration(generationDirectory, () => true, {
    directory: generationDirectory, manifest, manifestFingerprint: fingerprint,
    verify: () => ({ directory: generationDirectory, manifest, manifestFingerprint: fingerprint }),
  });
  assert.equal(selected, true);
  assert.equal(managed.selectedSetting, path.relative(f.root, generationDirectory).split(path.sep).join("/"));
  assert.equal(await managed.checkSettingOwnership(), true);
  assert.deepEqual(observed, [{ owned: true, value: path.relative(f.root, generationDirectory).split(path.sep).join("/"),
    projectStubPath: path.relative(f.root, generationDirectory).split(path.sep).join("/") }]);
  assert.equal(f.pythonStubPath, path.relative(f.root, generationDirectory).split(path.sep).join("/"));
  await managed.release();
  managed.cleanup();
  assert.equal(f.pythonStubPath, "python-custom");
});

test("managed config fails closed when existing stubs shadow generated package", async t => {
  const f = fixture(t, { json: '{"stubPath":"custom-stubs"}' });
  const shadow = path.join(f.root, "custom-stubs", "graphql_desk_generated");
  fs.mkdirSync(shadow, { recursive: true });
  fs.writeFileSync(path.join(shadow, "default.pyi"), "# user stub\n");
  const managed = await createManagedProjectConfig(f.vscode, f.folder, stateStore(), f.worker);
  await assert.rejects(managed.validateGeneratedPackages(["graphql_desk_generated"]), /shadows GraphQL Desk/);
  await managed.release();
  managed.cleanup();
  assert.equal(fs.readFileSync(path.join(shadow, "default.pyi"), "utf8"), "# user stub\n");
});

test("a user-owned reserved extraPath is not adopted", async t => {
  const f = fixture(t, { json: JSON.stringify({ extraPaths: [MANAGED_PATH] }) });
  await assert.rejects(createManagedProjectConfig(f.vscode, f.folder, stateStore(), f.worker), /reserved GraphQL Desk path/);
  assert.deepEqual(parseJsonConfig(fs.readFileSync(path.join(f.root, "pyrightconfig.json"), "utf8")).extraPaths, [MANAGED_PATH]);
});

test("a user-owned execution-environment path is not adopted", async t => {
  const config = JSON.stringify({ executionEnvironments: [{ root: "src", extraPaths: [MANAGED_PATH] }] });
  const f = fixture(t, { json: config });
  await assert.rejects(createManagedProjectConfig(f.vscode, f.folder, stateStore(), f.worker), /reserved GraphQL Desk path/);
  assert.equal(fs.readFileSync(path.join(f.root, "pyrightconfig.json"), "utf8"), config);
});
