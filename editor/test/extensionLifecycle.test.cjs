"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { pathToFileURL } = require("node:url");
const { acquireManagedRoot, analysisRequestIsCurrent, artifactsWithRetry, dispatchConfigurationChange, operationDependencyImpact, presentDoctorReport, reconfigureWorkspaceRoots, serializeStubPathChange, typingFingerprint, verifyDoctorPublication, waitForRootReconfigurations } = require("../src/extension");
const { createManagedStubPath } = require("../src/managedStubPath");

for (const layer of ["workspaceValue", "globalValue"]) {
 for (const inFlight of [false, true]) {
  test(`configuration dispatch detects masked ${layer}${inFlight ? " during owned selection" : ""} even when every affects check is false`, async t => {
    const workspace = fs.mkdtempSync(path.join(os.tmpdir(), "hidden-stub-choice-"));
    t.after(() => fs.rmSync(workspace, { recursive: true, force: true }));
    const folder = { uri: { scheme: "file", fsPath: workspace } };
    const values = {};
    const stored = new Map();
    const state = { get: key => stored.get(key), update: async (key, value) => stored.set(key, value) };
    let release, entered;
    const held = new Promise(resolve => { release = resolve; });
    const started = new Promise(resolve => { entered = resolve; });
    let holding = inFlight;
    const configuration = { get: (_key, fallback) => values.workspaceFolderValue ?? values.workspaceValue ?? values.globalValue ?? fallback,
      inspect: () => ({ ...values }), update: async (_key, value) => {
        values.workspaceFolderValue = value;
        if (holding) { holding = false; entered(); await held; }
      } };
    const vscode = { workspace: { isTrusted: true, textDocuments: [], workspaceFolders: [folder], getConfiguration: () => configuration },
      ConfigurationTarget: { WorkspaceFolder: 2 } };
    const current = await createManagedStubPath(vscode, folder, state);
    const generation = path.join(current.root, "generations", "g-1-00000000-0000-0000-0000-000000000000");
    fs.mkdirSync(generation, { recursive: true });
    const verification = { directory: generation, manifest: { owner: "graphql-desk-analysis-generation", format: 2,
      id: path.basename(generation), artifactFingerprint: "a" }, manifestFingerprint: "b".repeat(64), verify() { return this; } };
    assert.equal(await current.recordGeneration(generation, verification.manifestFingerprint), true);
    const selecting = current.selectGeneration(generation, () => true, verification);
    if (inFlight) await Promise.race([started, selecting.then(() => assert.fail("the actual owned update must be held"))]);
    else assert.equal(await selecting, true);
    const roots = new Map([[workspace, Promise.resolve(current)]]);
    const selected = configuration.get();
    values[layer] = `${layer}-user-stubs`;
    assert.equal(configuration.get(), selected, "the new layer is hidden by the unchanged owned folder override");
    let handoffs = 0;
    const dispatched = dispatchConfigurationChange(roots, { affectsConfiguration: () => false }, async () => {
      handoffs += 1;
      await reconfigureWorkspaceRoots({ roots, folders: [folder],
        create: () => createManagedStubPath(vscode, folder, state),
        retire: async (_root, managed, reroute) => { assert.equal(reroute, true); await managed.releaseForRouting(); } });
      return true;
    }, () => assert.fail("no project settings changed"));
    if (inFlight) {
      try { await new Promise(resolve => setImmediate(resolve)); assert.equal(handoffs, 0); }
      finally { release(); }
      assert.equal(await selecting, true);
    }
    await dispatched;
    assert.equal(handoffs, 1, "a masked change still reaches the existing ownership/handoff path");
    assert.notEqual(await roots.get(workspace), current);
    assert.equal(current.ownsSetting, false);
    assert.equal(values[layer], `${layer}-user-stubs`);
    assert.equal(configuration.get(), `${layer}-user-stubs`, "handoff preserves the newly chosen lower layer");
  });
 }
}

test("configuration dispatch does no expensive work for unrelated current settings and retains project refresh", async () => {
  for (const mode of ["unrelated", "project", "owned-and-project", "rerouted-and-project"]) {
    const selected = Promise.resolve();
    const roots = new Map([["root", Promise.resolve({ waitForGenerationSelection: () => selected,
      routingSettingsCurrent: () => true, checkSettingOwnership: () => assert.fail("the cheap path cannot inspect config files") })]]);
    let stubCalls = 0;
    let projectCalls = 0;
    await dispatchConfigurationChange(roots, { affectsConfiguration: section =>
      section === "graphqlDesk" ? mode !== "unrelated" : mode.endsWith("-and-project") }, async () => {
      stubCalls += 1;
      return mode === "rerouted-and-project";
    }, () => { projectCalls += 1; });
    assert.equal(stubCalls, mode.endsWith("-and-project") ? 1 : 0);
    assert.equal(projectCalls, ["project", "owned-and-project"].includes(mode) ? 1 : 0);
  }
});

test("configuration dispatch waits for owned selection before checking the all-layer snapshot", async () => {
  let release;
  let finished = false;
  let checks = 0;
  const selection = new Promise(resolve => { release = resolve; });
  const roots = new Map([["root", Promise.resolve({ waitForGenerationSelection: () => selection,
    routingSettingsCurrent() { checks += 1; assert.equal(finished, true); return true; } })]]);
  const dispatch = dispatchConfigurationChange(roots, { affectsConfiguration: () => false },
    async () => assert.fail("an owned in-flight update cannot be mistaken for a user change"), () => assert.fail("unrelated event"));
  try { await new Promise(resolve => setImmediate(resolve)); assert.equal(checks, 0); }
  finally { finished = true; release(); }
  await dispatch;
  assert.equal(checks, 1);
});

test("doctor's final command handoff discards a report revoked after its awaited collection", async () => {
  for (const boundary of ["trust", "folder", "generation", "dispose", "none"]) {
    const folder = {};
    const vscode = { workspace: { isTrusted: true, workspaceFolders: [folder] } };
    const lines = [];
    const output = { appendLine: line => lines.push(line), show: () => lines.push("shown") };
    const report = { checks: [], runtime: null, versions: {}, exitCode: 0 };
    let current = true;
    let resolve;
    const pending = new Promise(done => { resolve = done; });
    const command = (async () => presentDoctorReport(vscode, await pending, folder, () => current, output))();
    resolve(report);
    if (boundary === "trust") vscode.workspace.isTrusted = false;
    else if (boundary === "folder") vscode.workspace.workspaceFolders = [];
    else if (["generation", "dispose"].includes(boundary)) current = false;
    const observed = await command;
    if (boundary === "none") { assert.equal(observed, report); assert.equal(lines.length, 2); }
    else { assert.equal(observed.exitCode, 1); assert.equal(observed.runtime, null); assert.equal(lines.length, 0); }
  }
});

test("doctor ownership needs the live publisher's exact selected proof and current inputs", async () => {
  const workspace = path.resolve("doctor-project");
  const publication = { inputFingerprint: "current-input" };
  const managed = { selectedSetting: ".graphql-desk/typings/stable", checkSettingOwnership: async check => check() };
  const publisher = { verification: () => ({ directory: path.join(workspace, managed.selectedSetting),
    manifest: { inputFingerprint: "current-input" } }) };
  const verify = (owner = managed, source = publisher, current = () => true) =>
    verifyDoctorPublication(owner, source, publication, workspace, current);
  assert.deepEqual(await verify(), { ownership: true, current: true });
  assert.deepEqual(await verify(managed, { verification: () => ({ ...publisher.verification(),
    manifest: { inputFingerprint: "old-input" } }) }), { ownership: true, current: false });
  for (const [owner, source, current] of [
    [managed, {}, () => true],
    [{ ...managed, checkSettingOwnership: async () => false }, publisher, () => true],
    [managed, { verification: () => ({ ...publisher.verification(), directory: workspace }) }, () => true],
    [managed, publisher, () => false],
  ]) assert.deepEqual(await verify(owner, source, current), { ownership: false, current: false });
  let live = true;
  assert.deepEqual(await verify(managed, { verification: () => { live = false; return publisher.verification(); } }, () => live),
    { ownership: false, current: false });
  await assert.rejects(verify(managed, { verification() { throw new Error("foreign file changed"); } }), /foreign file changed/);
});

test("superseded typing tickets cannot recover or publish generated types", () => {
  const result = { isCurrent: () => true };
  assert.equal(analysisRequestIsCurrent(4, 4, false, result), true);
  assert.equal(analysisRequestIsCurrent(4, 5, false, result), false, "a newer refresh supersedes the old request");
  assert.equal(analysisRequestIsCurrent(4, 4, true, result), false, "disposed extensions cannot publish");
  assert.equal(analysisRequestIsCurrent(4, 4, false, { isCurrent: () => false }), false,
    "stale worker snapshots remain rejected");
});

test("stale artifact replies retry once when the publish is still current", async () => {
  let calls = 0;
  const skipped = [];
  const result = await artifactsWithRetry({ artifacts: async (_snapshot, _token, onFailure) => {
    calls += 1;
    if (calls === 1) { onFailure("worker-artifacts-stale"); return null; }
    return { ok: true };
  } }, {}, undefined, reason => skipped.push(reason), () => true);
  assert.deepEqual(result, { ok: true });
  assert.equal(calls, 2);
  assert.deepEqual(skipped, ["worker-artifacts-stale"]);

  calls = 0;
  const stillStale = await artifactsWithRetry({ artifacts: async (_snapshot, _token, onFailure) => {
    calls += 1;
    onFailure("worker-artifacts-stale");
    return null;
  } }, {}, undefined, () => {}, () => true);
  assert.equal(stillStale, null);
  assert.equal(calls, 2, "a second stale reply does not create an unbounded retry");

  calls = 0;
  const cancelled = await artifactsWithRetry({ artifacts: async (_snapshot, _token, onFailure) => {
    calls += 1;
    onFailure("worker-artifacts-stale");
    return null;
  } }, {}, undefined, () => {}, () => false);
  assert.equal(cancelled, null);
  assert.equal(calls, 1, "cancelled or superseded typing does not retry");
});

test("typing fingerprint follows Pylance stub inputs and ignores runtime-only changes", () => {
  const files = [
    { relative_path: "graphql_desk_generated/default.py", text: "runtime = 1" },
    { relative_path: "graphql_desk_generated/default.pyi", text: "value: str" },
    { relative_path: "graphql_desk-stubs/py.typed", text: "partial\n" },
  ];
  const baseline = typingFingerprint([["graphql_desk_generated", files.slice(0, 2)], ["graphql_desk-stubs", files.slice(2)]]);
  assert.equal(typingFingerprint([["graphql_desk-stubs", files.slice(2)], ["graphql_desk_generated", [...files.slice(0, 2)].reverse()]]), baseline,
    "group and file order do not trigger a language-server restart");
  assert.notEqual(typingFingerprint([["graphql_desk_generated", [files[0], { ...files[1], text: "value: int" }]], ["graphql_desk-stubs", files.slice(2)]]), baseline,
    "a changed typed declaration does trigger a restart");
  assert.notEqual(typingFingerprint([["graphql_desk_generated", files.slice(0, 2)], ["graphql_desk-stubs", [{ ...files[2], text: "full\n" }]]]), baseline,
    "the partial stub marker is a Pylance input");
});

test("schema and config deltas target only operations with changed provenance facts", () => {
  const operation = (binding, coordinate, fingerprint, config = []) => ({
    owner: { module: { project: "project", module: "consumer", uri: "file:///consumer.py" }, codepoint: { start: binding.length, end: binding.length + 1 } },
    binding, selector: binding, operation_name: binding, operation_kind: "query", status: "precise",
    source_dependencies_known: true, mapping_dependencies: [],
    schema_identity: ["project", "default"], context_token: "schema-context",
    schema_dependencies: [{ coordinate, fingerprint }], config_dependencies: config,
  });
  const before = [
    { project: "project", generated_package: "graphql_desk_generated.default", operations: [
      operation("selected", "field:Query.selected", "a".repeat(64), [{ kind: "scalar-output", name: "String", value: "builtins.str" }]),
      operation("id", "field:Query.id", "b".repeat(64)),
    ] },
  ];
  const after = [
    { ...before[0], operations: [
      { ...operation("selected", "field:Query.selected", "c".repeat(64), [{ kind: "scalar-output", name: "String", value: "builtins.int" }]), context_token: "next-snapshot" },
      { ...operation("id", "field:Query.id", "b".repeat(64)), context_token: "next-snapshot" },
    ] },
  ];

  assert.deepEqual(operationDependencyImpact(before, after), [before[0].operations[0].owner]);
  const fragment = { module: { project: "project", module: "fragments", uri: "file:///fragments.py", version: 1 },
    codepoint: { start: 0, end: 20 }, utf16: { start: 0, end: 20 } };
  const sourceBefore = [{ ...before[0], operations: [
    { ...before[0].operations[0], direct_fragments: [fragment], dependencies: [fragment.module] }, before[0].operations[1],
  ] }];
  const sourceAfter = [{ ...sourceBefore[0], operations: [
    { ...sourceBefore[0].operations[0], direct_fragments: [{ ...fragment,
      module: { ...fragment.module, version: 2 } }], dependencies: [{ ...fragment.module, version: 2 }] },
    sourceBefore[0].operations[1],
  ] }];
  assert.deepEqual(operationDependencyImpact(sourceBefore, sourceAfter), [sourceBefore[0].operations[0].owner],
    "a source dependency change during broad invalidation must keep its old declaration quarantined");
  assert.deepEqual(operationDependencyImpact(before, [{ ...after[0], operations: [
    { ...after[0].operations[0], schema_dependencies: "unknown" }, after[0].operations[1],
  ] }]), [before[0].operations[0].owner],
  "unknown facts quarantine only their operation when the sibling has complete unchanged provenance");
  assert.equal(operationDependencyImpact(before, [{ ...after[0], operations: "unknown" }]), null,
    "a malformed project manifest keeps the whole project fail-closed");
  assert.deepEqual(operationDependencyImpact(before, [{ ...after[0], operations: [after[0].operations[1]] }]),
    [before[0].operations[0].owner], "a disappeared operation does not quarantine a proven unaffected sibling");
  assert.deepEqual(operationDependencyImpact(before, [{ ...after[0], operations: [
    after[0].operations[0], after[0].operations[0], after[0].operations[1],
  ] }]), [before[0].operations[0].owner],
  "ambiguous matches quarantine their owner without widening to a proven sibling");
});

test("operation impact matches Windows paths that differ only by case", t => {
  if (process.platform !== "win32") return t.skip("Windows canonical path behavior");
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "graphql-desk-path-key-"));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const source = path.join(root, "Consumer.py");
  const project = path.join(root, "pyproject.toml");
  fs.writeFileSync(source, "");
  fs.writeFileSync(project, "");
  const differentlyCased = value => value.replace(/[a-z]/g, letter => letter.toUpperCase());
  const operation = (sourcePath, projectPath) => ({
    owner: { module: { project: pathToFileURL(projectPath).href, module: "consumer", uri: pathToFileURL(sourcePath).href } },
    binding: "doc", selector: "doc", operation_name: "Pick", operation_kind: "query", status: "precise",
    source_dependencies_known: true, mapping_dependencies: [], schema_identity: [pathToFileURL(projectPath).href, "default"],
    context_token: "same", schema_dependencies: [], config_dependencies: [],
  });
  const before = [{ project: "project", generated_package: "generated", operations: [operation(source, project)] }];
  const after = [{ ...before[0], operations: [operation(differentlyCased(source), differentlyCased(project))] }];
  assert.deepEqual(operationDependencyImpact(before, after), [],
    "path aliases for the same source and project must not force broad quarantine");
});

test("a stubPath conflict retires only its workspace and reconfigures the remaining roots", async () => {
  const firstPath = path.resolve("workspace-one");
  const secondPath = path.resolve("workspace-two");
  const firstFolder = { uri: { fsPath: firstPath } };
  const secondFolder = { uri: { fsPath: secondPath } };
  const firstManaged = { root: path.join(firstPath, "typings"), checkSettingOwnership: async () => false, cleanup() {} };
  const secondManaged = { root: path.join(secondPath, "typings"), checkSettingOwnership: async () => false, cleanup() {} };
  const nextSecond = { root: path.join(secondPath, "custom"), cleanup() {} };
  const roots = new Map([[firstPath, Promise.resolve(firstManaged)], [secondPath, Promise.resolve(secondManaged)]]);
  const retired = [];
  await assert.rejects(reconfigureWorkspaceRoots({
    roots,
    folders: [firstFolder, secondFolder],
    create: async folder => {
      if (folder === firstFolder) throw new Error("external stubPath conflict");
      return nextSecond;
    },
    retire: async (root, managed) => retired.push([root, managed]),
  }), AggregateError);
  assert.deepEqual(retired, [[firstPath, firstManaged], [secondPath, secondManaged]]);
  assert.equal(roots.has(firstPath), false, "conflicting workspace is disabled");
  assert.equal(await roots.get(secondPath), nextSecond, "other workspace still receives its new root");
});

test("reconfiguration reuses a root that still owns its setting", async () => {
  const root = path.resolve("active-workspace");
  const folder = { uri: { fsPath: root } };
  let created = false;
  let retired = false;
  const managed = { root: path.join(root, "typings"), checkSettingOwnership: async () => true, cleanup() {} };
  const roots = new Map([[root, Promise.resolve(managed)]]);

  await reconfigureWorkspaceRoots({
    roots,
    folders: [folder],
    create: async () => { created = true; return { ...managed }; },
    retire: async () => { retired = true; },
  });

  assert.equal(created, false);
  assert.equal(retired, false);
  assert.equal(await roots.get(root), managed);
});

test("an effective config route change releases the cached owned root before creating its replacement", async () => {
  const root = path.resolve("late-config-workspace");
  const folder = { uri: { fsPath: root } };
  const order = [];
  const current = { root: path.join(root, "old"), ownsSetting: true,
    checkSettingOwnership: async () => true, releaseForRouting() {}, cleanup() {} };
  const replacement = { root: path.join(root, "new"), ownsSetting: true,
    checkSettingOwnership: async () => true, releaseForRouting() {}, cleanup() {} };
  const roots = new Map([[root, Promise.resolve(current)]]);
  let release, started;
  const gate = new Promise(resolve => { release = resolve; });
  const entered = new Promise(resolve => { started = resolve; });
  const reconfiguration = reconfigureWorkspaceRoots({ roots, folders: [folder], routeCurrent: async (_folder, managed) => managed === replacement,
    retire: async (_root, managed, reroute) => { assert.equal(reroute, true); assert.equal(managed, current);
      order.push("release"); started(); await gate; order.push("released"); },
    create: async () => { order.push("create"); return replacement; } });
  await Promise.race([entered, reconfiguration.then(() => assert.fail("an owned root still needs rerouting"))]);
  const acquisition = acquireManagedRoot(roots, folder, root, [], async () => assert.fail("use the replacement"));
  try {
    await new Promise(resolve => setImmediate(resolve));
    assert.deepEqual(order, ["release"]);
    assert.equal(roots.has(root), false, "no publication may obtain a retiring root");
  } finally { release(); }
  assert.equal(await acquisition, replacement);
  await reconfiguration;
  assert.deepEqual(order, ["release", "released", "create"]);
});

test("dirty or changed routing inputs cannot authorize a replacement, while another workspace can progress", async () => {
  const root = path.resolve("dirty-routing-workspace");
  const other = path.resolve("clean-routing-workspace");
  const current = { root, checkSettingOwnership: async () => true, cleanup() {} };
  const replacement = { root: other, checkSettingOwnership: async () => true, cleanup() {} };
  const roots = new Map([[root, Promise.resolve(current)], [other, Promise.resolve(replacement)]]);
  await assert.rejects(reconfigureWorkspaceRoots({ roots, folders: [root, other].map(fsPath => ({ uri: { fsPath } })),
    routeCurrent: async folder => { if (folder.uri.fsPath === root) throw new Error("dirty config"); return true; },
    create: async () => assert.fail("no replacement can be authorized"), retire: async () => assert.fail("preserve the existing root") }), AggregateError);
  assert.equal(await roots.get(root), current);
  assert.equal(await roots.get(other), replacement);
});

test("same-root reconfiguration retains the fresh manager without releasing shared settings", async () => {
  const root = path.resolve("same-root-workspace");
  const folder = { uri: { fsPath: root } };
  const directory = path.join(root, ".graphql-desk", "live-typings");
  const abandoned = [];
  const current = { root: directory, checkSettingOwnership: async () => false, release: () => assert.fail("stale manager must not release shared settings"), cleanup() {} };
  const replacement = { root: directory, checkSettingOwnership: async () => true, cleanup() {} };
  const roots = new Map([[root, Promise.resolve(current)]]);

  await reconfigureWorkspaceRoots({ roots, folders: [folder], create: async () => replacement,
    retire: async () => assert.fail("same-root replacement must not release settings"),
    abandon: managed => abandoned.push(managed) });

  assert.equal(await roots.get(root), replacement);
  assert.deepEqual(abandoned, [current], "stale cleanup is deferred until active outputs are retired");
});

test("root reconfiguration waits for in-flight generation selection before replacing its manager", async () => {
  const root = path.resolve("rotating-workspace");
  const folder = { uri: { fsPath: root } };
  let finishSelection;
  let selectionStarted;
  const selection = new Promise(resolve => { finishSelection = resolve; });
  const started = new Promise(resolve => { selectionStarted = resolve; });
  let created = false;
  const current = { root: path.join(root, ".graphql-desk", "live-typings"),
    async checkSettingOwnership() { selectionStarted(); await selection; assert.equal(selectionFinished, true); return false; }, cleanup() {} };
  const replacement = { root: path.join(root, "custom"), cleanup() {} };
  const roots = new Map([[root, Promise.resolve(current)]]);
  let selectionFinished = false;
  const reconfiguration = reconfigureWorkspaceRoots({ roots, folders: [folder],
    create: async () => { created = true; return replacement; }, retire: async () => {} });

  await started;
  assert.equal(created, false, "a manager cannot be replaced during its setting rotation");
  selectionFinished = true;
  finishSelection();
  await reconfiguration;
  assert.equal(created, true);
  assert.equal(await roots.get(root), replacement);
});

test("overlapping stubPath changes reconcile in order", async () => {
  const roots = new Map();
  const order = [];
  let finishFirst;
  let firstStarted;
  const firstGate = new Promise(resolve => { finishFirst = resolve; });
  const started = new Promise(resolve => { firstStarted = resolve; });
  const first = serializeStubPathChange(roots, async () => {
    order.push("first-start");
    firstStarted();
    await firstGate;
    order.push("first-end");
  });
  await started;
  const second = serializeStubPathChange(roots, async () => { order.push("second"); });
  assert.deepEqual(order, ["first-start"], "the later event cannot inspect a stale root during the earlier reconciliation");
  finishFirst();
  await Promise.all([first, second]);
  assert.deepEqual(order, ["first-start", "first-end", "second"]);
});

test("new typing acquisition waits for an in-flight root replacement", async () => {
  const root = path.resolve("acquire-during-reconfiguration");
  const folder = { uri: { fsPath: root } };
  const validated = [];
  const current = { root: path.join(root, "old"), checkSettingOwnership: async () => false,
    validateGeneratedPackages: async () => validated.push("old"), cleanup() {} };
  const replacement = { root: path.join(root, "new"),
    validateGeneratedPackages: async packages => validated.push(packages), cleanup() {} };
  const roots = new Map([[root, Promise.resolve(current)]]);
  let releaseCreate;
  let startedCreate;
  const creating = new Promise(resolve => { releaseCreate = resolve; });
  const started = new Promise(resolve => { startedCreate = resolve; });
  const reconfiguration = reconfigureWorkspaceRoots({ roots, folders: [folder], create: async () => {
    startedCreate(); await creating; return replacement;
  }, retire: async () => {} });
  await started;
  let acquired = false;
  const acquisition = acquireManagedRoot(roots, folder, root, ["generated"], async () => assert.fail("use the replacement"))
    .then(managed => { acquired = true; return managed; });
  try {
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(acquired, false, "a new typing ticket cannot obtain the retiring manager");
    assert.deepEqual(validated, [], "the old manager cannot authorize another publication");
  } finally { releaseCreate(); }
  assert.equal(await acquisition, replacement);
  await reconfiguration;
  assert.deepEqual(validated, [["generated"]]);
});

test("typing acquisition drains root replacements queued while it is waiting", async () => {
  const root = path.resolve("acquire-during-queued-reconfiguration");
  const folder = { uri: { fsPath: root } };
  const validated = [];
  const current = { root: path.join(root, "old"), checkSettingOwnership: async () => false, cleanup() {} };
  const firstReplacement = { root: path.join(root, "first"), checkSettingOwnership: async () => false,
    validateGeneratedPackages: async () => validated.push("first"), cleanup() {} };
  const finalReplacement = { root: path.join(root, "final"),
    validateGeneratedPackages: async () => validated.push("final"), cleanup() {} };
  const roots = new Map([[root, Promise.resolve(current)]]);
  let releaseFirst, releaseSecond, startedFirst, startedSecond;
  const firstGate = new Promise(resolve => { releaseFirst = resolve; });
  const secondGate = new Promise(resolve => { releaseSecond = resolve; });
  const firstStarted = new Promise(resolve => { startedFirst = resolve; });
  const secondStarted = new Promise(resolve => { startedSecond = resolve; });
  const first = reconfigureWorkspaceRoots({ roots, folders: [folder], create: async () => {
    startedFirst(); await firstGate; return firstReplacement;
  }, retire: async () => {} });
  await firstStarted;
  let acquired = false;
  const acquisition = acquireManagedRoot(roots, folder, root, [], async () => assert.fail("use the final replacement"))
    .then(managed => { acquired = true; return managed; });
  const second = reconfigureWorkspaceRoots({ roots, folders: [folder], create: async () => {
    startedSecond(); await secondGate; return finalReplacement;
  }, retire: async () => {} });
  releaseFirst();
  await secondStarted;
  try {
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(acquired, false, "a subsequently queued replacement must finish too");
    assert.deepEqual(validated, []);
  } finally { releaseSecond(); }
  assert.equal(await acquisition, finalReplacement);
  await Promise.all([first, second]);
  assert.deepEqual(validated, ["final"]);
});

test("typing acquisition rejects a removed folder after waiting for retirement", async () => {
  const root = path.resolve("acquire-after-folder-removal");
  const folder = { uri: { fsPath: root } };
  const current = { root: path.join(root, "old"), checkSettingOwnership: async () => true, cleanup() {} };
  const roots = new Map([[root, Promise.resolve(current)]]);
  let releaseRetire, startedRetire;
  const retiring = new Promise(resolve => { releaseRetire = resolve; });
  const started = new Promise(resolve => { startedRetire = resolve; });
  const reconfiguration = reconfigureWorkspaceRoots({ roots, folders: [],
    create: async () => assert.fail("removed folders are not reconfigured"),
    retire: async () => { startedRetire(); await retiring; } });
  await started;
  let checkedCurrentFolder = false;
  const acquisition = acquireManagedRoot(roots, folder, root, [], async () => {
    checkedCurrentFolder = true;
    throw new Error("Trusted current workspace folder required");
  });
  const rejection = assert.rejects(acquisition, /Trusted current workspace folder required/);
  assert.equal(checkedCurrentFolder, false, "creation waits until retirement has finished");
  releaseRetire();
  await Promise.all([reconfiguration, rejection]);
  assert.equal(checkedCurrentFolder, true, "a cache miss still requires the creator's current-folder check");
  assert.equal(roots.has(root), false, "a rejected stale folder does not leave a cached manager");
});

test("concurrent root reconfigurations create and retire a replacement only once", async () => {
  const root = path.resolve("concurrent-workspace");
  const folder = { uri: { fsPath: root } };
  const current = { root: path.join(root, "old"), checkSettingOwnership: async () => false, cleanup() {} };
  const replacement = { root: path.join(root, "new"), checkSettingOwnership: async () => true, cleanup() {} };
  const roots = new Map([[root, Promise.resolve(current)]]);
  const retired = [];
  let releaseCreate;
  let startedCreate;
  const createStarted = new Promise(resolve => { startedCreate = resolve; });
  const creating = new Promise(resolve => { releaseCreate = resolve; });
  let creates = 0;
  const create = async () => { creates += 1; startedCreate(); await creating; return replacement; };
  const retire = async (_root, managed) => retired.push(managed);
  const first = reconfigureWorkspaceRoots({ roots, folders: () => [folder], create, retire });
  await createStarted;
  const second = reconfigureWorkspaceRoots({ roots, folders: () => [folder], create, retire });
  releaseCreate();
  await Promise.all([first, second]);
  assert.equal(creates, 1);
  assert.deepEqual(retired, [current]);
  assert.equal(await roots.get(root), replacement);
});

test("queued root reconfiguration uses the latest workspace list and shutdown can drain it", async () => {
  const root = path.resolve("removed-during-reconfiguration");
  const folder = { uri: { fsPath: root } };
  const current = { root: path.join(root, "old"), checkSettingOwnership: async () => false, cleanup() {} };
  const replacement = { root: path.join(root, "new"), checkSettingOwnership: async () => false, cleanup() {} };
  const roots = new Map([[root, Promise.resolve(current)]]);
  const folders = [folder];
  const retired = [];
  let releaseCreate;
  let startedCreate;
  const createStarted = new Promise(resolve => { startedCreate = resolve; });
  const creating = new Promise(resolve => { releaseCreate = resolve; });
  const first = reconfigureWorkspaceRoots({ roots, folders: () => folders, create: async () => {
    startedCreate(); await creating; return replacement;
  }, retire: async (_root, managed) => retired.push(managed) });
  await createStarted;
  folders.length = 0;
  const second = reconfigureWorkspaceRoots({ roots, folders: () => folders, create: async () => assert.fail("removed workspaces are not recreated"),
    retire: async (_root, managed) => retired.push(managed) });
  let drained = false;
  const drain = waitForRootReconfigurations(roots).then(() => { drained = true; });
  await Promise.resolve();
  assert.equal(drained, false);
  releaseCreate();
  await Promise.all([first, second, drain]);
  assert.deepEqual(retired, [current, replacement]);
  assert.equal(roots.has(root), false);
  assert.equal(drained, true);
});

test("removing a workspace retires its managed root and leaves other roots active", async () => {
  const removedPath = path.resolve("removed-workspace");
  const activePath = path.resolve("active-workspace");
  const removed = { root: path.join(removedPath, "typings"), checkSettingOwnership: async () => false, cleanup() {} };
  const active = { root: path.join(activePath, "typings"), checkSettingOwnership: async () => false, cleanup() {} };
  const replacement = { root: path.join(activePath, "custom"), cleanup() {} };
  const roots = new Map([[removedPath, Promise.resolve(removed)], [activePath, Promise.resolve(active)]]);
  const retired = [];

  await reconfigureWorkspaceRoots({
    roots,
    folders: [{ uri: { fsPath: activePath } }],
    create: async () => replacement,
    retire: async (root, managed) => retired.push([root, managed]),
  });

  assert.deepEqual(retired, [[removedPath, removed], [activePath, active]]);
  assert.equal(roots.has(removedPath), false);
  assert.equal(await roots.get(activePath), replacement);
});

test("workspace removal waits for an in-flight generation selection before retiring its root", async () => {
  const root = path.resolve("removed-during-generation-selection");
  let finishSelection;
  let selectionStarted;
  const selection = new Promise(resolve => { finishSelection = resolve; });
  const started = new Promise(resolve => { selectionStarted = resolve; });
  let retired = false;
  const managed = { root: path.join(root, "typings"), async checkSettingOwnership() {
    selectionStarted();
    await selection;
    return false;
  }, cleanup() {} };
  const roots = new Map([[root, Promise.resolve(managed)]]);
  const reconfiguration = reconfigureWorkspaceRoots({ roots, folders: [], create: async () => assert.fail("removed workspace is not recreated"),
    retire: async () => { retired = true; } });

  const first = await Promise.race([started.then(() => "selection"), reconfiguration.then(() => "retired")]);
  assert.equal(first, "selection", "ownership check must drain selection before removed-root cleanup");
  assert.equal(retired, false);
  finishSelection();
  await reconfiguration;
  assert.equal(retired, true);
});
