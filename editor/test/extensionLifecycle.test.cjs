"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { pathToFileURL } = require("node:url");
const { artifactsWithRetry, operationDependencyImpact, reconfigureWorkspaceRoots, typingFingerprint, waitForRootReconfigurations } = require("../src/extension");

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
  const firstManaged = { root: path.join(firstPath, "typings"), cleanup() {} };
  const secondManaged = { root: path.join(secondPath, "typings"), cleanup() {} };
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
  const managed = { root: path.join(root, "typings"), stillOwnsSetting: () => true, cleanup() {} };
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

test("concurrent root reconfigurations create and retire a replacement only once", async () => {
  const root = path.resolve("concurrent-workspace");
  const folder = { uri: { fsPath: root } };
  const current = { root: path.join(root, "old"), stillOwnsSetting: () => false, cleanup() {} };
  const replacement = { root: path.join(root, "new"), stillOwnsSetting: () => true, cleanup() {} };
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
  const current = { root: path.join(root, "old"), stillOwnsSetting: () => false, cleanup() {} };
  const replacement = { root: path.join(root, "new"), cleanup() {} };
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
  const removed = { root: path.join(removedPath, "typings"), cleanup() {} };
  const active = { root: path.join(activePath, "typings"), cleanup() {} };
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
