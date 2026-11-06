"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { createHash, randomUUID } = require("node:crypto");
const test = require("node:test");
const { spawnSync } = require("node:child_process");
const { createAnalysisWorker } = require("../src/analysisWorker");
const { parseJsonConfig, modifyJsonConfig, updateProjectConfig: rawUpdateProjectConfig,
  createManagedProjectConfig: rawCreateManagedProjectConfig, inspectProjectConfigRoute, acknowledgePendingConfigWrite, MANAGED_PATH, stateKey } = require("../src/projectPyrightConfig");
const { createManagedStubPath, managedStateKeys } = require("../src/managedStubPath");
const { reconfigureWorkspaceRoots } = require("../src/extension");
const { createAnalysisGenerations } = require("../src/analysisGenerations");
const fixtures = new WeakMap();

function createManagedProjectConfig(vscode, folder, state, worker, options = {}) {
  return rawCreateManagedProjectConfig(vscode, folder, state, worker,
    { configFile: fixtures.get(vscode)?.configFile,
      ownerCurrent: () => vscode.workspace.isTrusted === true && vscode.workspace.workspaceFolders.includes(folder) &&
        vscode.workspace.getWorkspaceFolder(folder.uri) === folder, ...options });
}

function updateProjectConfig(vscode, folder, action, worker, ownership = {}, managedPath = MANAGED_PATH, onOwnedWrite, isCurrent = () => true) {
  const fixture = fixtures.get(vscode);
  const journal = async (file, hash, operation) => {
    fixture.journal.push(operation);
    if (!["staged", "discarding"].includes(operation.phase) && onOwnedWrite && await onOwnedWrite(file, hash, operation) === false) return false;
    return true;
  };
  return rawUpdateProjectConfig(vscode, folder, action, worker, ownership, managedPath, journal, fixture?.configFile, isCurrent);
}

// Contract fake only; it does not prove native ACL preservation.
function configFileFixture(vscode, folder, root) {
  let pending;
  let hook = () => {};
  const calls = [];
  const directoryProof = () => {
    const stat = fs.lstatSync(root, { bigint: true });
    if (!stat.isDirectory() || stat.isSymbolicLink()) throw new Error("Workspace changed; preserved");
    return [stat.dev, stat.ino, stat.mode, stat.uid, stat.gid].map(String);
  };
  const proof = filename => {
    const stat = fs.lstatSync(filename, { bigint: true });
    if (!stat.isFile() || stat.isSymbolicLink() || stat.size > 1024 * 1024) throw new Error("Config proof changed; preserved");
    return [stat.dev, stat.ino, stat.size, stat.mtimeNs, stat.mode, stat.uid, stat.gid].map(String)
      .concat(createHash("sha256").update(fs.readFileSync(filename)).digest("hex"));
  };
  const matches = (filename, expected) => {
    try { return JSON.stringify(proof(filename)) === JSON.stringify(expected); }
    catch { return false; }
  };
  const exists = filename => {
    try { fs.lstatSync(filename); return true; }
    catch (error) { if (error.code === "ENOENT") return false; throw error; }
  };
  const configFile = async (targetFolder, action, payload) => {
    if (!vscode.workspace.isTrusted || targetFolder !== folder ||
        vscode.workspace.getWorkspaceFolder(vscode.Uri.file(root)) !== folder) throw new Error("Trusted config workspace changed; preserved");
    calls.push({ action, ...payload });
    await hook(action, payload);
    if (action === "stage") {
      if (pending) throw new Error("Pending stage needs recovery");
      const file = path.join(root, payload.file);
      assert.ok(["pyproject.toml", "pyrightconfig.json"].includes(payload.file));
      assert.equal(path.dirname(payload.temporary), root);
      assert.match(path.basename(payload.temporary), /^\.graphql-desk-[a-f0-9-]{36}\.tmp$/);
      const original = proof(file);
      assert.equal(original.at(-1), payload.expected_hash);
      const bytes = Buffer.from(payload.text, "utf8");
      assert.ok(bytes.length <= 1024 * 1024);
      const descriptor = fs.openSync(payload.temporary, "wx", 0o600);
      try {
        fs.fchmodSync(descriptor, Number(BigInt(original[4]) & 0o7777n));
        fs.writeFileSync(descriptor, bytes);
        fs.fsyncSync(descriptor);
      } finally { fs.closeSync(descriptor); }
      const token = randomUUID().replaceAll("-", "");
      pending = { token, file, temporary: payload.temporary, original, staged: proof(payload.temporary), directory: directoryProof() };
      const receipt = Buffer.from(JSON.stringify(pending)).toString("base64");
      return { ok: true, token, receipt, original_hash: payload.expected_hash,
        staged_hash: createHash("sha256").update(bytes).digest("hex") };
    }
    if (action === "resume") {
      if (pending) throw new Error("Pending stage needs recovery");
      const recovered = JSON.parse(Buffer.from(payload.receipt, "base64").toString("utf8"));
      assert.equal(path.dirname(recovered.file), root);
      assert.equal(path.dirname(recovered.temporary), root);
      assert.deepEqual(recovered.directory, directoryProof());
      if (exists(recovered.temporary) ? !matches(recovered.temporary, recovered.staged) :
        !matches(recovered.file, recovered.staged) && !matches(recovered.file, recovered.original)) {
        throw new Error("Config recovery proof changed; preserved");
      }
      pending = recovered;
      return { ok: true, token: pending.token };
    }
    if (!pending || payload.token !== pending.token) throw new Error("Unknown config stage");
    assert.deepEqual(pending.directory, directoryProof());
    const token = pending.token;
    if (action === "verify") {
      if (!matches(pending.file, pending.original) || !matches(pending.temporary, pending.staged)) throw new Error("Config proof changed; preserved");
    } else if (action === "verify_discard") {
      if (exists(pending.temporary) && !matches(pending.temporary, pending.staged)) throw new Error("Config temporary changed; preserved");
    } else if (action === "discard") {
      if (exists(pending.temporary)) throw new Error("Config temporary still exists; preserved");
      pending = undefined;
    } else if (action === "finish") {
      if (exists(pending.temporary) || !matches(pending.file, pending.staged)) throw new Error("Published config proof changed; preserved");
      pending = undefined;
    } else throw new Error("Unexpected config action");
    return { ok: true, token };
  };
  configFile.calls = calls;
  configFile.restart = () => { pending = undefined; };
  configFile.onRequest = callback => { hook = callback; };
  return configFile;
}

function stateStore() {
  const values = new Map();
  return { get: key => values.get(key), snapshot: () => structuredClone([...values]),
    update: async (key, value) => value === undefined ? values.delete(key) : values.set(key, value) };
}

test("discard preserves a staging file opened dirty during the native cleanup request", async t => {
  const f = fixture(t, { json: '{"extraPaths":["vendor"]}\n' });
  let temporary;
  f.configFile.onRequest((action, payload) => {
    if (action === "stage") temporary = payload.temporary;
    if (action === "verify") throw new Error("force staged cleanup");
    if (action === "verify_discard") f.vscode.workspace.textDocuments.push({ isDirty: true,
      uri: { scheme: "file", fsPath: temporary } });
  });
  await assert.rejects(updateProjectConfig(f.vscode, f.folder, "add", f.worker), /force staged cleanup/);
  assert.equal(fs.existsSync(temporary), true, "cleanup cannot delete a now-dirty editor buffer's file");
  assert.equal(f.configFile.calls.filter(call => call.action === "verify_discard").length, 1);
  assert.equal(f.configFile.calls.some(call => call.action === "discard"), false);
  assert.equal(fs.readFileSync(path.join(f.root, "pyrightconfig.json"), "utf8"), '{"extraPaths":["vendor"]}\n');
});

test("discard persists its full receipt before unlink and recovers a lost absence confirmation", async t => {
  const f = fixture(t, { json: '{"extraPaths":["vendor"]}\n' });
  const state = stateStore();
  const key = `${stateKey(f.root)}:pending-config-write`;
  let temporary;
  f.configFile.onRequest((action, payload) => {
    if (action === "stage") temporary = payload.temporary;
    if (action === "verify") throw new Error("force staged cleanup");
    if (action === "verify_discard") {
      const pending = state.get(key);
      assert.equal(pending.discarding, true);
      assert.equal(typeof pending.receipt, "string");
      assert.equal(typeof pending.token, "string");
    }
    if (action === "discard") throw new Error("absence confirmation lost");
  });
  const unlink = fs.unlinkSync;
  let removed = 0;
  t.mock.method(fs, "unlinkSync", file => {
    if (file === temporary) {
      removed += 1;
      assert.equal(state.get(key).discarding, true);
      assert.equal(f.configFile.calls.at(-1).action, "verify_discard");
    }
    return unlink(file);
  });
  await assert.rejects(createManagedProjectConfig(f.vscode, f.folder, state, f.worker), /force staged cleanup/);
  assert.equal(removed, 1);
  assert.equal(fs.existsSync(temporary), false);
  assert.equal(state.get(key).discarding, true);
  f.configFile.restart();
  f.configFile.onRequest(() => {});
  const recovered = await createManagedProjectConfig(f.vscode, f.folder, state, f.worker);
  assert.equal(state.get(key), undefined);
  assert.equal(removed, 1, "an already absent stage must not be deleted again");
  assert.ok(f.configFile.calls.some(call => call.action === "resume"));
  await recovered.release();
  recovered.cleanup();
});

test("a journal replacement during discard verification preserves the stage and replacement record", async t => {
  const f = fixture(t, { json: '{}' });
  const state = stateStore();
  const key = `${stateKey(f.root)}:pending-config-write`;
  let temporary;
  let replacement;
  f.configFile.onRequest(async (action, payload) => {
    if (action === "stage") temporary = payload.temporary;
    if (action === "verify") throw new Error("force staged cleanup");
    if (action === "verify_discard") {
      replacement = { ...state.get(key), hash: "c".repeat(64) };
      await state.update(key, replacement);
    }
  });
  await assert.rejects(createManagedProjectConfig(f.vscode, f.folder, state, f.worker), /force staged cleanup/);
  assert.ok(replacement);
  assert.equal(state.get(key), replacement);
  assert.equal(fs.existsSync(temporary), true);
  assert.equal(f.configFile.calls.some(call => call.action === "discard"), false);
});

test("recovery never deletes a file appearing at an initially absent discard path", async t => {
  const f = fixture(t, { toml: '[project]\nname="kept"\n' });
  const file = path.join(f.root, "pyproject.toml");
  const temporary = path.join(f.root, `.graphql-desk-${randomUUID()}.tmp`);
  const previousHash = createHash("sha256").update(fs.readFileSync(file)).digest("hex");
  const proof = await f.configFile(f.folder, "stage", { file: "pyproject.toml", temporary, expected_hash: previousHash, text: "owned stage" });
  const state = stateStore();
  const key = `${stateKey(f.root)}:pending-config-write`;
  await state.update(key, { version: 2, file: "pyproject.toml", temporaryPath: temporary, hash: proof.staged_hash,
    previousHash, receipt: proof.receipt, token: proof.token, discarding: true });
  fs.unlinkSync(temporary);
  const configFile = async (...args) => {
    const reply = await f.configFile(...args);
    if (args[1] === "verify_discard") fs.writeFileSync(temporary, "new foreign file");
    return reply;
  };
  await assert.rejects(createManagedProjectConfig(f.vscode, f.folder, state, f.worker, { configFile }), /appeared during cleanup/);
  assert.equal(fs.readFileSync(temporary, "utf8"), "new foreign file");
  assert.equal(state.get(key).discarding, true);
  assert.equal(f.configFile.calls.some(call => call.action === "discard"), false);
});

for (const scenario of ["legacy", "incomplete", "discarding", "cancel", "changed record", "trust loss", "foreign path", "workspace removed"]) {
  test(`explicit config recovery acknowledgement preserves files: ${scenario}`, async t => {
    const f = fixture(t, { json: '{"stubPath":"custom-stubs"}\n' });
    const file = path.join(f.root, "pyrightconfig.json");
    const temporary = path.join(f.root, `.graphql-desk-${randomUUID()}.tmp`);
    fs.writeFileSync(temporary, "uncertain preserved data");
    const state = stateStore();
    const key = `${stateKey(f.root)}:pending-config-write`;
    const pending = { version: scenario === "legacy" ? 1 : 2, file: "pyrightconfig.json", temporaryPath: temporary,
      hash: "a".repeat(64), ...(scenario === "legacy" ? {} : { previousHash: "b".repeat(64), receipt: null, token: null }) };
    if (scenario === "discarding") pending.discarding = true;
    if (scenario === "foreign path") pending.temporaryPath = path.join(path.dirname(f.root), path.basename(temporary));
    await state.update(key, pending);
    await state.update("unrelated", "preserve");
    let reset = 0;
    let dialogs = 0;
    f.vscode.window = { async showWarningMessage(_message, options, confirm) {
      dialogs += 1;
      assert.equal(options.modal, true);
      assert.ok(options.detail.includes(file));
      assert.ok(options.detail.includes(temporary));
      assert.equal(fs.readFileSync(temporary, "utf8"), "uncertain preserved data");
      if (scenario === "changed record") await state.update(key, { ...pending, hash: "c".repeat(64) });
      if (scenario === "trust loss") f.vscode.workspace.isTrusted = false;
      if (scenario === "workspace removed") f.vscode.workspace.getWorkspaceFolder = () => undefined;
      return scenario === "cancel" ? undefined : confirm;
    } };
    const action = () => acknowledgePendingConfigWrite(f.vscode, f.folder, state, () => {
      assert.equal(state.get(key), pending, "old worker is reset while the recovery intent still exists");
      reset += 1;
    });
    if (["changed record", "trust loss", "foreign path", "workspace removed"].includes(scenario)) {
      await assert.rejects(action(), /changed|record|workspace/i);
      assert.ok(state.get(key));
      assert.equal(reset, 0);
    } else {
      assert.equal(await action(), scenario !== "cancel");
      assert.equal(state.get(key) === undefined, scenario !== "cancel");
      assert.equal(reset, scenario === "cancel" ? 0 : 1);
    }
    assert.equal(dialogs, scenario === "foreign path" ? 0 : 1);
    assert.equal(state.get("unrelated"), "preserve");
    assert.equal(fs.readFileSync(file, "utf8"), '{"stubPath":"custom-stubs"}\n');
    assert.equal(fs.readFileSync(temporary, "utf8"), "uncertain preserved data");
    assert.deepEqual(f.configFile.calls, [], "acknowledgement grants no native file capability");
  });
}

test("config recovery acknowledgement serializes with recovery and preserves an unsuccessful state write", async t => {
  const f = fixture(t, { json: '{"stubPath":"custom-stubs"}\n' });
  const key = `${stateKey(f.root)}:pending-config-write`;
  const pending = { version: 2, file: "pyrightconfig.json", temporaryPath: path.join(f.root, `.graphql-desk-${randomUUID()}.tmp`),
    hash: "a".repeat(64), previousHash: "b".repeat(64), receipt: null, token: null };
  const state = stateStore();
  await state.update(key, pending);
  let release;
  const held = new Promise(resolve => { release = resolve; });
  let started;
  const shown = new Promise(resolve => { started = resolve; });
  let dialogs = 0;
  f.vscode.window = { async showWarningMessage(_message, _options, choice) { dialogs += 1; started(); await held; return choice; } };
  const action = acknowledgePendingConfigWrite(f.vscode, f.folder, state, () => {});
  t.after(() => release());
  await shown;
  await assert.rejects(acknowledgePendingConfigWrite(f.vscode, f.folder, state, () => {}), /Another/);
  await assert.rejects(createManagedProjectConfig(f.vscode, f.folder, state, f.worker), /Another/);
  assert.equal(dialogs, 1);
  assert.deepEqual(f.configFile.calls, []);
  release();
  assert.equal(await action, true);

  await state.update(key, pending);
  const failing = { get: state.get, update: async () => { throw new Error("state unavailable"); } };
  await assert.rejects(acknowledgePendingConfigWrite(f.vscode, f.folder, failing, () => {}), /state unavailable/);
  assert.equal(state.get(key), pending);
});

for (const changedRecord of [false, true]) {
  test(`automatic config recovery holds the acknowledgement lock${changedRecord ? " and preserves a replaced record" : ""}`, async t => {
    const f = fixture(t, { toml: '[project]\nname="kept"\n' });
    const file = path.join(f.root, "pyproject.toml");
    const temporary = path.join(f.root, `.graphql-desk-${randomUUID()}.tmp`);
    const originalHash = createHash("sha256").update(fs.readFileSync(file)).digest("hex");
    const proof = await f.configFile(f.folder, "stage", { file: "pyproject.toml", temporary, expected_hash: originalHash, text: "owned stage" });
    const pending = { version: 2, file: "pyproject.toml", temporaryPath: temporary, hash: proof.staged_hash,
      previousHash: originalHash, receipt: proof.receipt, token: proof.token };
    const state = stateStore();
    const key = `${stateKey(f.root)}:pending-config-write`;
    await state.update(key, pending);
    let release;
    const held = new Promise(resolve => { release = resolve; });
    let started;
    const firstStarted = new Promise(resolve => { started = resolve; });
    f.configFile.onRequest(async action => { if (action === "verify_discard") { started(); await held; } });
    let dialogs = 0;
    f.vscode.window = { async showWarningMessage() { dialogs += 1; } };
    const recovery = createManagedProjectConfig(f.vscode, f.folder, state, f.worker).then(value => ({ value }), error => ({ error }));
    t.after(() => release());
    await firstStarted;
    await assert.rejects(acknowledgePendingConfigWrite(f.vscode, f.folder, state, () => {}), /Another/);
    assert.equal(dialogs, 0);
    const replacement = { ...pending, hash: "c".repeat(64) };
    if (changedRecord) await state.update(key, replacement);
    release();
    const result = await recovery;
    if (changedRecord) {
      assert.match(result.error?.message, /record changed/);
      assert.equal(state.get(key), replacement);
    } else {
      assert.equal(result.error, undefined);
      assert.equal(result.value, null);
      assert.equal(state.get(key), undefined);
    }
    assert.equal(fs.readFileSync(file, "utf8"), '[project]\nname="kept"\n');
  });
}

test("custom stubs survive explicit execution environment paths and owned release", () => {
  const original = '{\n// user comment\n"stubPath":"custom-stubs","extraPaths":["vendor"],"executionEnvironments":[' +
    '{"root":"src","extraPaths":["environment-vendor"]},' +
    '{"root":"existing","extraPaths":["custom-stubs","keep"]},' +
    '{"root":"empty","extraPaths":[]},{"root":"inherited"}]}';
  const ownership = { expectedStubPathPresent: true, expectedStubPath: "custom-stubs",
    customStubPath: "custom-stubs", customStubPathAdded: true, executionEnvironmentRoots: ["src", "empty"],
    removeExtraPathsKey: false, originalStubPathPresent: true, originalStubPath: "custom-stubs" };
  const prepared = modifyJsonConfig(original, "prepare_stub_path", MANAGED_PATH, ownership);
  assert.deepEqual(prepared.executionEnvironments.map(item => item.extraPaths),
    [["environment-vendor", "custom-stubs"], ["custom-stubs", "keep"], ["custom-stubs"], []]);
  assert.equal(prepared.executionEnvironments[3].hasExtraPaths, false);
  assert.match(prepared.text, /\/\/ user comment/);
  const released = modifyJsonConfig(prepared.text, "release_stub_path", MANAGED_PATH,
    { ...ownership, expectedStubPath: MANAGED_PATH });
  assert.deepEqual(parseJsonConfig(released.text), parseJsonConfig(original));
  assert.match(released.text, /\/\/ user comment/);
  for (const executionEnvironmentRoots of [["src", "src"], ["missing"], ["existing"]]) {
    assert.throws(() => modifyJsonConfig(original, "prepare_stub_path", MANAGED_PATH,
      { ...ownership, executionEnvironmentRoots }), /environment|ownership/i);
  }
});

test("ownership metadata writes use a distinct temporary file purpose", async t => {
  const f = fixture(t, { json: "{}\n" });
  const ownerFile = path.join(f.root, MANAGED_PATH, "project-config-owner.json");
  const names = [];
  const rename = fs.renameSync;
  t.mock.method(fs, "renameSync", (source, destination) => {
    if (destination === ownerFile) names.push(path.basename(source));
    return rename(source, destination);
  });
  const managed = await createManagedProjectConfig(f.vscode, f.folder, stateStore(), f.worker);
  await managed.release();
  managed.cleanup();
  assert.ok(names.length > 0, "Exercise real ownership-file publication");
  for (const name of names) assert.match(name,
    /^\.graphql-desk-owner-[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}\.tmp$/i);
});

test("managed config records only added environment roots across restart and release", async t => {
  const f = fixture(t, { json: JSON.stringify({ stubPath: "custom-stubs", extraPaths: ["vendor"], executionEnvironments: [
    { root: "src", extraPaths: ["environment-vendor"] }, { root: "existing", extraPaths: ["./custom-stubs", "keep"] },
    { root: "empty", extraPaths: [] }, { root: "inherited" },
  ] }) });
  const state = stateStore();
  const first = await createManagedProjectConfig(f.vscode, f.folder, state, f.worker);
  assert.deepEqual(state.get(stateKey(f.root)).customStubPathEnvironmentRoots, ["src", "empty"]);
  const file = path.join(f.root, "pyrightconfig.json");
  const changed = JSON.parse(fs.readFileSync(file, "utf8"));
  changed.executionEnvironments[0].extraPaths.push("user-added");
  fs.writeFileSync(file, JSON.stringify(changed));
  const restarted = await createManagedProjectConfig(f.vscode, f.folder, stateStore(), f.worker);
  await restarted.release();
  restarted.cleanup();
  first.cleanup();
  const restored = parseJsonConfig(fs.readFileSync(file, "utf8"));
  assert.deepEqual(restored.extraPaths, ["vendor"]);
  assert.deepEqual(restored.executionEnvironments.map(item => item.extraPaths),
    [["environment-vendor", "user-added"], ["./custom-stubs", "keep"], [], []]);
  assert.equal(restored.executionEnvironments[2].hasExtraPaths, true);
  assert.equal(restored.executionEnvironments[3].hasExtraPaths, false);
});

test("legacy owner without environment provenance preserves config and state", async t => {
  const f = fixture(t, { json: '{"stubPath":"custom-stubs","executionEnvironments":[{"root":"src","extraPaths":["vendor"]}]}' });
  const state = stateStore();
  const managed = await createManagedProjectConfig(f.vscode, f.folder, state, f.worker);
  const file = path.join(f.root, "pyrightconfig.json");
  const original = JSON.parse(fs.readFileSync(file, "utf8"));
  original.executionEnvironments[0].extraPaths = ["vendor"];
  fs.writeFileSync(file, JSON.stringify(original));
  const legacy = { ...state.get(stateKey(f.root)) };
  delete legacy.customStubPathEnvironmentRoots;
  await state.update(stateKey(f.root), legacy);
  const before = fs.readFileSync(file, "utf8");
  const ownerFile = path.join(managed.root, "project-config-owner.json");
  const ownerBefore = fs.readFileSync(ownerFile, "utf8");
  await assert.rejects(createManagedProjectConfig(f.vscode, f.folder, state, f.worker),
    /Legacy.*restore the original config and clear GraphQL Desk ownership/);
  assert.equal(fs.readFileSync(file, "utf8"), before);
  assert.equal(fs.readFileSync(ownerFile, "utf8"), ownerBefore);
  assert.deepEqual(state.get(stateKey(f.root)), legacy);
  managed.cleanup();
});

test("v2 migration records custom roots after removing only its owned environment paths", async t => {
  const f = fixture(t, { json: JSON.stringify({ stubPath: "custom-stubs", extraPaths: ["vendor", MANAGED_PATH], executionEnvironments: [
    { root: "src", extraPaths: ["environment-vendor", MANAGED_PATH] }, { root: "inherited", extraPaths: [MANAGED_PATH] },
  ] }) });
  const state = stateStore();
  await state.update(stateKey(f.root), { version: 2, file: "pyrightconfig.json", format: "jsonc", path: MANAGED_PATH,
    ownership: { topLevel: false, executionEnvironmentRoots: ["inherited"] } });
  const managed = await createManagedProjectConfig(f.vscode, f.folder, state, f.worker);
  assert.deepEqual(state.get(stateKey(f.root)).customStubPathEnvironmentRoots, ["src"]);
  const file = path.join(f.root, "pyrightconfig.json");
  const active = parseJsonConfig(fs.readFileSync(file, "utf8"));
  assert.deepEqual(active.executionEnvironments[0].extraPaths, ["environment-vendor", "custom-stubs"]);
  assert.equal(active.executionEnvironments[1].hasExtraPaths, false);
  await managed.release();
  managed.cleanup();
  const restored = parseJsonConfig(fs.readFileSync(file, "utf8"));
  assert.deepEqual(restored.extraPaths, ["vendor"]);
  assert.deepEqual(restored.executionEnvironments[0].extraPaths, ["environment-vendor"]);
  assert.equal(restored.executionEnvironments[1].hasExtraPaths, false);
});

test("release preserves config and owner state when an added environment path changes", async t => {
  const f = fixture(t, { json: '{"stubPath":"custom-stubs","executionEnvironments":[{"root":"src","extraPaths":["vendor"]}]}' });
  const state = stateStore();
  const managed = await createManagedProjectConfig(f.vscode, f.folder, state, f.worker);
  const file = path.join(f.root, "pyrightconfig.json");
  const changed = JSON.parse(fs.readFileSync(file, "utf8"));
  changed.executionEnvironments[0].extraPaths = ["vendor", "user-path"];
  fs.writeFileSync(file, JSON.stringify(changed));
  const before = fs.readFileSync(file, "utf8");
  const owner = state.get(stateKey(f.root));
  await assert.rejects(managed.release(), /ownership changed/);
  assert.equal(fs.readFileSync(file, "utf8"), before);
  assert.deepEqual(state.get(stateKey(f.root)), owner);
  managed.cleanup();
});

test("real Pyright retains custom stub typing through an explicit environment", async t => {
  const f = fixture(t, { json: JSON.stringify({ stubPath: "custom-stubs", extraPaths: ["vendor"],
    executionEnvironments: [{ root: ".", extraPaths: ["environment-vendor"] }], typeCheckingMode: "strict" }) });
  fs.mkdirSync(path.join(f.root, "custom-stubs"));
  fs.mkdirSync(path.join(f.root, "environment-vendor"));
  fs.writeFileSync(path.join(f.root, "custom-stubs", "legacy.pyi"), "def existing() -> str: ...\n");
  fs.writeFileSync(path.join(f.root, "environment-vendor", "legacy.py"), 'def existing() -> object:\n    return "runtime"\n');
  fs.writeFileSync(path.join(f.root, "consumer.py"), "from legacy import existing\nvalue: str = existing()\n");
  const python = process.env.GRAPHQL_DESK_PYTHON ?? path.join(path.resolve(__dirname, "../.."), ".venv",
    process.platform === "win32" ? "Scripts/python.exe" : "bin/python");
  const check = () => {
    const result = spawnSync(python, ["-m", "pyright", "--pythonpath", python, "--outputjson", path.join(f.root, "consumer.py")],
      { cwd: f.root, encoding: "utf8" });
    assert.equal(result.error, undefined);
    const parsed = JSON.parse(result.stdout);
    assert.equal(parsed.summary.errorCount, 0, JSON.stringify(parsed.generalDiagnostics));
    assert.equal(result.status, 0);
  };
  check();
  const managed = await createManagedProjectConfig(f.vscode, f.folder, stateStore(), f.worker);
  assert.deepEqual(parseJsonConfig(fs.readFileSync(path.join(f.root, "pyrightconfig.json"), "utf8")).executionEnvironments[0].extraPaths,
    ["environment-vendor", "custom-stubs"]);
  check();
  await managed.release();
  managed.cleanup();
  check();
});

function fixture(t, { json, toml, dirty = false, pythonStubPath: initialPythonStubPath, inheritedPythonStubPath, globalPythonStubPath,
  workspaceSerialized = false, standaloneAliases = false } = {}) {
  const root = fs.mkdtempSync(path.join(require("node:os").tmpdir(), "pyright-config-"));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const folder = { uri: { scheme: "file", fsPath: root, toString: () => `file://${root.replaceAll("\\", "/")}` } };
  if (json !== undefined) fs.writeFileSync(path.join(root, "pyrightconfig.json"), json);
  if (toml !== undefined) fs.writeFileSync(path.join(root, "pyproject.toml"), toml);
  let pythonStubPath = initialPythonStubPath;
  const configurationListeners = new Set();
  let onPythonSettingChange;
  const pythonConfiguration = {
    get: (_name, fallback) => pythonStubPath ?? inheritedPythonStubPath ?? globalPythonStubPath ?? fallback,
    inspect: () => workspaceSerialized ? { workspaceValue: pythonStubPath ?? inheritedPythonStubPath, globalValue: globalPythonStubPath } :
      { workspaceFolderValue: pythonStubPath, workspaceValue: standaloneAliases ? pythonStubPath : inheritedPythonStubPath, globalValue: globalPythonStubPath },
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
  const configFile = configFileFixture(vscode, folder, root);
  const journal = [];
  fixtures.set(vscode, { configFile, journal });
  return { root, folder, vscode, worker, configFile, journal, get pythonStubPath() { return pythonStubPath; },
    setInheritedPythonStubPath(value) { inheritedPythonStubPath = value; },
    setGlobalPythonStubPath(value) { globalPythonStubPath = value; },
    onPythonSettingChange(callback) { onPythonSettingChange = callback; } };
}

test("project owner advances both standalone aliases through setup, rotation and restoration", async t => {
  const original = '{"stubPath":"project-user-stubs"}';
  const f = fixture(t, { json: original, standaloneAliases: true, globalPythonStubPath: "global-user-stubs" });
  const state = stateStore();
  const managed = await createManagedProjectConfig(f.vscode, f.folder, state, f.worker);
  assert.equal(managed.routingSettingsCurrent(), true, "Both native views of the same standalone setting are owned");
  const generation = path.join(managed.root, "generations", `g-1-${randomUUID()}`);
  fs.mkdirSync(generation, { recursive: true });
  const verification = { directory: generation, manifest: { owner: "graphql-desk-analysis-generation", format: 2,
    id: path.basename(generation), artifactFingerprint: "a" }, manifestFingerprint: "b".repeat(64), verify() { return this; } };
  assert.equal(await managed.recordGeneration(generation, verification.manifestFingerprint), true);
  assert.equal(await managed.selectGeneration(generation, () => true, verification), true);
  assert.equal(managed.routingSettingsCurrent(), true);
  assert.equal(f.vscode.workspace.getConfiguration().inspect().globalValue, "global-user-stubs");
  await managed.release();
  assert.equal(f.pythonStubPath, undefined);
  assert.equal(f.vscode.workspace.getConfiguration().get("analysis.stubPath"), "global-user-stubs");
  assert.deepEqual(JSON.parse(fs.readFileSync(path.join(f.root, "pyrightconfig.json"), "utf8")), JSON.parse(original));
});

for (const scheme of ["file", "untitled"]) {
  test(`project owner does not merge independent same-target layers in a ${scheme} workspace`, async t => {
    const f = fixture(t, { json: "{}" });
    f.vscode.workspace.workspaceFile = { scheme, fsPath: path.join(f.root, "project.code-workspace") };
    f.onPythonSettingChange(value => f.setInheritedPythonStubPath(value));
    const state = stateStore();
    const managed = await createManagedProjectConfig(f.vscode, f.folder, state, f.worker);
    assert.equal(managed.routingSettingsCurrent(), false, "An independent concurrent Workspace choice must still require routing");
    const prior = state.get(stateKey(f.root)).pythonStubPath.snapshot;
    assert.equal(prior.singleFolderDirectory, false, "An untitled workspace file also gives Workspace its own settings layer");
    assert.equal(prior.workspacePresent, false, "The owned update cannot adopt the concurrent Workspace choice as its prior");
  });
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

test("owner record replacement retries the same verified stage after transient locks", async t => {
  const f = fixture(t, { json: '{"stubPath":"custom-stubs"}' });
  const rename = fs.renameSync;
  let first;
  let attempts = 0;
  t.mock.method(fs, "renameSync", (source, destination) => {
    if (path.basename(destination) === "project-config-owner.json") {
      first ??= source;
      if (source === first && ++attempts <= 2) throw Object.assign(new Error("locked"), { code: "EPERM" });
    }
    return rename(source, destination);
  });
  const managed = await createManagedProjectConfig(f.vscode, f.folder, stateStore(), f.worker);
  assert.equal(attempts, 3);
  assert.equal(managed.stillOwnsSetting(), true);
  assert.equal(fs.readdirSync(managed.root).some(name => name.endsWith(".tmp")), false);
});

for (const code of ["EPERM", "EACCES", "EBUSY", "ENOSPC"]) {
  test(`owner record replacement bounds ${code} failures without leaving a verified temporary`, async t => {
    const original = '{"stubPath":"custom-stubs"}';
    const f = fixture(t, { json: original });
    const rename = fs.renameSync;
    const stages = new Set();
    let attempts = 0;
    t.mock.method(fs, "renameSync", (source, destination) => {
      if (path.basename(destination) !== "project-config-owner.json") return rename(source, destination);
      stages.add(source);
      attempts += 1;
      throw Object.assign(new Error("replacement failed"), { code });
    });
    await assert.rejects(createManagedProjectConfig(f.vscode, f.folder, stateStore(), f.worker), { code });
    assert.equal(attempts, code === "ENOSPC" ? 1 : 4);
    assert.equal(stages.size, 1);
    assert.equal(fs.readFileSync(path.join(f.root, "pyrightconfig.json"), "utf8"), original);
    assert.deepEqual(fs.readdirSync(path.join(f.root, MANAGED_PATH)), []);
  });
}

for (const change of ["lifecycle", "trust", "folder", "config", "python", "hidden-python", "dirty-config", "dirty-settings", "dirty-owner", "dirty-stage"]) {
  test(`owner replacement rechecks ${change} after a lock wait`, async t => {
    const original = '{"stubPath":"custom-stubs"}';
    const f = fixture(t, { json: original });
    const file = path.join(f.root, "pyrightconfig.json");
    const rename = fs.renameSync;
    let current = true;
    let attempts = 0;
    let stage;
    t.mock.method(fs, "renameSync", (source, destination) => {
      if (path.basename(destination) !== "project-config-owner.json") return rename(source, destination);
      attempts += 1;
      stage = source;
      setTimeout(() => {
        if (change === "lifecycle") current = false;
        else if (change === "trust") f.vscode.workspace.isTrusted = false;
        else if (change === "folder") f.vscode.workspace.getWorkspaceFolder = () => undefined;
        else if (change === "config") fs.appendFileSync(file, "\n// user edit\n");
        else if (change === "python") void f.vscode.workspace.getConfiguration().update("analysis.stubPath", "user-stubs");
        else if (change === "hidden-python") f.setGlobalPythonStubPath("new-global-stubs");
        else f.vscode.workspace.textDocuments.push({ isDirty: true, uri: { scheme: "file", fsPath:
          change === "dirty-config" ? file : change === "dirty-settings" ? path.join(f.root, ".vscode", "settings.json") :
            change === "dirty-owner" ? destination : source } });
      }, 0);
      throw Object.assign(new Error("locked"), { code: "EPERM" });
    });
    await assert.rejects(createManagedProjectConfig(f.vscode, f.folder, stateStore(), f.worker, { ownerCurrent: () => current }),
      /changed|Dirty|dirty/);
    assert.equal(attempts, 1, "a rejected guard cannot make a second replacement attempt");
    assert.equal(fs.existsSync(path.join(f.root, MANAGED_PATH, "project-config-owner.json")), false);
    assert.equal(fs.existsSync(stage), ["trust", "folder", "dirty-stage"].includes(change),
      "only a still-verifiable clean temporary may be removed");
    assert.equal(fs.readFileSync(file, "utf8"), change === "config" ? `${original}\n// user edit\n` : original);
  });
}

test("owner replacement preserves a same-byte foreign stage and refuses another allocation on restart", async t => {
  const f = fixture(t, { json: '{"stubPath":"custom-stubs"}' });
  const rename = fs.renameSync;
  let attempts = 0;
  let stage;
  let foreignIdentity;
  t.mock.method(fs, "renameSync", (source, destination) => {
    if (path.basename(destination) !== "project-config-owner.json") return rename(source, destination);
    stage = source;
    attempts += 1;
    const bytes = fs.readFileSync(source);
    rename(source, `${source}.held`);
    fs.writeFileSync(source, bytes, { flag: "wx" });
    foreignIdentity = fs.statSync(source, { bigint: true });
    throw Object.assign(new Error("locked"), { code: "EPERM" });
  });
  await assert.rejects(createManagedProjectConfig(f.vscode, f.folder, stateStore(), f.worker), /staging file changed.*staging preserved/);
  assert.equal(attempts, 1);
  assert.equal(fs.statSync(stage, { bigint: true }).ino, foreignIdentity.ino);
  const before = fs.readdirSync(path.dirname(stage));
  await assert.rejects(createManagedProjectConfig(f.vscode, f.folder, stateStore(), f.worker), /Unverified.*temporary/);
  assert.deepEqual(fs.readdirSync(path.dirname(stage)), before);
  assert.equal(attempts, 1);
});

test("cold owner preflight bounds directory enumeration before allocating a stage", async t => {
  const f = fixture(t, { json: "{}" });
  const root = path.join(f.root, MANAGED_PATH);
  fs.mkdirSync(root, { recursive: true });
  fs.writeFileSync(path.join(root, "user.txt"), "preserve me");
  const opendir = fs.opendirSync;
  const open = fs.openSync;
  let reads = 0, closes = 0, allocations = 0;
  t.mock.method(fs, "opendirSync", (directory, ...args) => directory === root ? {
    readSync() { return ++reads <= 5000 ? { name: `foreign-${reads}.txt` } : null; },
    closeSync() { closes += 1; },
  } : opendir(directory, ...args));
  t.mock.method(fs, "openSync", (file, flags, ...args) => {
    if (flags === "wx" && path.basename(file).startsWith(".graphql-desk-owner-")) allocations += 1;
    return open(file, flags, ...args);
  });
  await assert.rejects(createManagedProjectConfig(f.vscode, f.folder, stateStore(), f.worker), /owner directory exceeds 4096 entries/);
  assert.equal(reads, 4097, "one lookahead establishes overflow without enumerating the remaining entries");
  assert.equal(closes, 1);
  assert.equal(allocations, 0);
  assert.deepEqual(fs.readdirSync(root), ["user.txt"]);
  assert.equal(fs.readFileSync(path.join(root, "user.txt"), "utf8"), "preserve me");
});

test("owner replacement rechecks the original native identity during a wait", async t => {
  const f = fixture(t, { json: '{"stubPath":"custom-stubs"}' });
  const rename = fs.renameSync;
  let attempts = 0;
  let foreign;
  let stage;
  t.mock.method(fs, "renameSync", (source, destination) => {
    if (path.basename(destination) !== "project-config-owner.json" || !fs.existsSync(destination)) return rename(source, destination);
    stage = source;
    attempts += 1;
    const bytes = fs.readFileSync(destination);
    rename(destination, `${destination}.held`);
    fs.writeFileSync(destination, bytes, { flag: "wx" });
    foreign = fs.statSync(destination, { bigint: true });
    throw Object.assign(new Error("locked"), { code: "EBUSY" });
  });
  await assert.rejects(createManagedProjectConfig(f.vscode, f.folder, stateStore(), f.worker), /owner record changed/);
  assert.equal(attempts, 1);
  assert.equal(fs.statSync(path.join(f.root, MANAGED_PATH, "project-config-owner.json"), { bigint: true }).ino, foreign.ino);
  assert.equal(fs.existsSync(stage), false, "an unchanged clean stage can still be removed after the original changes");
});

test("owner replacement preserves a changed durable ownership record during backoff", async t => {
  const f = fixture(t, { json: "{}" });
  const state = stateStore();
  const foreign = { owner: "another operation" };
  const rename = fs.renameSync;
  let attempts = 0;
  t.mock.method(fs, "renameSync", (source, destination) => {
    if (path.basename(destination) !== "project-config-owner.json") return rename(source, destination);
    if (++attempts === 1) {
      setTimeout(() => { void state.update(stateKey(f.root), foreign); }, 0);
      throw Object.assign(new Error("locked"), { code: "EPERM" });
    }
    return rename(source, destination);
  });
  await assert.rejects(createManagedProjectConfig(f.vscode, f.folder, state, f.worker), /owner state changed/);
  assert.equal(attempts, 1);
  assert.deepEqual(state.get(stateKey(f.root)), foreign);
  assert.equal(fs.existsSync(path.join(f.root, MANAGED_PATH, "project-config-owner.json")), false);
  assert.deepEqual(fs.readdirSync(path.join(f.root, MANAGED_PATH)), []);
});

test("owner replacement refuses a changed directory while preserving both namespaces", async t => {
  const f = fixture(t, { json: "{}" });
  const root = path.join(f.root, MANAGED_PATH);
  const rename = fs.renameSync;
  let attempts = 0;
  let stage;
  t.mock.method(fs, "renameSync", (source, destination) => {
    if (path.basename(destination) !== "project-config-owner.json") return rename(source, destination);
    stage = path.basename(source);
    attempts += 1;
    rename(root, `${root}.held`);
    fs.mkdirSync(root);
    fs.writeFileSync(path.join(root, "foreign.txt"), "user data");
    throw Object.assign(new Error("locked"), { code: "EACCES" });
  });
  await assert.rejects(createManagedProjectConfig(f.vscode, f.folder, stateStore(), f.worker), /owner directory changed.*staging preserved/);
  assert.equal(attempts, 1);
  assert.deepEqual(fs.readdirSync(root), ["foreign.txt"]);
  assert.equal(fs.existsSync(path.join(`${root}.held`, stage)), true);
});

test("owner staging distinguishes adjacent native IDs above JavaScript number precision", async t => {
  const f = fixture(t, { json: "{}" });
  const huge = 2n ** 100n;
  const open = fs.openSync, close = fs.closeSync, fstat = fs.fstatSync, lstat = fs.lstatSync, rename = fs.renameSync;
  let stage, descriptor;
  let replaced = false;
  t.mock.method(fs, "openSync", (file, ...args) => {
    const result = open(file, ...args);
    if (path.basename(file).startsWith(".graphql-desk-owner-")) { stage = file; descriptor = result; }
    return result;
  });
  t.mock.method(fs, "fstatSync", (fd, options) => {
    const stat = fstat(fd, options);
    if (fd === descriptor && options?.bigint) stat.ino = huge;
    return stat;
  });
  t.mock.method(fs, "closeSync", fd => { if (fd === descriptor) descriptor = undefined; return close(fd); });
  t.mock.method(fs, "lstatSync", (file, options) => {
    const stat = lstat(file, options);
    if (file === stage && options?.bigint) stat.ino = huge + (replaced ? 1n : 0n);
    return stat;
  });
  t.mock.method(fs, "renameSync", (source, destination) => {
    if (path.basename(destination) !== "project-config-owner.json") return rename(source, destination);
    replaced = true;
    throw Object.assign(new Error("locked"), { code: "EPERM" });
  });
  await assert.rejects(createManagedProjectConfig(f.vscode, f.folder, stateStore(), f.worker), /staging file changed.*staging preserved/);
  assert.equal(Number(huge), Number(huge + 1n), "the fixture really exceeds number precision");
  assert.equal(fs.existsSync(stage), true);
});

test("superseded generation cannot replace an owner record after a backoff", async t => {
  const f = fixture(t, { json: '{"stubPath":"custom-stubs"}' });
  const managed = await createManagedProjectConfig(f.vscode, f.folder, stateStore(), f.worker);
  const original = fs.readFileSync(path.join(f.root, "pyrightconfig.json"));
  const owner = fs.readFileSync(path.join(managed.root, "project-config-owner.json"));
  const generation = path.join(managed.root, "generations", `g-1-${randomUUID()}`);
  fs.mkdirSync(generation, { recursive: true });
  const verification = { directory: generation, manifest: { owner: "graphql-desk-analysis-generation", format: 2,
    id: path.basename(generation), artifactFingerprint: "a" }, manifestFingerprint: "b".repeat(64), verify() { return this; } };
  assert.equal(await managed.recordGeneration(generation, verification.manifestFingerprint), true);
  const rename = fs.renameSync;
  let current = true;
  let attempts = 0;
  t.mock.method(fs, "renameSync", (source, destination) => {
    if (path.basename(destination) !== "project-config-owner.json") return rename(source, destination);
    attempts += 1;
    setTimeout(() => { current = false; }, 0);
    throw Object.assign(new Error("locked"), { code: "EBUSY" });
  });
  await assert.rejects(managed.selectGeneration(generation, () => current, verification), /phase or Python settings changed/);
  assert.equal(attempts, 1);
  assert.deepEqual(fs.readFileSync(path.join(f.root, "pyrightconfig.json")), original);
  assert.deepEqual(fs.readFileSync(path.join(managed.root, "project-config-owner.json")), owner);
  assert.equal(f.pythonStubPath, MANAGED_PATH);
  assert.equal(fs.readdirSync(managed.root).some(name => name.endsWith(".tmp")), false);
});

test("uncertain owner cleanup blocks new stages until its exact file is clean again", async t => {
  const original = '{"stubPath":"custom-stubs"}';
  const f = fixture(t, { json: original });
  const managed = await createManagedProjectConfig(f.vscode, f.folder, stateStore(), f.worker);
  const rename = fs.renameSync;
  const open = fs.openSync;
  let stage;
  let allocations = 0;
  t.mock.method(fs, "openSync", (file, flags, ...args) => {
    if (flags === "wx" && path.basename(file).startsWith(".graphql-desk-owner-")) allocations += 1;
    return open(file, flags, ...args);
  });
  t.mock.method(fs, "renameSync", (source, destination) => {
    if (!stage && path.basename(destination) === "project-config-owner.json") {
      stage = source;
      f.vscode.workspace.textDocuments.push({ isDirty: true, uri: { scheme: "file", fsPath: source } });
      throw Object.assign(new Error("locked"), { code: "EBUSY" });
    }
    return rename(source, destination);
  });
  await assert.rejects(managed.release(), /Dirty.*staging preserved/);
  const identity = fs.statSync(stage, { bigint: true });
  await assert.rejects(managed.release(), /Dirty/);
  assert.equal(allocations, 1, "retrying an unresolved stage cannot allocate another one");
  assert.equal(fs.statSync(stage, { bigint: true }).ino, identity.ino);
  f.vscode.workspace.textDocuments.length = 0;
  await managed.release();
  assert.equal(allocations, 2, "the exact old stage is safely removed before one new attempt");
  assert.equal(fs.existsSync(stage), false);
  assert.equal(fs.readdirSync(managed.root).some(name => name.endsWith(".tmp")), false);
  assert.deepEqual(JSON.parse(fs.readFileSync(path.join(f.root, "pyrightconfig.json"), "utf8")), JSON.parse(original));
});

test("owner replacement requires an explicit lifecycle guard before setup", async t => {
  const f = fixture(t, { json: "{}" });
  await assert.rejects(rawCreateManagedProjectConfig(f.vscode, f.folder, stateStore(), f.worker, { configFile: f.configFile }),
    /freshness guard/);
  assert.deepEqual(fs.readdirSync(f.root), ["pyrightconfig.json"]);
});

function activateConfigExtension(t, f, state) {
  const Module = require("node:module");
  const filename = require.resolve("../src/extension");
  const cached = require.cache[filename];
  delete require.cache[filename];
  const lines = [];
  let controllerDisposed = false;
  let reachedArtifacts;
  const ready = new Promise(resolve => { reachedArtifacts = resolve; });
  const disposable = () => ({ dispose() {} });
  const uri = fsPath => ({ scheme: "file", fsPath, toString: () => require("node:url").pathToFileURL(fsPath).href });
  f.vscode.Uri = { file: uri, parse: value => uri(require("node:url").fileURLToPath(value)) };
  f.folder.uri = uri(f.root);
  f.vscode.workspace.onDidChangeWorkspaceFolders = disposable;
  f.vscode.languages = { createDiagnosticCollection: () => ({ ...disposable(), delete() {}, clear() {}, set() {} }) };
  f.vscode.commands = { registerCommand: disposable, executeCommand: async () => {} };
  f.vscode.StatusBarAlignment = { Right: 1 };
  f.vscode.CancellationTokenSource = class { token = {}; cancel() { this.token.isCancellationRequested = true; } dispose() {} };
  const document = { uri: uri(path.join(f.root, "example.py")), languageId: "python", getText: () => "" };
  f.vscode.window = { visibleTextEditors: [{ document }],
    createOutputChannel: () => ({ appendLine: line => lines.push(line), dispose() {} }),
    createStatusBarItem: () => ({ ...disposable(), show() {}, hide() {} }), onDidChangeVisibleTextEditors: disposable };
  const controller = {
    isTrusted: () => f.vscode.workspace.isTrusted,
    editPyrightConfig: f.worker, configFile: f.configFile,
    trackOwnedProjectConfigWrite: () => true, markOwnedProjectConfig() {}, setManagedProjectStubPath() {},
    snapshot: () => ({}), artifacts: async () => { reachedArtifacts(); return null; },
    dispose() { controllerDisposed = true; },
  };
  const provider = () => ({ ...disposable(), close() {}, refresh: async () => {} });
  const load = Module._load;
  const mocked = t.mock.method(Module, "_load", function(request, ...args) {
    if (request === "vscode") return f.vscode;
    if (args[0]?.filename === filename) {
      if (request === "./analysisController") return { createAnalysisController: () => controller };
      if (request === "./languageFeatures") return { registerLanguageFeatures: provider };
      if (request === "./highlighting") return { registerHighlighting: provider };
      if (request === "./renameLanguageClient") return { createRenameLanguageClient: () => ({ start: async () => {}, stop: async () => {} }) };
      if (request === "./analysisGenerations") return { createAnalysisGenerations: () => ({ current: () => [], publish: async () => false }) };
    }
    return load.call(this, request, ...args);
  });
  const extension = require(filename);
  extension.activate({ subscriptions: [], workspaceState: state });
  mocked.mock.restore();
  t.after(async () => {
    await extension.deactivate();
    require.cache[filename] = cached;
  });
  return { ...extension, ready, lines, get controllerDisposed() { return controllerDisposed; } };
}

test("actual extension deactivation restores project settings through an owner retry", async t => {
  const original = '{"stubPath":"custom-stubs"}';
  const f = fixture(t, { json: original });
  const state = stateStore();
  const extension = activateConfigExtension(t, f, state);
  await extension.ready;
  const rename = fs.renameSync;
  let attempts = 0;
  let stage;
  t.mock.method(fs, "renameSync", (source, destination) => {
    if (path.basename(destination) === "project-config-owner.json" && JSON.parse(fs.readFileSync(source)).phase === "release") {
      assert.equal(extension.controllerDisposed, false, "the native config backend stays alive through restoration");
      stage ??= source;
      assert.equal(source, stage);
      if (++attempts === 1) throw Object.assign(new Error("locked during teardown"), { code: "EPERM" });
    }
    return rename(source, destination);
  });
  await extension.deactivate();
  assert.equal(attempts, 2);
  assert.deepEqual(JSON.parse(fs.readFileSync(path.join(f.root, "pyrightconfig.json"), "utf8")), JSON.parse(original));
  assert.equal(f.pythonStubPath, undefined);
  assert.equal(state.get(stateKey(f.root)), undefined);
  assert.equal(extension.controllerDisposed, true);
  assert.equal(extension.lines.some(line => line.includes("cleanup failed")), false);
  assert.equal(fs.existsSync(stage), false);
});

test("actual extension disposal during an owner wait rejects unfinished setup", async t => {
  const original = '{"stubPath":"custom-stubs"}';
  const f = fixture(t, { json: original });
  const rename = fs.renameSync;
  let entered;
  const waiting = new Promise(resolve => { entered = resolve; });
  let attempts = 0;
  let stage;
  t.mock.method(fs, "renameSync", (source, destination) => {
    if (path.basename(destination) !== "project-config-owner.json") return rename(source, destination);
    stage = source;
    attempts += 1;
    entered();
    throw Object.assign(new Error("locked during setup"), { code: "EPERM" });
  });
  const state = stateStore();
  const extension = activateConfigExtension(t, f, state);
  await waiting;
  await extension.deactivate();
  assert.equal(attempts, 1);
  assert.equal(fs.readFileSync(path.join(f.root, "pyrightconfig.json"), "utf8"), original);
  assert.equal(f.pythonStubPath, undefined);
  assert.equal(state.get(stateKey(f.root)), undefined);
  assert.equal(fs.existsSync(stage), false);
  assert.equal(extension.lines.some(line => line.includes("phase or Python settings changed")), true);
});

test("config replacement retries a transient Windows lock without changing write ownership", async t => {
  const original = '{"extraPaths":["vendor"],"stubPath":"stubs"}\n';
  const f = fixture(t, { json: original });
  const file = path.join(f.root, "pyrightconfig.json");
  const rename = fs.renameSync;
  let attempts = 0;
  t.mock.method(fs, "renameSync", (source, destination) => {
    if (destination === file && attempts++ === 0) throw Object.assign(new Error("locked"), { code: "EPERM" });
    return rename(source, destination);
  });
  const phases = [];
  const result = await updateProjectConfig(f.vscode, f.folder, "add", f.worker, {}, MANAGED_PATH,
    (_file, _hash, detail) => { phases.push(detail.phase); });
  assert.deepEqual(result.extraPaths, ["vendor", MANAGED_PATH]);
  assert.equal(attempts, 2);
  assert.deepEqual(phases, ["begin", "commit"]);
  assert.deepEqual(fs.readdirSync(f.root), ["pyrightconfig.json"]);
});

test("config replacement stops after the bounded Windows-lock retries", async t => {
  const original = '{"extraPaths":["vendor"]}\n';
  const f = fixture(t, { json: original });
  const file = path.join(f.root, "pyrightconfig.json");
  const rename = fs.renameSync;
  let attempts = 0;
  t.mock.method(fs, "renameSync", (source, destination) => {
    if (destination !== file) return rename(source, destination);
    attempts += 1;
    throw Object.assign(new Error("locked"), { code: "EPERM" });
  });
  await assert.rejects(updateProjectConfig(f.vscode, f.folder, "add", f.worker), { code: "EPERM" });
  assert.equal(attempts, 4);
  assert.equal(fs.readFileSync(file, "utf8"), original);
  assert.deepEqual(fs.readdirSync(f.root), ["pyrightconfig.json"]);
  assert.equal(f.configFile.calls.filter(call => call.action === "verify").length, 4);
});

test("mutating paths require a permission backend and durable journal", async t => {
  const original = '{"extraPaths":["vendor"]}';
  const f = fixture(t, { json: original });
  await assert.rejects(rawUpdateProjectConfig(f.vscode, f.folder, "add", f.worker, {}, MANAGED_PATH,
    async () => true), /Permission-preserving/);
  await assert.rejects(rawUpdateProjectConfig(f.vscode, f.folder, "add", f.worker, {}, MANAGED_PATH,
    undefined, f.configFile), /durable journal/);
  const inspected = await rawUpdateProjectConfig(f.vscode, f.folder, "inspect", f.worker);
  assert.equal(typeof inspected.identity.ino, "bigint");
  assert.equal(typeof inspected.identity.dev, "bigint");
  assert.equal(f.configFile.calls.length, 0);
  assert.equal(fs.readFileSync(inspected.file, "utf8"), original);
  assert.deepEqual(fs.readdirSync(f.root), ["pyrightconfig.json"]);
});

test("malformed UTF-8 is preserved before intent or staging", async t => {
  const f = fixture(t, { json: "{}" });
  const file = path.join(f.root, "pyrightconfig.json");
  const original = Buffer.from('{"private":"user\xff"}', "latin1");
  fs.writeFileSync(file, original);
  await assert.rejects(updateProjectConfig(f.vscode, f.folder, "add", f.worker), /Invalid UTF-8/);
  assert.deepEqual(fs.readFileSync(file), original);
  assert.equal(f.configFile.calls.length, 0);
  assert.equal(f.journal.length, 0);
});

test("intent precedes stage and its permission receipt is durable before every rename", async t => {
  const f = fixture(t, { json: '{}' });
  const state = stateStore();
  const pendingKey = `${stateKey(f.root)}:pending-config-write`;
  const file = path.join(f.root, "pyrightconfig.json");
  f.configFile.onRequest(action => {
    const pending = state.get(pendingKey);
    if (action === "stage") { assert.equal(pending.version, 2); assert.equal(pending.receipt, null); }
    if (action === "verify") { assert.equal(typeof pending.receipt, "string"); assert.equal(typeof pending.token, "string"); }
  });
  const rename = fs.renameSync;
  let commits = 0;
  t.mock.method(fs, "renameSync", (source, destination) => {
    if (destination === file) {
      commits += 1;
      assert.equal(typeof state.get(pendingKey).receipt, "string");
      assert.equal(f.configFile.calls.at(-1).action, "verify");
    }
    return rename(source, destination);
  });
  const managed = await createManagedProjectConfig(f.vscode, f.folder, state, f.worker);
  assert.equal(state.get(pendingKey), undefined);
  await managed.release();
  managed.cleanup();
  assert.equal(state.get(pendingKey), undefined);
  assert.ok(commits >= 2);
});

for (const race of ["dirty", "trust", "source", "stage", "generation"]) {
  test(`the final synchronous guard preserves ${race} changes after permission verification`, async t => {
    const original = '{"extraPaths":["vendor"]}';
    const f = fixture(t, { json: original });
    const file = path.join(f.root, "pyrightconfig.json");
    let current = true;
    const configFile = async (...args) => {
      const reply = await f.configFile(...args);
      if (args[1] === "verify") {
        if (race === "dirty") f.vscode.workspace.textDocuments.push({ isDirty: true, uri: f.vscode.Uri.file(file) });
        else if (race === "trust") f.vscode.workspace.isTrusted = false;
        else if (race === "generation") current = false;
        else {
          const target = race === "source" ? file : f.configFile.calls.find(call => call.action === "stage").temporary;
          const replacement = path.join(f.root, "foreign-replacement");
          fs.writeFileSync(replacement, fs.readFileSync(target));
          fs.renameSync(replacement, target);
        }
      }
      return reply;
    };
    let commits = 0;
    const rename = fs.renameSync;
    t.mock.method(fs, "renameSync", (source, destination) => {
      if (destination === file && path.basename(source).startsWith(".graphql-desk-")) commits += 1;
      return rename(source, destination);
    });
    await assert.rejects(rawUpdateProjectConfig(f.vscode, f.folder, "add", f.worker, {}, MANAGED_PATH,
      async () => true, configFile, () => current), /Dirty|Trusted|changed|stale/);
    assert.equal(commits, 0);
    assert.equal(fs.readFileSync(file, "utf8"), original);
    assert.equal(f.configFile.calls.some(call => call.action === "finish"), false);
  });
}

test("a failed receipt save preserves the stage and intent before any cleanup", async t => {
  const original = '{"extraPaths":["vendor"]}';
  const f = fixture(t, { json: original });
  const state = stateStore();
  const update = state.update;
  const pendingKey = `${stateKey(f.root)}:pending-config-write`;
  state.update = async (key, value) => {
    if (key === pendingKey && value?.receipt) throw new Error("receipt storage unavailable");
    return update(key, value);
  };
  await assert.rejects(createManagedProjectConfig(f.vscode, f.folder, state, f.worker), /storage unavailable/);
  assert.equal(fs.readFileSync(path.join(f.root, "pyrightconfig.json"), "utf8"), original);
  assert.deepEqual(f.configFile.calls.map(call => call.action), ["stage"]);
  assert.equal(state.get(pendingKey).receipt, null);
  assert.equal(fs.existsSync(state.get(pendingKey).temporaryPath), true);
});

test("lost staging replies retain incomplete intent and prevent new stage creation", async t => {
  const original = '{"extraPaths":["vendor"]}';
  const f = fixture(t, { json: original });
  const state = stateStore();
  const configFile = async (...args) => {
    await f.configFile(...args);
    throw new Error("stage reply lost");
  };
  await assert.rejects(createManagedProjectConfig(f.vscode, f.folder, state, f.worker, { configFile }), /reply lost/);
  const pending = state.get(`${stateKey(f.root)}:pending-config-write`);
  assert.equal(pending.version, 2);
  assert.equal(pending.receipt, null);
  assert.equal(fs.existsSync(pending.temporaryPath), true);
  f.configFile.restart();
  await assert.rejects(createManagedProjectConfig(f.vscode, f.folder, state, f.worker), /manual recovery/);
  assert.equal(f.configFile.calls.filter(call => call.action === "stage").length, 1);
  assert.equal(fs.readFileSync(path.join(f.root, "pyrightconfig.json"), "utf8"), original);
});

test("freshness failure before native staging clears only the unused intent", async t => {
  const f = fixture(t, { json: '{"extraPaths":["vendor"]}' });
  const file = path.join(f.root, "pyrightconfig.json");
  const userText = '{"extraPaths":["user-added"]}';
  const state = stateStore();
  const update = state.update;
  const pendingKey = `${stateKey(f.root)}:pending-config-write`;
  state.update = async (key, value) => {
    await update(key, value);
    if (key === pendingKey && value?.receipt === null) fs.writeFileSync(file, userText);
  };
  await assert.rejects(createManagedProjectConfig(f.vscode, f.folder, state, f.worker), /changed concurrently/);
  assert.equal(f.configFile.calls.length, 0);
  assert.equal(state.get(pendingKey), undefined);
  assert.equal(fs.readFileSync(file, "utf8"), userText);
  assert.equal(fs.readdirSync(f.root).some(name => /^\.graphql-desk-[a-f0-9-]+\.tmp$/.test(name)), false);
});

for (const race of ["config edit", "dirty buffer", "trust revoked", "workspace removed", "staged edit"]) {
  test(`config replacement preserves ${race} during a Windows-lock retry`, async t => {
    const original = '{"extraPaths":["vendor"],"stubPath":"stubs"}\n';
    const userText = "// concurrent user edit\n" + original;
    const f = fixture(t, { json: original });
    const file = path.join(f.root, "pyrightconfig.json");
    const rename = fs.renameSync;
    let attempts = 0;
    let staged;
    t.mock.method(fs, "renameSync", (source, destination) => {
      if (destination !== file) return rename(source, destination);
      attempts += 1;
      staged = source;
      queueMicrotask(() => {
        if (race === "config edit") fs.writeFileSync(file, userText);
        else if (race === "dirty buffer") f.vscode.workspace.textDocuments.push({ isDirty: true, uri: f.vscode.Uri.file(file) });
        else if (race === "trust revoked") f.vscode.workspace.isTrusted = false;
        else if (race === "workspace removed") f.vscode.workspace.getWorkspaceFolder = () => undefined;
        else fs.writeFileSync(source, userText);
      });
      throw Object.assign(new Error("locked"), { code: "EPERM" });
    });
    await assert.rejects(updateProjectConfig(f.vscode, f.folder, "add", f.worker),
      /changed|Dirty|trusted/i);
    assert.equal(attempts, 1, "ownership must be rechecked before another rename attempt");
    assert.equal(fs.readFileSync(file, "utf8"), race === "config edit" ? userText : original);
    assert.equal(fs.existsSync(staged), ["staged edit", "trust revoked", "workspace removed"].includes(race));
    if (race === "staged edit") assert.equal(fs.readFileSync(staged, "utf8"), userText);
  });
}

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

test("legacy hash-only config journals require manual recovery and preserve all files", async t => {
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

  await assert.rejects(createManagedProjectConfig(f.vscode, f.folder, state, f.worker), /manual recovery/);
  assert.equal(fs.existsSync(temporary), true);
  assert.equal(fs.existsSync(unrelated), true);
  assert.equal(state.get(`${stateKey(f.root)}:pending-config-write`).version, 1);
  assert.equal(fs.readFileSync(config, "utf8"), '{"extraPaths":["vendor"]}');
});

for (const committed of [false, true]) {
  test(`permission receipts recover ${committed ? "committed" : "uncommitted"} config writes after restart`, async t => {
    const f = fixture(t, { json: '{"extraPaths":["vendor"]}' });
    const state = stateStore();
    const file = path.join(f.root, "pyrightconfig.json");
    const temporary = path.join(f.root, `.graphql-desk-${randomUUID()}.tmp`);
    const unrelated = path.join(f.root, `.graphql-desk-${randomUUID()}.tmp`);
    fs.writeFileSync(unrelated, "unowned");
    const previousHash = createHash("sha256").update(fs.readFileSync(file)).digest("hex");
    const proof = await f.configFile(f.folder, "stage", { file: "pyrightconfig.json", temporary,
      expected_hash: previousHash, text: '{"extraPaths":["vendor","typings"]}' });
    await state.update(`${stateKey(f.root)}:pending-config-write`, {
      version: 2, file: "pyrightconfig.json", temporaryPath: temporary, hash: proof.staged_hash,
      previousHash, token: proof.token, receipt: proof.receipt,
    });
    if (committed) fs.renameSync(temporary, file);
    f.configFile.restart();
    const managed = await createManagedProjectConfig(f.vscode, f.folder, state, f.worker);
    assert.equal(fs.existsSync(temporary), false);
    assert.equal(fs.readFileSync(unrelated, "utf8"), "unowned");
    assert.equal(state.get(`${stateKey(f.root)}:pending-config-write`), undefined);
    assert.ok(f.configFile.calls.some(call => call.action === "resume"));
    await managed.release();
    managed.cleanup();
    assert.deepEqual(parseJsonConfig(fs.readFileSync(file, "utf8")).extraPaths,
      committed ? ["vendor", "typings"] : ["vendor"]);
  });
}

test("recovery preserves a foreign replacement even when its bytes match the receipt", async t => {
  const original = '{"extraPaths":["vendor"]}';
  const f = fixture(t, { json: original });
  const state = stateStore();
  const file = path.join(f.root, "pyrightconfig.json");
  const temporary = path.join(f.root, `.graphql-desk-${randomUUID()}.tmp`);
  const previousHash = createHash("sha256").update(original).digest("hex");
  const proof = await f.configFile(f.folder, "stage", { file: "pyrightconfig.json", temporary,
    expected_hash: previousHash, text: '{"extraPaths":["vendor","typings"]}' });
  const pending = { version: 2, file: "pyrightconfig.json", temporaryPath: temporary,
    previousHash, hash: proof.staged_hash, token: proof.token, receipt: proof.receipt };
  await state.update(`${stateKey(f.root)}:pending-config-write`, pending);
  const foreign = path.join(f.root, "foreign");
  fs.writeFileSync(foreign, fs.readFileSync(temporary));
  fs.renameSync(foreign, temporary);
  f.configFile.restart();
  await assert.rejects(createManagedProjectConfig(f.vscode, f.folder, state, f.worker), /proof changed/);
  assert.equal(fs.existsSync(temporary), true);
  assert.deepEqual(state.get(`${stateKey(f.root)}:pending-config-write`), { ...pending, discarding: true });
  assert.equal(fs.readFileSync(file, "utf8"), original);
});

test("parallel callers cannot overwrite the one active config-write intent", async t => {
  const f = fixture(t, { json: '{}' });
  let entered;
  let release;
  const started = new Promise(resolve => { entered = resolve; });
  const gate = new Promise(resolve => { release = resolve; });
  f.configFile.onRequest(async action => { if (action === "stage") { entered(); await gate; } });
  const first = updateProjectConfig(f.vscode, f.folder, "add", f.worker);
  await started;
  await assert.rejects(updateProjectConfig(f.vscode, f.folder, "add", f.worker), /Another Pyright config write/);
  release();
  await first;
  assert.equal(f.configFile.calls.filter(call => call.action === "stage").length, 1);
  assert.equal(f.journal.filter(item => item.phase === "begin").length, 1);
});

test("a generation becoming stale during native verification cannot change project or Python settings", async t => {
  const f = fixture(t, { json: '{"stubPath":"custom-stubs"}' });
  const state = stateStore();
  const managed = await createManagedProjectConfig(f.vscode, f.folder, state, f.worker);
  const id = `g-1-${randomUUID()}`;
  const directory = path.join(managed.root, "generations", id);
  fs.mkdirSync(directory, { recursive: true });
  const fingerprint = "a".repeat(64);
  const keys = managedStateKeys(f.root, `:project-${createHash("sha256").update("pyrightconfig.json").digest("hex").slice(0, 16)}`);
  await state.update(keys.generationStateKey, { [id]: fingerprint });
  const manifest = { owner: "graphql-desk-analysis-generation", format: 2, id,
    artifactFingerprint: fingerprint };
  let current = true;
  f.configFile.onRequest(action => { if (action === "verify") current = false; });
  await assert.rejects(managed.selectGeneration(directory, () => current, {
    directory, manifest, manifestFingerprint: fingerprint,
    verify: () => ({ directory, manifest, manifestFingerprint: fingerprint }),
  }), /stale/);
  assert.equal(parseJsonConfig(fs.readFileSync(path.join(f.root, "pyrightconfig.json"), "utf8")).stubPath, MANAGED_PATH);
  assert.equal(f.pythonStubPath, MANAGED_PATH);
  assert.equal(state.get(`${stateKey(f.root)}:pending-config-write`), undefined);
  assert.equal(state.get(keys.stateKey).current, MANAGED_PATH);
  f.configFile.onRequest(() => {});
  await managed.release();
  managed.cleanup();
});

test("native file witnesses retain distinctions above JavaScript's safe integer range", async t => {
  const original = '{"extraPaths":["vendor"]}';
  const f = fixture(t, { json: original });
  const file = path.join(f.root, "pyrightconfig.json");
  const lstat = fs.lstatSync;
  const fstat = fs.fstatSync;
  const inode = lstat(file, { bigint: true }).ino;
  let offset = 0n;
  const adjust = stat => {
    if (typeof stat.ino === "bigint" && stat.ino === inode) stat.ino = 2n ** 100n + offset;
    return stat;
  };
  t.mock.method(fs, "lstatSync", (...args) => adjust(lstat(...args)));
  t.mock.method(fs, "fstatSync", (...args) => adjust(fstat(...args)));
  const configFile = async (...args) => {
    const reply = await f.configFile(...args);
    if (args[1] === "verify") offset = 1n;
    return reply;
  };
  await assert.rejects(rawUpdateProjectConfig(f.vscode, f.folder, "add", f.worker, {}, MANAGED_PATH,
    async () => true, configFile), /changed concurrently/);
  assert.equal(fs.readFileSync(file, "utf8"), original);
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
  let becameStale = false;
  f.onPythonSettingChange(value => { if (value?.endsWith(id)) becameStale = true; });
  assert.equal(await managed.selectGeneration(directory, () => { currentChecks += 1; return !becameStale; }, {
    directory, manifest, manifestFingerprint: fingerprint,
    verify: () => ({ directory, manifest, manifestFingerprint: fingerprint }),
  }), false);
  assert.ok(currentChecks >= 2);
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

test("routing releases the exact shadowed TOML config before acquiring JSON", async t => {
  const toml = '[tool.pyright]\nstubPath = "custom-stubs"\n';
  const f = fixture(t, { toml });
  const state = stateStore();
  const managed = await createManagedProjectConfig(f.vscode, f.folder, state, f.worker);
  const json = '{"stubPath":"json-user-stubs","extraPaths":["json-vendor"]}';
  fs.writeFileSync(path.join(f.root, "pyrightconfig.json"), json);
  await managed.releaseForRouting();
  assert.equal(fs.readFileSync(path.join(f.root, "pyproject.toml"), "utf8"), toml);
  assert.equal(fs.readFileSync(path.join(f.root, "pyrightconfig.json"), "utf8"), json);
  assert.equal(state.get(stateKey(f.root)), undefined);
  const replacement = await createManagedProjectConfig(f.vscode, f.folder, state, f.worker);
  await replacement.release();
  assert.deepEqual(JSON.parse(fs.readFileSync(path.join(f.root, "pyrightconfig.json"), "utf8")), JSON.parse(json));
});

test("routing preserves a permanent external Python stub choice without transiently restoring the old choice", async t => {
  const original = '{"stubPath":"custom-stubs","extraPaths":["vendor"]}';
  const f = fixture(t, { json: original });
  const state = stateStore();
  const managed = await createManagedProjectConfig(f.vscode, f.folder, state, f.worker);
  const generation = path.join(managed.root, "generations", "g-1-00000000-0000-0000-0000-000000000000");
  fs.mkdirSync(generation, { recursive: true });
  const verification = { directory: generation, manifest: { owner: "graphql-desk-analysis-generation", format: 2,
    id: path.basename(generation), artifactFingerprint: "a" }, manifestFingerprint: "b".repeat(64), verify() { return this; } };
  assert.equal(await managed.recordGeneration(generation, verification.manifestFingerprint), true);
  assert.equal(await managed.selectGeneration(generation, () => true, verification), true);
  await f.vscode.workspace.getConfiguration().update("analysis.stubPath", "new-user-stubs", 2);
  const updates = [];
  f.onPythonSettingChange(value => updates.push(value));
  await managed.releaseForRouting();
  assert.equal(f.pythonStubPath, "new-user-stubs");
  assert.deepEqual(updates, [], "the new user choice must never be overwritten during handoff");
  assert.deepEqual(JSON.parse(fs.readFileSync(path.join(f.root, "pyrightconfig.json"), "utf8")), JSON.parse(original));
  const protectedState = state.snapshot();
  const protectedGenerations = fs.readdirSync(path.join(managed.root, "generations")).sort();
  assert.equal(managed.ownsSetting, false, "the released inner manager must lose its publication capability");
  assert.equal(managed.stillOwnsSetting(), false);
  assert.equal(await managed.checkSettingOwnership(), false);
  assert.equal(await managed.recordGeneration(generation, verification.manifestFingerprint), false);
  assert.equal(await managed.selectGeneration(generation, () => true, verification), false);
  const stagedName = `.graphql-desk-${randomUUID()}-marker-${randomUUID()}.tmp`;
  await assert.rejects(managed.recordStaging(generation, "c".repeat(64), stagedName), /not owned/);
  const publisher = createAnalysisGenerations(f.vscode, f.root, managed.root);
  const packages = new Map([["example_generated", [{ relative_path: "example_generated/default.pyi", text: "value: str\n" }]]]);
  assert.equal(await publisher.publish(packages, "retired-publisher", () => true, managed, "a".repeat(64), "b".repeat(64), []), false);
  assert.deepEqual(state.snapshot(), protectedState, "retired capabilities cannot change any workspace-state record");
  assert.deepEqual(fs.readdirSync(path.join(managed.root, "generations")).sort(), protectedGenerations);
  assert.deepEqual(JSON.parse(fs.readFileSync(path.join(f.root, "pyrightconfig.json"), "utf8")), JSON.parse(original));
  assert.deepEqual(updates, []);
  const replacement = await createManagedProjectConfig(f.vscode, f.folder, state, f.worker);
  await replacement.release();
  assert.equal(f.pythonStubPath, "new-user-stubs", "the replacement restores the newly captured user choice");
});

test("an already managed empty folder reroutes when JSON or a Pyright TOML table is created", async t => {
  for (const kind of ["json", "toml"]) {
    const f = fixture(t);
    const state = stateStore();
    const current = await createManagedStubPath(f.vscode, f.folder, state);
    const generation = path.join(current.root, "generations", "g-1-00000000-0000-0000-0000-000000000000");
    fs.mkdirSync(generation, { recursive: true });
    const verification = { directory: generation, manifest: { owner: "graphql-desk-analysis-generation", format: 2,
      id: path.basename(generation), artifactFingerprint: "a" }, manifestFingerprint: "b".repeat(64), verify() { return this; } };
    assert.equal(await current.recordGeneration(generation, verification.manifestFingerprint), true);
    assert.equal(await current.selectGeneration(generation, () => true, verification), true);
    const roots = new Map([[f.root, Promise.resolve(current)]]);
    const filename = path.join(f.root, kind === "json" ? "pyrightconfig.json" : "pyproject.toml");
    const original = kind === "json" ? '{"stubPath":"user-stubs"}' : '[tool.pyright]\nstubPath = "user-stubs"\n';
    fs.writeFileSync(filename, original);
    await reconfigureWorkspaceRoots({ roots, folders: [f.folder],
      routeCurrent: async (_folder, managed) => {
        const route = await inspectProjectConfigRoute(f.vscode, f.folder, f.worker);
        assert.equal(route.isCurrent(), true);
        return route.file === (managed.configurationFile ?? null);
      },
      retire: async (_root, managed, reroute) => { assert.equal(reroute, true); await managed.releaseForRouting(); },
      create: async () => createManagedProjectConfig(f.vscode, f.folder, state, f.worker) });
    const next = await roots.get(f.root);
    assert.equal(next.projectConfig, true);
    assert.equal(state.get(stateKey(f.root)).pythonStubPath.snapshot.folderPresent, false);
    await next.release();
    assert.equal(f.pythonStubPath, undefined);
  }
});

test("routing does not recreate a deleted config or a valid removed TOML table", async t => {
  for (const kind of ["json", "toml"]) {
    const f = fixture(t, kind === "json" ? { json: '{"stubPath":"user-stubs"}' } : { toml: '[tool.pyright]\nstubPath = "user-stubs"\n' });
    const state = stateStore();
    const managed = await createManagedProjectConfig(f.vscode, f.folder, state, f.worker);
    const filename = path.join(f.root, kind === "json" ? "pyrightconfig.json" : "pyproject.toml");
    if (kind === "json") fs.unlinkSync(filename);
    else fs.writeFileSync(filename, '[project]\nname = "kept"\n');
    await managed.releaseForRouting();
    assert.equal(f.pythonStubPath, undefined);
    assert.equal(state.get(stateKey(f.root)), undefined);
    if (kind === "json") assert.equal(fs.existsSync(filename), false);
    else assert.equal(fs.readFileSync(filename, "utf8"), '[project]\nname = "kept"\n');
  }
});

test("routing keeps a new lower-priority Python choice while removing only the owned override", async t => {
  const f = fixture(t, { json: '{"stubPath":"project-user-stubs"}', inheritedPythonStubPath: "old-inherited-stubs" });
  const state = stateStore();
  const managed = await createManagedProjectConfig(f.vscode, f.folder, state, f.worker);
  f.setInheritedPythonStubPath("new-inherited-stubs");
  assert.equal(managed.routingSettingsCurrent(), false, "effective equality cannot hide a changed lower-priority layer");
  const updates = [];
  f.onPythonSettingChange(value => updates.push(value));
  await managed.releaseForRouting();
  assert.equal(f.vscode.workspace.getConfiguration().get("analysis.stubPath"), "new-inherited-stubs");
  assert.equal(updates.includes("old-inherited-stubs"), false);
  assert.equal(updates.includes(".graphql-desk/live-typings"), false);
  const replacement = await createManagedProjectConfig(f.vscode, f.folder, state, f.worker);
  await replacement.release();
  assert.equal(f.vscode.workspace.getConfiguration().get("analysis.stubPath"), "new-inherited-stubs");
});

for (const layer of ["workspace", "global"]) for (const startup of [true, false]) for (const workspaceSerialized of layer === "global" ? [false, true] : [false]) {
  test(`project routing keeps hidden ${layer} changes during owned Python ${startup ? "startup" : "rotation"}${workspaceSerialized ? " serialized at workspace scope" : ""}`, async t => {
    const original = '{"stubPath":"project-user-stubs"}';
    const f = fixture(t, { json: original, workspaceSerialized });
    const state = stateStore();
    let managed = startup ? null : await createManagedProjectConfig(f.vscode, f.folder, state, f.worker);
    let release, entered;
    const gate = new Promise(resolve => { release = resolve; });
    const started = new Promise(resolve => { entered = resolve; });
    let holding = true;
    f.onPythonSettingChange(async () => { if (holding) { holding = false; entered(); await gate; } });
    let pending;
    if (startup) pending = createManagedProjectConfig(f.vscode, f.folder, state, f.worker);
    else {
      const generation = path.join(managed.root, "generations", `g-1-${randomUUID()}`);
      fs.mkdirSync(generation, { recursive: true });
      const verification = { directory: generation, manifest: { owner: "graphql-desk-analysis-generation", format: 2,
        id: path.basename(generation), artifactFingerprint: "a" }, manifestFingerprint: "b".repeat(64), verify() { return this; } };
      assert.equal(await managed.recordGeneration(generation, verification.manifestFingerprint), true);
      pending = managed.selectGeneration(generation, () => true, verification);
    }
    await Promise.race([started, pending.then(() => assert.fail("the actual Python update must be held"))]);
    const selected = f.pythonStubPath;
    const choice = `new-${layer}-stubs`;
    if (layer === "workspace") f.setInheritedPythonStubPath(choice);
    else f.setGlobalPythonStubPath(choice);
    assert.equal(f.vscode.workspace.getConfiguration().get("analysis.stubPath"), selected, "the new user choice is hidden");
    release();
    if (startup) managed = await pending;
    else assert.equal(await pending, true);
    assert.equal(managed.routingSettingsCurrent(), false, "the owned update must not absorb a hidden user choice");
    assert.equal(state.get(stateKey(f.root)).pythonStubPath.snapshot[`${layer}Present`], false, "the saved prior stays unchanged until routing");
    await managed.releaseForRouting();
    assert.equal(f.vscode.workspace.getConfiguration().get("analysis.stubPath"), choice);
    const replacement = await createManagedProjectConfig(f.vscode, f.folder, state, f.worker);
    await replacement.release();
    assert.equal(f.vscode.workspace.getConfiguration().get("analysis.stubPath"), choice);
    assert.deepEqual(JSON.parse(fs.readFileSync(path.join(f.root, "pyrightconfig.json"), "utf8")), JSON.parse(original));
  });
}

test("routing waits for the current native generation transaction before capturing release inputs", async t => {
  const f = fixture(t, { json: '{"stubPath":"project-user-stubs"}' });
  const state = stateStore();
  const managed = await createManagedProjectConfig(f.vscode, f.folder, state, f.worker);
  const generation = path.join(managed.root, "generations", "g-1-00000000-0000-0000-0000-000000000000");
  fs.mkdirSync(generation, { recursive: true });
  const verification = { directory: generation, manifest: { owner: "graphql-desk-analysis-generation", format: 2,
    id: path.basename(generation), artifactFingerprint: "a" }, manifestFingerprint: "b".repeat(64), verify() { return this; } };
  assert.equal(await managed.recordGeneration(generation, verification.manifestFingerprint), true);
  let release, entered;
  const gate = new Promise(resolve => { release = resolve; });
  const started = new Promise(resolve => { entered = resolve; });
  let holding = true;
  f.configFile.onRequest(async action => { if (action === "verify" && holding) { holding = false; entered(); await gate; } });
  const selecting = managed.selectGeneration(generation, () => true, verification);
  await Promise.race([started, selecting.then(() => assert.fail("the native verify must be held"))]);
  let finished = false;
  const releasing = managed.releaseForRouting().then(() => { finished = true; });
  try {
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(finished, false);
  } finally { release(); }
  assert.equal(await selecting, true);
  await releasing;
  assert.equal(f.pythonStubPath, undefined);
  assert.equal(JSON.parse(fs.readFileSync(path.join(f.root, "pyrightconfig.json"), "utf8")).stubPath, "project-user-stubs");
});

async function interruptedPythonRouting(t, boundary, choice = "restored") {
  const original = '{"stubPath":"project-user-stubs","extraPaths":["vendor"]}';
  const f = fixture(t, { json: original, standaloneAliases: true, globalPythonStubPath: "global-user-stubs" });
  const state = stateStore();
  const managed = await createManagedProjectConfig(f.vscode, f.folder, state, f.worker);
  const publisher = createAnalysisGenerations(f.vscode, f.root, managed.root);
  const groups = new Map([["example_generated", [
    { relative_path: "example_generated/default.py", text: 'value = "safe"\n' },
    { relative_path: "example_generated/default.pyi", text: "value: str\n" },
  ]]]);
  assert.equal(await publisher.publish(groups, "before-transient-routing", () => true, managed,
    "a".repeat(64), "b".repeat(64), []), true);
  const complete = publisher.current(managed)[0];
  assert.ok(complete);
  const protectedFiles = [...complete.files.keys(), "complete.json"]
    .map(name => [path.join(complete.directory, name), fs.readFileSync(path.join(complete.directory, name))]);
  const selected = f.pythonStubPath;
  const latest = choice === "restored" ? selected : choice === "later" ? "later-user-stubs" : "custom_stubs";
  await f.vscode.workspace.getConfiguration().update("analysis.stubPath", "custom_stubs", 2);
  let interrupted = false;
  f.configFile.onRequest(async action => {
    if (action !== boundary || interrupted) return;
    interrupted = true;
    if (choice === "unchanged") throw new Error("interrupted routing release");
    await f.vscode.workspace.getConfiguration().update("analysis.stubPath", latest, 2);
  });
  await assert.rejects(managed.releaseForRouting(), /routing inputs changed|interrupted routing release/);
  assert.equal(interrupted, true);
  assert.equal(state.get(stateKey(f.root)).routing, true);
  assert.equal(managed.ownsSetting, false, "The durable release intent revokes the old handle even when later I/O fails");
  const protectedState = state.snapshot();
  assert.equal(await managed.recordGeneration(complete.directory, complete.manifestFingerprint), false);
  assert.equal(await managed.forgetGeneration(complete.directory, complete.manifestFingerprint), false);
  assert.equal(await managed.selectGeneration(complete.directory, () => true, complete), false);
  await assert.rejects(managed.recordStaging(path.join(managed.root, "generations", `g-2-${randomUUID()}`), "c".repeat(64),
    `.graphql-desk-${randomUUID()}-marker-${randomUUID()}.tmp`), /not owned/);
  assert.deepEqual(state.snapshot(), protectedState, "Interrupted old capabilities cannot change retained cleanup/reader proof");
  f.configFile.restart();
  f.configFile.onRequest(() => {});
  return { f, state, managed, selected, latest, original, complete, protectedFiles };
}

for (const boundary of ["verify", "finish"]) for (const choice of ["restored", "later", "unchanged"]) {
  test(`routing recovery preserves the ${choice} Python choice after interruption at ${boundary}`, async t => {
    const { f, state, managed, latest, original, complete, protectedFiles } = await interruptedPythonRouting(t, boundary, choice);
    const before = structuredClone(f.vscode.workspace.getConfiguration().inspect("analysis.stubPath"));
    const updates = [];
    f.onPythonSettingChange(value => updates.push(value));
    let releaseBoundary;
    const update = state.update;
    state.update = async (key, value) => {
      await update(key, value);
      if (key === stateKey(f.root) && value === undefined && !releaseBoundary) {
        releaseBoundary = { settings: structuredClone(f.vscode.workspace.getConfiguration().inspect("analysis.stubPath")),
          updates: [...updates], config: JSON.parse(fs.readFileSync(path.join(f.root, "pyrightconfig.json"), "utf8")) };
      }
    };
    const next = await createManagedProjectConfig(f.vscode, f.folder, state, f.worker);
    assert.ok(releaseBoundary, "Observe release completion before fresh authorized setup writes anything");
    assert.deepEqual(releaseBoundary.settings, before, "Old config release preserves the latest Python choice across every layer");
    assert.deepEqual(releaseBoundary.updates, []);
    assert.deepEqual(releaseBoundary.config, JSON.parse(original));
    for (const [file, bytes] of protectedFiles) assert.deepEqual(fs.readFileSync(file), bytes);
    assert.equal(managed.ownsSetting, false, "The interrupted old handle is permanently revoked");
    assert.equal(await managed.recordGeneration(complete.directory, complete.manifestFingerprint), false);
    assert.equal(await managed.selectGeneration(complete.directory, () => true, complete), false);
    const protectedState = state.snapshot();
    await assert.rejects(managed.recordStaging(path.join(managed.root, "generations", `g-2-${randomUUID()}`), "c".repeat(64),
      `.graphql-desk-${randomUUID()}-marker-${randomUUID()}.tmp`), /not owned/);
    assert.deepEqual(state.snapshot(), protectedState);
    assert.deepEqual(state.get(stateKey(f.root)).pythonStubPath.snapshot, {
      folderPresent: true, folderValue: latest, workspacePresent: true, workspaceValue: latest,
      globalPresent: true, globalValue: "global-user-stubs", singleFolderDirectory: true,
    }, "Only the fresh manager may capture the preserved choice as its prior value");
    assert.equal(next.routingSettingsCurrent(), true);
    await next.release();
    assert.equal(f.pythonStubPath, latest);
    assert.deepEqual(f.vscode.workspace.getConfiguration().inspect("analysis.stubPath"), before);
  });
}

for (const boundary of ["settings-dirty", "config-dirty", "malformed", "invalid-python", "python", "hidden-python", "owner-state-python", "config-identity", "currentness"]) {
  test(`routing recovery keeps user data and durable evidence when ${boundary} changes`, async t => {
    const { f, state, managed, latest, protectedFiles } = await interruptedPythonRouting(t, "verify");
    const file = path.join(f.root, "pyrightconfig.json");
    const before = fs.readFileSync(file, "utf8");
    let current = true;
    let changed = false;
    let replacementIdentity;
    if (boundary === "settings-dirty" || boundary === "config-dirty") f.vscode.workspace.textDocuments.push({ isDirty: true,
      uri: { scheme: "file", fsPath: boundary === "config-dirty" ? file : path.join(f.root, ".vscode", "settings.json") } });
    if (boundary === "malformed") fs.writeFileSync(file, "{");
    if (boundary === "invalid-python") await f.vscode.workspace.getConfiguration().update("analysis.stubPath", 7, 2);
    if (boundary === "owner-state-python") {
      const update = state.update;
      state.update = async (key, value) => {
        await update(key, value);
        if (!changed && key === stateKey(f.root) && value?.pythonStubPath?.current === latest) {
          changed = true;
          await f.vscode.workspace.getConfiguration().update("analysis.stubPath", "third-user-choice", 2);
        }
      };
    }
    f.configFile.onRequest(async action => {
      if (action !== "verify" || changed) return;
      changed = true;
      if (boundary === "python") await f.vscode.workspace.getConfiguration().update("analysis.stubPath", "third-user-choice", 2);
      if (boundary === "hidden-python") f.setGlobalPythonStubPath("third-hidden-choice");
      if (boundary === "currentness") current = false;
      if (boundary === "config-identity") {
        fs.renameSync(file, path.join(f.root, "preserved-original-config.json"));
        fs.writeFileSync(file, before);
        replacementIdentity = fs.lstatSync(file, { bigint: true });
      }
    });
    await assert.rejects(createManagedProjectConfig(f.vscode, f.folder, state, f.worker, {
      ownerCurrent: () => current && f.vscode.workspace.isTrusted && f.vscode.workspace.workspaceFolders.includes(f.folder),
    }), /dirty|changed|malformed|invalid|expected|preserved|JSON/i);
    assert.equal(fs.readFileSync(file, "utf8"), boundary === "malformed" ? "{" : before);
    assert.equal(f.pythonStubPath, ["python", "owner-state-python"].includes(boundary) ? "third-user-choice" : boundary === "invalid-python" ? 7 : latest);
    if (boundary === "hidden-python") assert.equal(f.vscode.workspace.getConfiguration().inspect("analysis.stubPath").globalValue, "third-hidden-choice");
    if (replacementIdentity) assert.equal(fs.lstatSync(file, { bigint: true }).ino, replacementIdentity.ino);
    assert.equal(state.get(stateKey(f.root)).routing, true, "A failed recovery cannot erase its durable release evidence");
    assert.equal(managed.ownsSetting, false);
    for (const [filename, bytes] of protectedFiles) assert.deepEqual(fs.readFileSync(filename), bytes);
  });
}

test("interrupted routing release recovers the old shadowed config before adopting the new config", async t => {
  const original = '[tool.pyright]\nstubPath = "old-user-stubs"\n';
  const f = fixture(t, { toml: original });
  const state = stateStore();
  const managed = await createManagedProjectConfig(f.vscode, f.folder, state, f.worker);
  const publisher = createAnalysisGenerations(f.vscode, f.root, managed.root);
  const groups = new Map([["example_generated", [
    { relative_path: "example_generated/default.py", text: 'value = "old"\n' },
    { relative_path: "example_generated/default.pyi", text: "value: str\n" },
  ]]]);
  assert.equal(await publisher.publish(groups, "routing-before-interruption", () => true, managed,
    "a".repeat(64), "b".repeat(64), []), true);
  const complete = publisher.current(managed)[0];
  assert.ok(complete, "the retained generation must have a real verified complete manifest");
  const stagingId = `g-2-${randomUUID()}`;
  const staging = path.join(managed.root, "generations", stagingId);
  fs.mkdirSync(staging);
  const stagedFile = path.join(staging, "pending.pyi");
  fs.writeFileSync(stagedFile, "value: object\n");
  const markerFile = path.join(managed.root, "generations", `${stagingId}.staging.json`);
  fs.writeFileSync(markerFile, JSON.stringify({ owner: "graphql-desk-analysis-staging", format: 1,
    id: stagingId, sequence: 2, token: randomUUID(), files: [] }));
  assert.equal(await managed.recordStaging(staging, createHash("sha256").update(fs.readFileSync(markerFile)).digest("hex"),
    `.graphql-desk-${randomUUID()}-marker-${randomUUID()}.tmp`), true);
  const keys = managedStateKeys(f.root, `:project-${createHash("sha256").update("pyproject.toml").digest("hex").slice(0, 16)}`);
  const generationRecords = structuredClone(state.get(keys.generationStateKey));
  const stagingRecords = structuredClone(state.get(keys.stagingStateKey));
  const protectedFiles = [...complete.files.keys()].map(relative => path.join(complete.directory, relative))
    .concat(path.join(complete.directory, "complete.json"), stagedFile, markerFile)
    .map(file => [file, fs.readFileSync(file)]);
  const json = '{"stubPath":"new-user-stubs"}';
  fs.writeFileSync(path.join(f.root, "pyrightconfig.json"), json);
  f.configFile.onRequest(action => { if (action === "verify") throw new Error("interrupted release"); });
  await assert.rejects(managed.releaseForRouting(), /interrupted release/);
  assert.equal(state.get(stateKey(f.root)).routing, true);
  f.configFile.restart();
  f.configFile.onRequest(() => {});
  const replacement = await createManagedProjectConfig(f.vscode, f.folder, state, f.worker);
  assert.deepEqual(state.get(keys.generationStateKey), generationRecords);
  assert.deepEqual(state.get(keys.stagingStateKey), stagingRecords);
  for (const [file, bytes] of protectedFiles) assert.deepEqual(fs.readFileSync(file), bytes, "routing recovery cannot discard reader/cleanup ownership proof or bytes");
  assert.equal(fs.readFileSync(path.join(f.root, "pyproject.toml"), "utf8"), original);
  assert.equal(replacement.configurationFile, path.join(f.root, "pyrightconfig.json"));
  await replacement.release();
  assert.equal(JSON.parse(fs.readFileSync(path.join(f.root, "pyrightconfig.json"), "utf8")).stubPath, "new-user-stubs");
});

test("interrupted routing release after physical deletion or table removal never recreates the old config", async t => {
  for (const kind of ["deleted", "table"]) {
    const f = fixture(t, { toml: '[tool.pyright]\nstubPath = "old-user-stubs"\n' });
    const state = stateStore();
    const managed = await createManagedProjectConfig(f.vscode, f.folder, state, f.worker);
    const file = path.join(f.root, "pyproject.toml");
    if (kind === "deleted") fs.unlinkSync(file);
    else fs.writeFileSync(file, '[project]\nname = "kept"\n');
    f.onPythonSettingChange(() => { throw new Error("interrupted settings restore"); });
    await assert.rejects(managed.releaseForRouting(), /interrupted settings restore/);
    f.onPythonSettingChange(undefined);
    assert.equal(await createManagedProjectConfig(f.vscode, f.folder, state, f.worker), null);
    assert.equal(state.get(stateKey(f.root)), undefined);
    if (kind === "deleted") assert.equal(fs.existsSync(file), false);
    else assert.equal(fs.readFileSync(file, "utf8"), '[project]\nname = "kept"\n');
  }
});

test("routing refuses dirty old config, malformed new config, or a changed Python choice during native verification", async t => {
  for (const boundary of ["dirty", "settings-dirty", "malformed", "python", "hidden-python", "source"]) {
    const f = fixture(t, { json: '{"stubPath":"user-stubs"}' });
    const state = stateStore();
    const managed = await createManagedProjectConfig(f.vscode, f.folder, state, f.worker);
    const filename = path.join(f.root, "pyrightconfig.json");
    const before = fs.readFileSync(filename, "utf8");
    let current = true;
    if (boundary === "dirty") f.vscode.workspace.textDocuments.push({ isDirty: true, uri: { scheme: "file", fsPath: filename } });
    if (boundary === "settings-dirty") f.vscode.workspace.textDocuments.push({ isDirty: true, uri: { scheme: "file", fsPath: path.join(f.root, ".vscode", "settings.json") } });
    if (boundary === "malformed") fs.writeFileSync(filename, "{");
    if (["python", "hidden-python", "source"].includes(boundary)) f.configFile.onRequest(async action => {
      if (action === "verify") {
        if (boundary === "python") await f.vscode.workspace.getConfiguration().update("analysis.stubPath", "later-user-choice", 2);
        else if (boundary === "hidden-python") f.setInheritedPythonStubPath("later-inherited-choice");
        else current = false;
      }
    });
    await assert.rejects(managed.releaseForRouting(() => current), /Dirty|Malformed|invalid|changed|stale/i);
    assert.equal(fs.readFileSync(filename, "utf8"), boundary === "malformed" ? "{" : before);
    assert.ok(state.get(stateKey(f.root)), "the recoverable owner cannot be discarded on a failed handoff");
    if (boundary === "python") assert.equal(f.pythonStubPath, "later-user-choice");
  }
});

test("a new higher-priority config during routing verification preserves both configs and recovery state", async t => {
  const f = fixture(t, { toml: '[tool.pyright]\nstubPath = "user-stubs"\n' });
  const state = stateStore();
  const managed = await createManagedProjectConfig(f.vscode, f.folder, state, f.worker);
  const toml = path.join(f.root, "pyproject.toml");
  const before = fs.readFileSync(toml, "utf8");
  const json = '{"stubPath":"new-choice"}';
  f.configFile.onRequest(action => { if (action === "verify") fs.writeFileSync(path.join(f.root, "pyrightconfig.json"), json); });
  await assert.rejects(managed.releaseForRouting(), /changed/);
  assert.equal(fs.readFileSync(toml, "utf8"), before);
  assert.equal(fs.readFileSync(path.join(f.root, "pyrightconfig.json"), "utf8"), json);
  assert.equal(state.get(stateKey(f.root)).routing, true);
});

test("routing inspection rejects a dangling or redirected config candidate rather than treating it as missing", async t => {
  const f = fixture(t, { json: '{}', toml: '[tool.pyright]\n' });
  const file = path.join(f.root, "pyrightconfig.json");
  const lstat = fs.lstatSync;
  t.mock.method(fs, "lstatSync", (target, ...options) => {
    const stat = lstat(target, ...options);
    if (target === file) stat.isSymbolicLink = () => true;
    return stat;
  });
  await assert.rejects(inspectProjectConfigRoute(f.vscode, f.folder, f.worker), /safe file/);
});

for (const jsonWins of [false, true]) {
  test(`read-only routing preserves a dirty GraphQL-only TOML ${jsonWins ? "below JSON" : "with the fallback manager"}`, async t => {
    const toml = '[tool.graphql-desk]\nschema="schema.graphql"\n';
    const f = fixture(t, { ...(jsonWins ? { json: '{"stubPath":"user-stubs"}' } : {}), toml });
    const file = path.join(f.root, "pyproject.toml");
    const dirty = { uri: f.vscode.Uri.file(file), isDirty: true, getText: () => `${toml}# unsaved GraphQL change\n` };
    f.vscode.workspace.textDocuments.push(dirty);
    const state = stateStore();
    const project = await createManagedProjectConfig(f.vscode, f.folder, state, f.worker);
    if (!jsonWins) assert.equal(project, null, "Read-only absence detection permits initial fallback acquisition too");
    const managed = project ?? await createManagedStubPath(f.vscode, f.folder, state);
    const roots = new Map([[f.root, Promise.resolve(managed)]]);
    await reconfigureWorkspaceRoots({ roots, folders: [f.folder],
      create: () => assert.fail("An unchanged route cannot allocate another manager"),
      retire: () => assert.fail("A dirty unrelated GraphQL buffer cannot retire this manager"),
      routeCurrent: async (folder, owner) => {
        const route = await inspectProjectConfigRoute(f.vscode, folder, f.worker);
        assert.equal(route.isCurrent(), true);
        return route.file === (owner.configurationFile ?? null);
      } });
    assert.equal(await roots.get(f.root), managed);
    assert.equal(managed.stillOwnsSetting(), true);
    assert.equal(fs.readFileSync(file, "utf8"), toml);
    assert.equal(dirty.isDirty, true);
    assert.match(dirty.getText(), /unsaved GraphQL change/);
  });
}

for (const format of ["json", "toml"]) {
  test(`read-only routing still refuses a dirty effective ${format} writer`, async t => {
    const original = format === "json" ? '{"stubPath":"user-stubs"}' : '[tool.pyright]\nstubPath="user-stubs"\n';
    const f = fixture(t, { [format]: original });
    const file = path.join(f.root, format === "json" ? "pyrightconfig.json" : "pyproject.toml");
    f.vscode.workspace.textDocuments.push({ uri: f.vscode.Uri.file(file), isDirty: true });
    await assert.rejects(inspectProjectConfigRoute(f.vscode, f.folder, f.worker), /Dirty/);
    await assert.rejects(updateProjectConfig(f.vscode, f.folder, "add", f.worker), /Dirty/);
    assert.equal(fs.readFileSync(file, "utf8"), original);
    assert.deepEqual(fs.readdirSync(f.root), [path.basename(file)]);
  });
}

for (const boundary of ["bytes", "file-identity", "workspace-identity", "precedence", "trust", "folder", "dirty-effective"]) {
  test(`read-only routing rechecks ${boundary} after TOML inspection`, async t => {
    const original = boundary === "dirty-effective" ? '[tool.pyright]\nstubPath="user-stubs"\n' : '[tool.graphql-desk]\nschema="schema.graphql"\n';
    const f = fixture(t, { toml: original });
    const file = path.join(f.root, "pyproject.toml");
    const lstat = fs.lstatSync;
    let changed = false;
    if (boundary === "workspace-identity") t.mock.method(fs, "lstatSync", (target, ...args) => {
      const stat = lstat(target, ...args);
      if (target === f.root && changed && typeof stat.ino === "bigint") stat.ino += 1n;
      return stat;
    });
    const worker = async (...args) => {
      const result = await f.worker(...args);
      changed = true;
      if (boundary === "bytes") fs.appendFileSync(file, "# concurrent edit\n");
      if (boundary === "file-identity") { fs.renameSync(file, `${file}.held`); fs.writeFileSync(file, original, { flag: "wx" }); }
      if (boundary === "precedence") fs.writeFileSync(path.join(f.root, "pyrightconfig.json"), "{}", { flag: "wx" });
      if (boundary === "trust") f.vscode.workspace.isTrusted = false;
      if (boundary === "folder") f.vscode.workspace.getWorkspaceFolder = () => undefined;
      if (boundary === "dirty-effective") f.vscode.workspace.textDocuments.push({ uri: f.vscode.Uri.file(file), isDirty: true });
      return result;
    };
    await assert.rejects(inspectProjectConfigRoute(f.vscode, f.folder, worker), /changed|Dirty/);
    assert.equal(fs.readFileSync(file, "utf8"), boundary === "bytes" ? `${original}# concurrent edit\n` : original);
    assert.equal(fs.readdirSync(f.root).some(name => name.endsWith(".tmp")), false);
  });
}

test("initial fallback acquisition rejects a higher-priority config appearing during inspection", async t => {
  const f = fixture(t, { toml: '[tool.graphql-desk]\nschema="schema.graphql"\n' });
  const worker = async (...args) => {
    const result = await f.worker(...args);
    fs.writeFileSync(path.join(f.root, "pyrightconfig.json"), "{}", { flag: "wx" });
    return result;
  };
  await assert.rejects(createManagedProjectConfig(f.vscode, f.folder, stateStore(), worker), /routing inputs changed/);
  assert.equal(fs.existsSync(path.join(f.root, ".graphql-desk")), false);
});

test("real TOML inspection permits dirty GraphQL-only absence and refuses malformed disk input", async t => {
  const original = '[tool.graphql-desk]\nschema="schema.graphql"\n';
  const f = fixture(t, { toml: original });
  const file = path.join(f.root, "pyproject.toml");
  f.vscode.workspace.textDocuments.push({ uri: f.vscode.Uri.file(file), isDirty: true, getText: () => `${original}# unsaved\n` });
  const python = process.env.GRAPHQL_DESK_PYTHON ?? path.join(path.resolve(__dirname, "../.."), ".venv",
    process.platform === "win32" ? "Scripts/python.exe" : "bin/python");
  const realWorker = createAnalysisWorker({ python, workspace: f.root, isTrusted: () => true });
  t.after(() => realWorker.dispose());
  const worker = (folder, text, action, managedPath) => realWorker.request({ op: "pyright_config", workspace: folder.uri.fsPath,
    text, action, managed_path: managedPath, ownership: { topLevel: false, executionEnvironmentRoots: [] } });
  assert.equal(await createManagedProjectConfig(f.vscode, f.folder, stateStore(), worker), null);
  assert.equal((await inspectProjectConfigRoute(f.vscode, f.folder, worker)).isCurrent(), true);
  const malformed = '[tool.graphql-desk]\nschema=\n';
  fs.writeFileSync(file, malformed);
  await assert.rejects(inspectProjectConfigRoute(f.vscode, f.folder, worker));
  assert.equal(fs.readFileSync(file, "utf8"), malformed);
  assert.deepEqual(fs.readdirSync(f.root), ["pyproject.toml"]);
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

for (const rollback of [false, true]) test(`package validation waits for native generation ${rollback ? "rollback" : "commit"}`, async t => {
  const f = fixture(t, { json: '{"stubPath":"custom-stubs","extraPaths":["vendor"]}' });
  const state = stateStore();
  const managed = await createManagedProjectConfig(f.vscode, f.folder, state, f.worker);
  const id = `g-1-${randomUUID()}`;
  const directory = path.join(managed.root, "generations", id);
  fs.mkdirSync(directory, { recursive: true });
  const fingerprint = "a".repeat(64);
  const keys = managedStateKeys(f.root, `:project-${createHash("sha256").update("pyrightconfig.json").digest("hex").slice(0, 16)}`);
  await state.update(keys.generationStateKey, { [id]: fingerprint });
  const manifest = { owner: "graphql-desk-analysis-generation", format: 2, id, artifactFingerprint: fingerprint };
  let releaseFinish, startedFinish;
  const finishing = new Promise(resolve => { releaseFinish = resolve; });
  const started = new Promise(resolve => { startedFinish = resolve; });
  let held = false;
  f.configFile.onRequest(async action => {
    if (action === "finish" && !held) { held = true; startedFinish(); await finishing; }
  });
  let current = true;
  const selecting = managed.selectGeneration(directory, () => current, {
    directory, manifest, manifestFingerprint: fingerprint,
    verify: () => ({ directory, manifest, manifestFingerprint: fingerprint }),
  });
  await started;
  assert.equal(parseJsonConfig(fs.readFileSync(path.join(f.root, "pyrightconfig.json"), "utf8")).stubPath,
    path.relative(f.root, directory).split(path.sep).join("/"), "the native rename has already happened");
  let settled = false;
  const validating = managed.validateGeneratedPackages(["graphql_desk_generated"])
    .then(value => { settled = true; return { value }; }, error => { settled = true; return { error }; });
  let settledDuringFinish;
  try {
    await new Promise(resolve => setImmediate(resolve));
    settledDuringFinish = settled;
    current = !rollback;
  } finally { releaseFinish(); }
  assert.equal(await selecting, !rollback);
  const validationResult = await validating;
  assert.equal(settledDuringFinish, false, "package validation cannot inspect a half-completed generation transaction");
  assert.deepEqual(validationResult, { value: true });
  assert.equal(await managed.checkSettingOwnership(), true);
  await managed.release();
  managed.cleanup();
});

test("async TOML package validation holds the selection queue through inspection", async t => {
  const f = fixture(t, { toml: '[tool.pyright]\nstubPath = "custom-stubs"\n' });
  let hold = false, held = false, releaseInspect, startedInspect;
  const inspecting = new Promise(resolve => { releaseInspect = resolve; });
  const started = new Promise(resolve => { startedInspect = resolve; });
  const worker = async (...args) => {
    if (hold && !held && args[2] === "inspect") { held = true; startedInspect(); await inspecting; }
    return f.worker(...args);
  };
  const managed = await createManagedProjectConfig(f.vscode, f.folder, stateStore(), worker);
  hold = true;
  const validating = managed.validateGeneratedPackages(["graphql_desk_generated"]);
  await started;
  let settled = false;
  const ownership = managed.checkSettingOwnership().then(value => { settled = true; return value; });
  let settledDuringInspect;
  try {
    await new Promise(resolve => setImmediate(resolve));
    settledDuringInspect = settled;
  } finally { releaseInspect(); }
  assert.equal(await validating, true);
  assert.equal(await ownership, true);
  assert.equal(settledDuringInspect, false, "the queue must remain held across the asynchronous config inspection");
  await managed.release();
  managed.cleanup();
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

async function normalReleaseGeneration(t, partialRollback = false) {
  const original = '[tool.pyright]\nstubPath = "custom-stubs"\nextraPaths = ["vendor"]\n';
  const f = fixture(t, { toml: original, pythonStubPath: "python-custom-stubs", standaloneAliases: true });
  const state = stateStore();
  let disposing = false;
  const managed = await createManagedProjectConfig(f.vscode, f.folder, state, f.worker, { ownerCurrent: () => !disposing });
  const publisher = createAnalysisGenerations(f.vscode, f.root, managed.root);
  const groups = type => new Map([["example_generated", [
    { relative_path: "example_generated/default.py", text: 'value = "runtime"\n' },
    { relative_path: "example_generated/default.pyi", text: `value: ${type}\n` },
  ]]]);
  assert.equal(await publisher.publish(groups("str"), "normal-release-first", () => true, managed,
    "a".repeat(64), "b".repeat(64), []), true);
  const keys = managedStateKeys(f.root, `:project-${createHash("sha256").update("pyproject.toml").digest("hex").slice(0, 16)}`);
  const proofRecords = () => [state.get(keys.generationStateKey), state.get(keys.stagingStateKey)].map(value => JSON.stringify(value));
  if (!partialRollback) return { f, state, managed, keys, original, proofRecords };
  const previous = f.pythonStubPath;
  const config = f.vscode.workspace.getConfiguration();
  const update = config.update;
  let entered, rejectUpdate;
  const started = new Promise(resolve => { entered = resolve; });
  const held = new Promise((resolve, reject) => { rejectUpdate = reject; });
  config.update = async (...args) => {
    if (args[1] === previous) { entered(); await held; }
    return update(...args);
  };
  t.after(() => { rejectUpdate(new Error("test cleanup")); config.update = update; });
  let current = true;
  const updateState = state.update;
  state.update = async (key, value) => {
    await updateState(key, value);
    if (key === keys.stateKey && value?.current?.includes("/g-2-") && value.pending !== true) current = false;
  };
  const publishing = publisher.publish(groups("str | None"), "normal-release-second", () => current, managed,
    "c".repeat(64), "d".repeat(64), []).then(value => ({ value }), error => ({ error }));
  await started;
  assert.notEqual(state.get(stateKey(f.root)).currentStubPath,
    /^stubPath\s*=\s*"([^"]*)"/m.exec(fs.readFileSync(path.join(f.root, "pyproject.toml"), "utf8"))[1]);
  disposing = true;
  return { f, state, managed, keys, original, proofRecords, publishing,
    interrupt() { rejectUpdate(new Error("interrupted pending Python rollback")); },
    resume() { disposing = false; config.update = update; } };
}

test("normal release drains a real rollback before journaling and survives interrupted Python mirroring", async t => {
  const h = await normalReleaseGeneration(t, true);
  const records = h.proofRecords();
  let settled = false;
  const releasing = h.managed.release().then(() => { settled = true; }, error => { settled = true; return error; });
  await new Promise(resolve => setImmediate(resolve));
  try {
    assert.equal(settled, false);
    assert.equal(h.state.get(stateKey(h.f.root)).phase, "rotate", "release cannot replace the in-flight rotation's durable intent");
  } finally { h.interrupt(); }
  assert.match((await h.publishing).error.message, /interrupted pending Python rollback/);
  assert.equal(await releasing, undefined);
  assert.equal(fs.readFileSync(path.join(h.f.root, "pyproject.toml"), "utf8"), h.original);
  assert.equal(h.f.pythonStubPath, "python-custom-stubs");
  assert.equal(h.managed.ownsSetting, false);
  assert.equal(h.state.get(h.keys.stateKey), undefined);
  assert.deepEqual(h.proofRecords(), records, "teardown cannot discard generations or staging reader provenance");
  h.resume();
  const reopened = await createManagedProjectConfig(h.f.vscode, h.f.folder, h.state, h.f.worker);
  await reopened.release();
});

for (const boundary of ["verify", "finish"]) test(`normal release recovers a settled partial rollback interrupted at ${boundary}`, async t => {
  const h = await normalReleaseGeneration(t, true);
  h.interrupt();
  assert.match((await h.publishing).error.message, /interrupted pending Python rollback/);
  const records = h.proofRecords();
  let interrupted = false;
  h.f.configFile.onRequest(action => {
    if (action === boundary && !interrupted) { interrupted = true; throw new Error("interrupted normal release"); }
  });
  await assert.rejects(h.managed.release(), /interrupted normal release/);
  assert.equal(interrupted, true);
  const release = h.state.get(stateKey(h.f.root));
  assert.equal(release.phase, "release");
  assert.match(release.currentStubPath, /\/g-1-/);
  assert.match(release.pythonStubPath.current, /\/g-2-/);
  assert.equal(h.managed.ownsSetting, false);
  const snapshot = h.state.snapshot();
  const generation = path.join(h.managed.root, "generations", `g-3-${randomUUID()}`);
  assert.equal(await h.managed.recordGeneration(generation, "a".repeat(64)), false);
  assert.equal(await h.managed.forgetGeneration(generation, "a".repeat(64)), false);
  assert.equal(await h.managed.selectGeneration(generation), false);
  await assert.rejects(h.managed.recordStaging(generation, "b".repeat(64), `.graphql-desk-${randomUUID()}-marker-${randomUUID()}.tmp`), /not owned/);
  assert.deepEqual(h.state.snapshot(), snapshot);
  h.resume(); h.f.configFile.restart(); h.f.configFile.onRequest(() => {});
  const reopened = await createManagedProjectConfig(h.f.vscode, h.f.folder, h.state, h.f.worker);
  assert.deepEqual(h.proofRecords(), records, "cold release recovery retains reader provenance");
  await reopened.release();
  assert.equal(h.f.pythonStubPath, "python-custom-stubs");
  assert.equal(fs.readFileSync(path.join(h.f.root, "pyproject.toml"), "utf8"), h.original);
});

test("normal release revokes its generation capability and permits fresh acquisition", async t => {
  const h = await normalReleaseGeneration(t);
  const records = h.proofRecords();
  await h.managed.release();
  assert.equal(h.managed.ownsSetting, false);
  assert.equal(h.state.get(h.keys.stateKey), undefined);
  assert.deepEqual(h.proofRecords(), records);
  const reopened = await createManagedProjectConfig(h.f.vscode, h.f.folder, h.state, h.f.worker);
  assert.equal(reopened.ownsSetting, true);
  await reopened.release();
});

for (const boundary of ["python-choice", "hidden-python", "dirty-config", "dirty-settings", "native-file", "selection-record", "owner-record", "trust", "folder"]) {
  test(`normal release preserves changed ${boundary} after its durable intent`, async t => {
    const h = await normalReleaseGeneration(t);
    const file = path.join(h.f.root, "pyproject.toml");
    const original = fs.readFileSync(file);
    const records = h.proofRecords();
    let changed = false;
    let replacement;
    h.f.configFile.onRequest(async action => {
      if (action !== "verify" || changed) return;
      changed = true;
      assert.equal(h.managed.ownsSetting, false, "the durable intent revokes the old capability before native cleanup");
      if (boundary === "python-choice") await h.f.vscode.workspace.getConfiguration().update("analysis.stubPath", "later-user-stubs", 2);
      if (boundary === "hidden-python") h.f.setGlobalPythonStubPath("hidden-user-stubs");
      if (boundary.startsWith("dirty-")) h.f.vscode.workspace.textDocuments.push({ isDirty: true,
        uri: h.f.vscode.Uri.file(boundary === "dirty-config" ? file : path.join(h.f.root, ".vscode", "settings.json")) });
      if (boundary === "native-file") {
        fs.renameSync(file, `${file}.preserved`);
        fs.writeFileSync(file, original, { flag: "wx" });
        replacement = fs.lstatSync(file, { bigint: true });
      }
      if (boundary === "selection-record") await h.state.update(h.keys.stateKey, { base: MANAGED_PATH, current: "later-user-stubs", currentPresent: true });
      if (boundary === "owner-record") await h.state.update(stateKey(h.f.root), { ...h.state.get(stateKey(h.f.root)), phase: "unknown" });
      if (boundary === "trust") h.f.vscode.workspace.isTrusted = false;
      if (boundary === "folder") h.f.vscode.workspace.getWorkspaceFolder = () => undefined;
    });
    await assert.rejects(h.managed.release(), /changed|Dirty|dirty|Trusted|trusted|Config proof/i);
    assert.equal(changed, true);
    assert.deepEqual(fs.readFileSync(file), original, "foreign changes cannot authorize restoring the project config");
    assert.notEqual(h.state.get(stateKey(h.f.root)), undefined, "failed cleanup retains its durable evidence");
    assert.deepEqual(h.proofRecords(), records);
    assert.equal(h.managed.ownsSetting, false);
    if (replacement) assert.equal(fs.lstatSync(file, { bigint: true }).ino, replacement.ino);
    if (boundary === "python-choice") assert.equal(h.f.pythonStubPath, "later-user-stubs");
    if (boundary === "hidden-python") assert.equal(h.f.vscode.workspace.getConfiguration().inspect().globalValue, "hidden-user-stubs");
    if (boundary === "selection-record") assert.equal(h.state.get(h.keys.stateKey).current, "later-user-stubs");
    if (boundary === "owner-record") assert.equal(h.state.get(stateKey(h.f.root)).phase, "unknown");
  });
}

test("normal release recovery preserves a foreign generic selection record", async t => {
  const h = await normalReleaseGeneration(t);
  h.f.configFile.onRequest(action => { if (action === "verify") throw new Error("interrupted normal release"); });
  await assert.rejects(h.managed.release(), /interrupted normal release/);
  h.f.configFile.restart(); h.f.configFile.onRequest(() => {});
  const foreign = { base: MANAGED_PATH, current: "later-user-stubs", currentPresent: true };
  await h.state.update(h.keys.stateKey, foreign);
  const original = fs.readFileSync(path.join(h.f.root, "pyproject.toml"));
  const owner = h.state.get(stateKey(h.f.root));
  await assert.rejects(createManagedProjectConfig(h.f.vscode, h.f.folder, h.state, h.f.worker), /generation ownership changed/);
  assert.deepEqual(h.state.get(h.keys.stateKey), foreign);
  assert.deepEqual(h.state.get(stateKey(h.f.root)), owner);
  assert.deepEqual(fs.readFileSync(path.join(h.f.root, "pyproject.toml")), original);
});

test("normal release recovery cannot clear a later owner record during native verification", async t => {
  const h = await normalReleaseGeneration(t);
  h.f.configFile.onRequest(action => { if (action === "verify") throw new Error("interrupted normal release"); });
  await assert.rejects(h.managed.release(), /interrupted normal release/);
  h.f.configFile.restart();
  const foreign = { ...h.state.get(stateKey(h.f.root)), phase: "unknown" };
  let changed = false;
  h.f.configFile.onRequest(async action => {
    if (action === "verify" && !changed) { changed = true; await h.state.update(stateKey(h.f.root), foreign); }
  });
  await assert.rejects(createManagedProjectConfig(h.f.vscode, h.f.folder, h.state, h.f.worker), /cleanup inputs changed/);
  assert.equal(changed, true);
  assert.deepEqual(h.state.get(stateKey(h.f.root)), foreign);
  assert.notEqual(h.state.get(h.keys.stateKey), undefined);
});
