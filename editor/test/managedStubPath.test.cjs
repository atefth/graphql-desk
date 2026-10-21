"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs/promises");
const os = require("node:os");
const path = require("node:path");
const { createManagedStubPath } = require("../src/managedStubPath");

async function fixture(t, setting, dirty = false) {
  const workspace = await fs.mkdtemp(path.join(os.tmpdir(), "graphql-desk-stub-path-"));
  const settingsPath = path.join(workspace, ".vscode", "settings.json");
  await fs.mkdir(path.dirname(settingsPath));
  const settings = { "editor.tabSize": 2 };
  await fs.writeFile(settingsPath, JSON.stringify(settings));
  const values = { "analysis.stubPath": setting };
  const updates = [];
  const config = {
    get: (_key, fallback) => values["analysis.stubPath"] ?? fallback,
    inspect: () => ({ defaultValue: "./typings", workspaceFolderValue: values["analysis.stubPath"] }),
    async update(key, value, target) {
      updates.push([key, value, target]);
      assert.equal(key, "analysis.stubPath");
      assert.equal(target, 2);
      values[key] = value;
      settings[key] = value;
      await fs.writeFile(settingsPath, JSON.stringify(settings));
    },
  };
  const vscode = { ConfigurationTarget: { WorkspaceFolder: 2 }, workspace: {
    isTrusted: true, workspaceFile: null,
    textDocuments: dirty ? [{ uri: { scheme: "file", fsPath: settingsPath }, isDirty: true }] : [],
    getConfiguration: () => config,
  } };
  const folder = { uri: { scheme: "file", fsPath: workspace } };
  t.after(() => fs.rm(workspace, { recursive: true, force: true }));
  return { workspace, settings, settingsPath, vscode, folder, values, updates };
}

test("sets the managed workspace stubPath without dropping other settings and removes only empty created parents", async t => {
  const f = await fixture(t, undefined);
  const managed = await createManagedStubPath(f.vscode, f.folder);
  assert.equal(managed.root, path.join(f.workspace, ".graphql-desk", "typings"));
  assert.equal(f.values["analysis.stubPath"], ".graphql-desk/typings");
  assert.equal(f.settings["editor.tabSize"], 2);
  await fs.writeFile(path.join(managed.root, "keep.txt"), "user data");
  managed.cleanup();
  assert.equal(await fs.readFile(path.join(managed.root, "keep.txt"), "utf8"), "user data");
  assert.equal(await fs.stat(path.join(f.workspace, ".graphql-desk" )).then(() => true), true);
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
