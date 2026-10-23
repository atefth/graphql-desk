"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const path = require("node:path");
const { reconfigureWorkspaceRoots } = require("../src/extension");

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
