"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const fsSync = require("node:fs");
const fs = require("node:fs/promises");
const { createHash } = require("node:crypto");
const os = require("node:os");
const path = require("node:path");
const { createManagedStubPath, managedStateKeys, updateManagedState } = require("../src/managedStubPath");
const { drainPublications } = require("../src/extension");

async function fixture(t, setting, dirty = false, settingLayer = "workspaceFolderValue") {
  const workspace = await fs.mkdtemp(path.join(os.tmpdir(), "graphql-desk-stub-path-"));
  const settingsPath = path.join(workspace, ".vscode", "settings.json");
  await fs.mkdir(path.dirname(settingsPath));
  const settings = { "editor.tabSize": 2 };
  await fs.writeFile(settingsPath, JSON.stringify(settings));
  const values = { "analysis.stubPath": setting };
  const updates = [];
  const stored = new Map();
  let rejectNextUpdate = false;
  let rejectNextStateUpdate = false;
  let delayedUpdate;
  let delayedAfterWrite;
  let onConfigUpdate;
  let onConfigWrite;
  let onStateUpdate;
  const workspaceState = { get: key => stored.get(key), update: async (key, value) => {
    onStateUpdate?.(value);
    if (rejectNextStateUpdate) { rejectNextStateUpdate = false; throw new Error("workspace state write failed"); }
    stored.set(key, value);
  } };
  const config = {
    get: (_key, fallback) => values["analysis.stubPath"] ?? fallback,
    inspect: () => ({ defaultValue: "./typings", [settingLayer]: values["analysis.stubPath"] }),
    async update(key, value, target) {
      assert.equal(arguments.length, 3, "native configuration updates keep VS Code's argument contract");
      updates.push([key, value, target]);
      assert.equal(key, "analysis.stubPath");
      assert.equal(target, 2);
      if (rejectNextUpdate) { rejectNextUpdate = false; throw new Error("settings write failed"); }
      onConfigUpdate?.();
      if (delayedUpdate) { const delay = delayedUpdate; delayedUpdate = null; await delay.promise; }
      values[key] = value;
      settings[key] = value;
      await fs.writeFile(settingsPath, JSON.stringify(settings));
      onConfigWrite?.();
      if (delayedAfterWrite) { const delay = delayedAfterWrite; delayedAfterWrite = null; await delay.promise; }
    },
  };
  const vscode = { ConfigurationTarget: { WorkspaceFolder: 2 }, workspace: {
    isTrusted: true, workspaceFile: null,
    textDocuments: dirty ? [{ uri: { scheme: "file", fsPath: settingsPath }, isDirty: true }] : [],
    getConfiguration: () => config,
  } };
  const folder = { uri: { scheme: "file", fsPath: workspace } };
  t.after(() => fs.rm(workspace, { recursive: true, force: true }));
  return { workspace, settings, settingsPath, vscode, folder, values, updates, workspaceState,
    rejectNextUpdate() { rejectNextUpdate = true; },
    rejectNextStateUpdate() { rejectNextStateUpdate = true; },
    delayNextUpdate() { let release; const promise = new Promise(resolve => { release = resolve; }); delayedUpdate = { promise }; return release; },
    delayNextUpdateAfterWrite() { let release; const promise = new Promise(resolve => { release = resolve; }); delayedAfterWrite = { promise }; return release; },
    onConfigUpdate(callback) { onConfigUpdate = callback; },
    onConfigWrite(callback) { onConfigWrite = callback; },
    onStateUpdate(callback) { onStateUpdate = callback; },
    stored };
}

async function ownGeneration(managed, verification) {
  verification.manifestFingerprint ??= "a".repeat(64);
  assert.equal(await managed.recordGeneration(verification.directory, verification.manifestFingerprint), true);
}

test("only a custom configuration adapter receives the final synchronous generation guard", async t => {
  const f = await fixture(t, undefined);
  const config = f.vscode.workspace.getConfiguration();
  const update = config.update.bind(config);
  let current = true;
  let finalGuard;
  config.update = async (key, value, target, guard) => {
    finalGuard = guard;
    await new Promise(resolve => setImmediate(resolve));
    current = false;
    if (guard && !guard()) throw new Error("custom configuration became stale before commit");
    return update(key, value, target);
  };
  const managed = await createManagedStubPath(f.vscode, f.folder, f.workspaceState, { config });
  const generation = path.join(managed.root, "generations", "g-1-00000000-0000-0000-0000-000000000000");
  await fs.mkdir(generation, { recursive: true });
  const verified = { directory: generation, manifest: { owner: "graphql-desk-analysis-generation", format: 2, id: path.basename(generation), artifactFingerprint: "a" }, verify() { return this; } };
  await ownGeneration(managed, verified);
  await assert.rejects(managed.selectGeneration(generation, () => current, verified), /custom configuration became stale/);
  assert.equal(typeof finalGuard, "function");
  assert.equal(f.values["analysis.stubPath"], undefined);
  assert.equal(f.updates.length, 0, "stale generations are never temporarily committed");
});

for (const layer of ["workspaceValue", "globalValue"]) {
  test(`managed-root initialization does not absorb ${layer} changes during its ownership write`, async t => {
    const f = await fixture(t, undefined);
    const config = f.vscode.workspace.getConfiguration();
    const values = {};
    config.inspect = () => ({ ...values });
    config.get = (_name, fallback) => values.workspaceFolderValue ?? values.workspaceValue ?? values.globalValue ?? fallback;
    let entered, release;
    const started = new Promise(resolve => { entered = resolve; });
    const gate = new Promise(resolve => { release = resolve; });
    const update = f.workspaceState.update;
    f.workspaceState.update = async (key, value) => { await update(key, value); entered(); await gate; };
    const pending = createManagedStubPath(f.vscode, f.folder, f.workspaceState);
    await Promise.race([started, pending.then(() => assert.fail("the ownership write must be held"))]);
    values[layer] = "user-stubs";
    release();
    const managed = await pending;
    assert.equal(managed.routingSettingsCurrent(), false);
    await managed.releaseForRouting();
    assert.equal(config.get("analysis.stubPath"), "user-stubs");
    assert.equal(f.updates.length, 0);
  });
}

test("fallback routing release drains native selection and removes only its completed owned override", async t => {
  const f = await fixture(t, undefined);
  const managed = await createManagedStubPath(f.vscode, f.folder, f.workspaceState);
  const generation = path.join(managed.root, "generations", "g-1-00000000-0000-0000-0000-000000000000");
  await fs.mkdir(generation, { recursive: true });
  const verification = { directory: generation, manifest: { owner: "graphql-desk-analysis-generation", format: 2,
    id: path.basename(generation), artifactFingerprint: "a" }, manifestFingerprint: "b".repeat(64), verify() { return this; } };
  await ownGeneration(managed, verification);
  let started;
  const entered = new Promise(resolve => { started = resolve; });
  f.onConfigUpdate(() => started());
  const finishUpdate = f.delayNextUpdate();
  const selecting = managed.selectGeneration(generation, () => true, verification);
  await Promise.race([entered, selecting.then(() => assert.fail("native selection should be held"))]);
  let finished = false;
  const releasing = managed.releaseForRouting().then(() => { finished = true; });
  try {
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(finished, false);
    assert.equal(f.updates.length, 1);
  } finally { finishUpdate(); }
  assert.equal(await selecting, true);
  await releasing;
  assert.equal(f.values["analysis.stubPath"], undefined);
  assert.equal(managed.ownsSetting, false);
  assert.equal(f.stored.get(managedStateKeys(f.workspace).stateKey), undefined);
  assert.equal(fsSync.existsSync(generation), true, "routing does not assert that readers released an old generation");
});

test("fallback routing preserves an external choice and refuses dirty or stale settings", async t => {
  for (const boundary of ["external", "dirty", "stale"]) {
    const f = await fixture(t, undefined);
    const managed = await createManagedStubPath(f.vscode, f.folder, f.workspaceState);
    if (boundary === "external") f.values["analysis.stubPath"] = "new-user-stubs";
    if (boundary === "dirty") f.vscode.workspace.textDocuments.push({ isDirty: true,
      uri: { scheme: "file", fsPath: f.settingsPath } });
    if (boundary === "external") {
      await managed.releaseForRouting();
      assert.equal(f.values["analysis.stubPath"], "new-user-stubs");
      assert.equal(managed.ownsSetting, false);
    } else {
      await assert.rejects(managed.releaseForRouting(() => boundary !== "stale"), /changed or dirty/);
      assert.equal(managed.ownsSetting, true);
    }
    assert.deepEqual(f.updates, [], "no external or dirty setting is rewritten");
  }
});

test("prepares an owned root without changing stubPath before a complete generation exists", async t => {
  const f = await fixture(t, undefined);
  const managed = await createManagedStubPath(f.vscode, f.folder, f.workspaceState);
  assert.equal(managed.root, path.join(f.workspace, ".graphql-desk", "typings"));
  assert.equal(managed.ownsSetting, true);
  assert.equal(f.values["analysis.stubPath"], undefined);
  assert.equal(f.updates.length, 0);
  assert.equal(f.settings["editor.tabSize"], 2);
  await fs.writeFile(path.join(managed.root, "keep.txt"), "user data");
  managed.cleanup();
  assert.equal(await fs.readFile(path.join(managed.root, "keep.txt"), "utf8"), "user data");
  assert.equal(await fs.stat(path.join(f.workspace, ".graphql-desk" )).then(() => true), true);
});

test("rotates only the extension-owned setting and restores ownership across activation", async t => {
  const f = await fixture(t, undefined);
  const first = await createManagedStubPath(f.vscode, f.folder, f.workspaceState);
  const generations = path.join(first.root, "generations");
  await fs.mkdir(generations);
  const generation = path.join(generations, "g-1-00000000-0000-0000-0000-000000000000");
  await fs.mkdir(generation);
  const verified = { directory: generation, manifest: { owner: "graphql-desk-analysis-generation", format: 2, id: path.basename(generation), artifactFingerprint: "a" }, verify() { return this; } };
  await ownGeneration(first, verified);
  assert.equal(await first.selectGeneration(generation, () => true, verified), true);
  assert.equal(f.values["analysis.stubPath"], ".graphql-desk/typings/generations/g-1-00000000-0000-0000-0000-000000000000");
  const restarted = await createManagedStubPath(f.vscode, f.folder, f.workspaceState);
  assert.equal(restarted.ownsSetting, true);
  assert.equal(restarted.root, first.root);
  assert.equal(restarted.selectedSetting, f.values["analysis.stubPath"]);

  const custom = await fixture(t, "custom-stubs");
  const userManager = await createManagedStubPath(custom.vscode, custom.folder, custom.workspaceState);
  assert.equal(userManager.ownsSetting, false);
  const customPath = custom.values["analysis.stubPath"];
  assert.equal(await userManager.selectGeneration(generation, () => true, verified), false);
  assert.equal(custom.values["analysis.stubPath"], customPath);
});

test("setting-change checks wait for selection and retain an intervening user override", async t => {
  const f = await fixture(t, undefined);
  const managed = await createManagedStubPath(f.vscode, f.folder, f.workspaceState);
  const generations = path.join(managed.root, "generations");
  await fs.mkdir(generations);
  const generation = path.join(generations, "g-1-00000000-0000-0000-0000-000000000000");
  await fs.mkdir(generation);
  const verified = { directory: generation, manifest: { owner: "graphql-desk-analysis-generation", format: 2,
    id: path.basename(generation), artifactFingerprint: "a" }, verify() { return this; } };
  await ownGeneration(managed, verified);
  const releaseUpdate = f.delayNextUpdateAfterWrite();
  let writeFinished;
  const written = new Promise(resolve => { writeFinished = resolve; });
  f.onConfigWrite(writeFinished);

  const selecting = managed.selectGeneration(generation, () => true, verified);
  await written;
  const settled = managed.waitForGenerationSelection();
  f.values["analysis.stubPath"] = "user-stubs";
  f.settings["analysis.stubPath"] = "user-stubs";
  await fs.writeFile(f.settingsPath, JSON.stringify(f.settings));
  releaseUpdate();
  await assert.rejects(selecting, /ownership changed/);
  await settled;
  assert.equal(f.values["analysis.stubPath"], "user-stubs");
  assert.equal(managed.ownsSetting, false);
  assert.equal(managed.stillOwnsSetting(), false);
  assert.equal(await managed.checkSettingOwnership(), false);
  assert.equal(f.stored.get(managedStateKeys(f.workspace).stateKey), undefined);
});

test("ownership inspection waits for an in-flight selection rollback", async t => {
  const f = await fixture(t, undefined);
  const managed = await createManagedStubPath(f.vscode, f.folder, f.workspaceState);
  const directory = path.join(managed.root, "generations", "g-1-00000000-0000-0000-0000-000000000000");
  await fs.mkdir(directory, { recursive: true });
  const verified = { directory, manifest: { owner: "graphql-desk-analysis-generation", format: 2,
    id: path.basename(directory), artifactFingerprint: "a" }, verify() { return this; } };
  await ownGeneration(managed, verified);
  let current = true;
  let selected;
  const written = new Promise(resolve => { selected = resolve; });
  f.onConfigWrite(selected);
  const finishSelection = f.delayNextUpdateAfterWrite();
  const selecting = managed.selectGeneration(directory, () => current, verified);
  await written;
  current = false;
  const finishRollback = f.delayNextUpdate();
  let rollingBack;
  const rollback = new Promise(resolve => { rollingBack = resolve; });
  f.onConfigUpdate(rollingBack);
  finishSelection();
  await rollback;
  assert.equal(managed.stillOwnsSetting(), false, "the local selection changes before the setting rollback completes");
  let inspected = false;
  const owned = managed.checkSettingOwnership(() => { inspected = true; return true; });
  await Promise.resolve();
  assert.equal(inspected, false, "ownership must not be inspected in the middle of the rollback");
  finishRollback();
  assert.equal(await selecting, false);
  assert.equal(await owned, true);
  assert.equal(inspected, true);
  assert.equal(f.values["analysis.stubPath"], undefined);
});

test("shutdown preserves the completed generation when its setting update is in flight", async t => {
  const f = await fixture(t, undefined);
  const managed = await createManagedStubPath(f.vscode, f.folder, f.workspaceState);
  const directory = path.join(managed.root, "generations", "g-1-00000000-0000-0000-0000-000000000000");
  await fs.mkdir(directory, { recursive: true });
  const verified = { directory, manifest: { owner: "graphql-desk-analysis-generation", format: 2,
    id: path.basename(directory), artifactFingerprint: "a" }, verify() { return this; } };
  await ownGeneration(managed, verified);
  const finishUpdate = f.delayNextUpdateAfterWrite();
  let selected;
  const written = new Promise(resolve => { selected = resolve; });
  f.onConfigWrite(selected);
  let cancelled = false;
  const selecting = managed.selectGeneration(directory, () => !cancelled, verified);
  await written;
  const shutdown = drainPublications(new Set([selecting]), () => { cancelled = true; });
  await Promise.resolve();
  assert.equal(cancelled, false, "the current request stays valid until the selection commits");
  finishUpdate();
  await shutdown;
  assert.equal(await selecting, true);
  assert.equal(cancelled, true);
  assert.equal(path.resolve(f.workspace, f.values["analysis.stubPath"]), directory);
  assert.equal(JSON.parse(await fs.readFile(f.settingsPath, "utf8"))["analysis.stubPath"], managed.selectedSetting);
});

test("recovers the reserved managed root after workspace state is lost", async t => {
  const f = await fixture(t, undefined);
  const first = await createManagedStubPath(f.vscode, f.folder, f.workspaceState);
  const generations = path.join(first.root, "generations");
  await fs.mkdir(generations);
  const generation = path.join(generations, "g-1-00000000-0000-0000-0000-000000000000");
  await fs.mkdir(generation);
  await fs.writeFile(path.join(generation, "complete.json"), JSON.stringify({
    owner: "graphql-desk-analysis-generation", format: 2, id: path.basename(generation), sequence: 1,
    files: [], operationManifest: [], inputFingerprint: "b".repeat(64), artifactFingerprint: "a".repeat(64),
  }));
  const verified = { directory: generation, manifest: { owner: "graphql-desk-analysis-generation", format: 2, id: path.basename(generation) }, verify() { return this; } };
  await ownGeneration(first, verified);
  assert.equal(await first.selectGeneration(generation, () => true, { ...verified, manifestFingerprint: "a".repeat(64) }), true);
  f.stored.clear();

  const restarted = await createManagedStubPath(f.vscode, f.folder, f.workspaceState);
  assert.equal(restarted.ownsSetting, true);
  assert.equal(restarted.root, first.root);
  assert.equal(restarted.selectedSetting, f.values["analysis.stubPath"]);
  assert.equal(restarted.stillOwnsSetting(), true);
});

test("does not claim a global stubPath that resembles a GraphQL Desk generation", async t => {
  const setting = ".graphql-desk/typings/generations/g-1-00000000-0000-0000-0000-000000000000";
  const f = await fixture(t, setting, false, "globalValue");
  const directory = path.resolve(f.workspace, setting);
  await fs.mkdir(directory, { recursive: true });
  await fs.writeFile(path.join(directory, "complete.json"), JSON.stringify({
    owner: "graphql-desk-analysis-generation", format: 2, id: path.basename(directory), sequence: 1,
    files: [], operationManifest: [], inputFingerprint: "b".repeat(64), artifactFingerprint: "a".repeat(64),
  }));

  const managed = await createManagedStubPath(f.vscode, f.folder, f.workspaceState);
  assert.equal(managed.ownsSetting, false);
  assert.equal(f.values["analysis.stubPath"], setting);
});

test("does not infer GraphQL Desk ownership from an incomplete or linked generation", async t => {
  for (const linked of [false, true]) {
    const f = await fixture(t, ".graphql-desk/typings/generations/g-1-00000000-0000-0000-0000-000000000000");
    const directory = path.resolve(f.workspace, f.values["analysis.stubPath"]);
    await fs.mkdir(path.dirname(directory), { recursive: true });
    if (linked) {
      const target = path.join(f.workspace, "foreign");
      await fs.mkdir(target);
      await fs.symlink(target, directory, "junction");
    }
    else await fs.mkdir(directory);
    if (linked) await assert.rejects(createManagedStubPath(f.vscode, f.folder, f.workspaceState), /not a safe directory/);
    else {
      const managed = await createManagedStubPath(f.vscode, f.folder, f.workspaceState);
      assert.equal(managed.ownsSetting, false);
    }
    assert.equal(f.values["analysis.stubPath"], ".graphql-desk/typings/generations/g-1-00000000-0000-0000-0000-000000000000");
  }
});

test("recovers ownership when activation interrupts a managed stubPath rotation", async t => {
  const f = await fixture(t, undefined);
  const first = await createManagedStubPath(f.vscode, f.folder, f.workspaceState);
  const generations = path.join(first.root, "generations");
  await fs.mkdir(generations);
  const previous = ".graphql-desk/typings/generations/g-1-00000000-0000-0000-0000-000000000000";
  const next = ".graphql-desk/typings/generations/g-2-00000000-0000-0000-0000-000000000000";
  const previousPath = path.join(f.workspace, previous);
  const nextPath = path.join(f.workspace, next);
  await fs.mkdir(previousPath);
  await fs.mkdir(nextPath);
  const verified = { directory: nextPath, manifest: {
    owner: "graphql-desk-analysis-generation", format: 2,
    id: path.basename(nextPath), artifactFingerprint: "a",
  }, verify() { return this; } };
  await ownGeneration(first, verified);

  const releaseSettingsWrite = f.delayNextUpdate();
  const selection = first.selectGeneration(nextPath, () => true, verified);
  await new Promise(resolve => setImmediate(resolve));
  const pendingOwner = [...f.stored.values()].find(value => value?.pending === true);
  assert.deepEqual(pendingOwner, {
    base: ".graphql-desk/typings", current: next,
    previous: "", previousPresent: false, pending: true,
  });
  assert.equal(f.values["analysis.stubPath"], undefined, "the setting write has not happened yet");

  const restarted = await createManagedStubPath(f.vscode, f.folder, f.workspaceState);
  assert.equal(restarted.ownsSetting, true);
  assert.equal(restarted.selectedSetting, "");
  assert.equal(restarted.stillOwnsSetting(), true);
  releaseSettingsWrite();
  assert.equal(await selection, true);
  assert.deepEqual([...f.stored.values()].find(value => value?.current === next), {
    base: ".graphql-desk/typings", current: next, currentPresent: true,
  }, "successful rotation clears the pending marker");

  f.values["analysis.stubPath"] = ".graphql-desk/typings";
  const userOverride = await createManagedStubPath(f.vscode, f.folder, f.workspaceState);
  assert.equal(userOverride.ownsSetting, false, "a third setting value remains user-owned");
  assert.equal(f.values["analysis.stubPath"], ".graphql-desk/typings");
  first.cleanup();
  restarted.cleanup();
  userOverride.cleanup();
});

test("recognizes owned stubPath when VS Code stores a single-folder setting at workspace scope", async t => {
  const f = await fixture(t, undefined, false, "workspaceValue");
  const managed = await createManagedStubPath(f.vscode, f.folder, f.workspaceState);
  const generations = path.join(managed.root, "generations");
  await fs.mkdir(generations);
  const generation = path.join(generations, "g-1-00000000-0000-0000-0000-000000000000");
  await fs.mkdir(generation);
  const verified = { directory: generation, manifest: { owner: "graphql-desk-analysis-generation", format: 2, id: path.basename(generation), artifactFingerprint: "a" }, verify() { return this; } };
  await ownGeneration(managed, verified);
  assert.equal(await managed.selectGeneration(generation, () => true, verified), true);
  assert.equal(managed.stillOwnsSetting(), true);
  const restarted = await createManagedStubPath(f.vscode, f.folder, f.workspaceState);
  assert.equal(restarted.ownsSetting, true);
  assert.equal(restarted.selectedSetting, f.values["analysis.stubPath"]);
});

test("rolls ownership state back when the workspace setting write fails before mutation", async t => {
  const f = await fixture(t, undefined);
  const managed = await createManagedStubPath(f.vscode, f.folder, f.workspaceState);
  const generations = path.join(managed.root, "generations");
  await fs.mkdir(generations);
  const generation = path.join(generations, "g-1-00000000-0000-0000-0000-000000000000");
  await fs.mkdir(generation);
  const verified = { directory: generation, manifest: { owner: "graphql-desk-analysis-generation", format: 2, id: path.basename(generation), artifactFingerprint: "a" }, verify() { return this; } };
  await ownGeneration(managed, verified);
  f.rejectNextUpdate();
  await assert.rejects(managed.selectGeneration(generation, () => true, verified), /settings write failed/);
  assert.equal(f.values["analysis.stubPath"], undefined);
  assert.equal(managed.selectedSetting, "");
  assert.equal(managed.stillOwnsSetting(), true);
  assert.ok([...f.stored.values()].some(value => value.current === ""), "persisted ownership must match the unchanged setting");
});

test("restores in-memory ownership when persisting the selected setting fails before mutation", async t => {
  const f = await fixture(t, undefined);
  const managed = await createManagedStubPath(f.vscode, f.folder, f.workspaceState);
  const generations = path.join(managed.root, "generations");
  await fs.mkdir(generations);
  const generation = path.join(generations, "g-1-00000000-0000-0000-0000-000000000000");
  await fs.mkdir(generation);
  const verified = { directory: generation, manifest: { owner: "graphql-desk-analysis-generation", format: 2, id: path.basename(generation), artifactFingerprint: "a" }, verify() { return this; } };
  await ownGeneration(managed, verified);
  f.rejectNextStateUpdate();
  await assert.rejects(managed.selectGeneration(generation, () => true, verified), /workspace state write failed/);
  assert.equal(f.values["analysis.stubPath"], undefined);
  assert.equal(managed.selectedSetting, "");
  assert.equal(managed.stillOwnsSetting(), true);
  assert.ok([...f.stored.values()].some(value => value.current === ""), "the previous ownership record remains authoritative");
});

test("serializes overlapping generation selection so the latest edit wins", async t => {
  const f = await fixture(t, undefined);
  const managed = await createManagedStubPath(f.vscode, f.folder, f.workspaceState);
  const generations = path.join(managed.root, "generations");
  await fs.mkdir(generations);
  const first = path.join(generations, "g-1-00000000-0000-0000-0000-000000000000");
  const second = path.join(generations, "g-2-00000000-0000-0000-0000-000000000000");
  await fs.mkdir(first);
  await fs.mkdir(second);
  const verified = directory => ({ directory, manifest: { owner: "graphql-desk-analysis-generation", format: 2, id: path.basename(directory), artifactFingerprint: "a" }, verify() { return this; } });
  const firstVerification = verified(first);
  const secondVerification = verified(second);
  await ownGeneration(managed, firstVerification);
  await ownGeneration(managed, secondVerification);
  let firstIsCurrent = true;
  const releaseFirstWrite = f.delayNextUpdate();
  const oldSelection = managed.selectGeneration(first, () => firstIsCurrent, firstVerification);
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(f.updates.length, 1, "the first update is waiting on the simulated slow settings write");
  firstIsCurrent = false;
  const latestSelection = managed.selectGeneration(second, () => true, secondVerification);
  releaseFirstWrite();
  assert.equal(await oldSelection, false);
  assert.equal(await latestSelection, true);
  assert.equal(managed.selectedSetting, path.relative(f.workspace, second).split(path.sep).join("/"));
  assert.equal(f.values["analysis.stubPath"], managed.selectedSetting);
});

test("waits for generation rotation before recording staging ownership", async t => {
  const f = await fixture(t, undefined);
  const managed = await createManagedStubPath(f.vscode, f.folder, f.workspaceState);
  const generations = path.join(managed.root, "generations");
  await fs.mkdir(generations);
  const generation = path.join(generations, "g-1-00000000-0000-0000-0000-000000000000");
  await fs.mkdir(generation);
  const verified = { directory: generation, manifest: {
    owner: "graphql-desk-analysis-generation", format: 2, id: path.basename(generation), artifactFingerprint: "a",
  }, verify() { return this; } };
  await ownGeneration(managed, verified);

  const releaseSettingsWrite = f.delayNextUpdate();
  const selection = managed.selectGeneration(generation, () => true, verified);
  await new Promise(resolve => setImmediate(resolve));
  let stagingSettled = false;
  const staging = managed.recordStaging(generation, "b".repeat(64),
    ".graphql-desk-00000000-0000-0000-0000-000000000001-marker-00000000-0000-0000-0000-000000000002.tmp")
    .then(result => { stagingSettled = true; return result; });
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(stagingSettled, false, "staging waits while the configured path is rotating");

  releaseSettingsWrite();
  assert.equal(await selection, true);
  assert.equal(await staging, true);
});

test("refuses staging when Pyright ownership changed and preserves the user setting", async t => {
  const f = await fixture(t, undefined);
  const managed = await createManagedStubPath(f.vscode, f.folder, f.workspaceState);
  f.values["analysis.stubPath"] = "user-stubs";
  const generation = path.join(managed.root, "generations", "g-1-00000000-0000-0000-0000-000000000000");
  await assert.rejects(managed.recordStaging(generation, "a".repeat(64),
    ".graphql-desk-00000000-0000-0000-0000-000000000001-marker-00000000-0000-0000-0000-000000000002.tmp"),
  /ownership changed.*expected.*actual/);

  assert.deepEqual(managed.stagingEntries(), {});
  assert.equal(f.values["analysis.stubPath"], "user-stubs");
});

test("serializes staging ownership and release cleanup across managers", async t => {
  const f = await fixture(t, undefined);
  const first = await createManagedStubPath(f.vscode, f.folder, f.workspaceState);
  const second = await createManagedStubPath(f.vscode, f.folder, f.workspaceState);
  const stagingStateKey = managedStateKeys(f.workspace).stagingStateKey;
  const update = f.workspaceState.update;
  f.workspaceState.update = async (key, value) => {
    if (key === stagingStateKey) await new Promise(resolve => setImmediate(resolve));
    return update(key, value);
  };
  const generations = path.join(first.root, "generations");
  const paths = [
    path.join(generations, "g-1-00000000-0000-0000-0000-000000000000"),
    path.join(generations, "g-2-00000000-0000-0000-0000-000000000000"),
  ];
  const temporary = [
    ".graphql-desk-00000000-0000-0000-0000-000000000001-marker-00000000-0000-0000-0000-000000000002.tmp",
    ".graphql-desk-00000000-0000-0000-0000-000000000003-marker-00000000-0000-0000-0000-000000000004.tmp",
  ];
  const results = await Promise.all([
    first.recordStaging(paths[0], "a".repeat(64), temporary[0]),
    second.recordStaging(paths[1], "b".repeat(64), temporary[1]),
  ]);
  assert.deepEqual(results, [true, true]);
  assert.equal(Object.keys(first.stagingEntries()).length, 2);
  const cleanup = updateManagedState(f.workspaceState, stagingStateKey, undefined);
  const next = first.recordStaging(
    path.join(generations, "g-3-00000000-0000-0000-0000-000000000000"), "c".repeat(64),
    ".graphql-desk-00000000-0000-0000-0000-000000000005-marker-00000000-0000-0000-0000-000000000006.tmp");
  await cleanup;
  assert.equal(await next, true);
  assert.deepEqual(Object.keys(first.stagingEntries()), ["g-3-00000000-0000-0000-0000-000000000000"]);
});

test("a stale first generation rollback leaves an originally absent stubPath unset", async t => {
  const f = await fixture(t, undefined);
  const managed = await createManagedStubPath(f.vscode, f.folder, f.workspaceState);
  const generations = path.join(managed.root, "generations");
  await fs.mkdir(generations);
  const generation = path.join(generations, "g-1-00000000-0000-0000-0000-000000000000");
  await fs.mkdir(generation);
  const verified = { directory: generation, manifest: {
    owner: "graphql-desk-analysis-generation", format: 2, id: path.basename(generation), artifactFingerprint: "a",
  }, verify() { return this; } };
  await ownGeneration(managed, verified);
  let currentChecks = 0;

  assert.equal(await managed.selectGeneration(generation, () => ++currentChecks === 1, verified), false);
  assert.equal(f.values["analysis.stubPath"], undefined);
  assert.equal(Object.hasOwn(JSON.parse(await fs.readFile(f.settingsPath, "utf8")), "analysis.stubPath"), false,
    "rollback must remove its override instead of writing an empty value");
  assert.deepEqual(f.updates.map(([, value]) => value), [
    ".graphql-desk/typings/generations/g-1-00000000-0000-0000-0000-000000000000", undefined,
  ]);
  assert.ok([...f.stored.values()].some(value => value.current === "" && value.currentPresent === false),
    "persisted ownership records that the prior setting was absent");
});

test("revalidates generation bytes immediately before selecting stubPath", async t => {
  const f = await fixture(t, undefined);
  const managed = await createManagedStubPath(f.vscode, f.folder, f.workspaceState);
  const generations = path.join(managed.root, "generations");
  await fs.mkdir(generations);
  const generation = path.join(generations, "g-1-00000000-0000-0000-0000-000000000000");
  await fs.mkdir(generation);
  const artifact = path.join(generation, "generated.pyi");
  const originalBytes = Buffer.from("owned generated bytes");
  await fs.writeFile(artifact, originalBytes);
  const fingerprint = createHash("sha256").update(originalBytes).digest("hex");
  const targetSetting = path.relative(f.workspace, generation).split(path.sep).join("/");
  f.onStateUpdate(value => {
    if (value?.current === targetSetting) fsSync.writeFileSync(artifact, "changed during state update");
  });
  const verified = { directory: generation, manifest: { owner: "graphql-desk-analysis-generation", format: 2, id: path.basename(generation), artifactFingerprint: "a" },
    verify() {
      const current = fsSync.readFileSync(artifact);
      if (createHash("sha256").update(current).digest("hex") !== fingerprint) throw new Error("generation changed");
      return this;
    } };
  verified.manifestFingerprint = "b".repeat(64);
  await ownGeneration(managed, verified);
  await assert.rejects(managed.selectGeneration(generation, () => true, verified), /generation changed/);
  assert.equal(f.values["analysis.stubPath"], undefined);
  assert.equal(managed.selectedSetting, "");
  assert.equal(f.updates.length, 0, "Pylance is not pointed at a generation that changed during ownership persistence");
  assert.equal(await fs.readFile(artifact, "utf8"), "changed during state update", "foreign mutation is preserved");
});

test("reuses a workspace-local custom stubPath and refuses external or dirty settings", async t => {
  const local = await fixture(t, "typings");
  const reused = await createManagedStubPath(local.vscode, local.folder);
  assert.equal(reused.root, path.join(local.workspace, "typings"));
  assert.equal(local.values["analysis.stubPath"], "typings");
  reused.cleanup();

  const external = await fixture(t, path.join(os.tmpdir(), "custom-stubs"));
  await assert.rejects(createManagedStubPath(external.vscode, external.folder), /outside the workspace/);
  assert.equal(external.values["analysis.stubPath"], path.join(os.tmpdir(), "custom-stubs"));

  const dirty = await fixture(t, undefined, true);
  await assert.rejects(createManagedStubPath(dirty.vscode, dirty.folder), /Dirty workspace settings/);
  assert.equal(dirty.values["analysis.stubPath"], undefined);
  await assert.rejects(fs.stat(path.join(dirty.workspace, ".graphql-desk")), { code: "ENOENT" });
});

test("preserves an explicitly blank stubPath and uses Pylance's default typings directory", async t => {
  const f = await fixture(t, "");
  const managed = await createManagedStubPath(f.vscode, f.folder);
  assert.equal(managed.root, path.join(f.workspace, "typings"));
  assert.equal(f.updates.length, 0);
  managed.cleanup();
});
