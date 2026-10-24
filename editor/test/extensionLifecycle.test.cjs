"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const path = require("node:path");
const { operationDependencyImpact, reconfigureWorkspaceRoots } = require("../src/extension");

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
      operation("selected", "field:Query.selected", "c".repeat(64), [{ kind: "scalar-output", name: "String", value: "builtins.int" }]),
      operation("id", "field:Query.id", "b".repeat(64)),
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
  assert.equal(operationDependencyImpact(before, [{ ...after[0], operations: [
    { ...after[0].operations[0], schema_dependencies: "unknown" }, after[0].operations[1],
  ] }]), null, "unknown authoritative facts require fail-closed project handling");
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
