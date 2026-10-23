"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { createAnalysisGenerations } = require("../src/analysisGenerations");

function fixture(t) {
  const workspace = fs.mkdtempSync(path.join(os.tmpdir(), "graphql-desk-generations-"));
  const root = path.join(workspace, ".graphql-desk", "typings");
  fs.mkdirSync(root, { recursive: true });
  fs.writeFileSync(path.join(root, "user.txt"), "keep");
  const vscode = { Uri: { file: value => ({ scheme: "file", fsPath: value, toString: () => value }) }, workspace: {
    isTrusted: true, textDocuments: [], getWorkspaceFolder: () => ({ uri: { fsPath: workspace } }),
  } };
  const managed = { ownsSetting: true, selectedSetting: ".graphql-desk/typings", async selectGeneration(directory, isCurrent = () => true) {
    if (isCurrent() !== true) return false;
    this.selectedSetting = path.relative(workspace, directory).split(path.sep).join("/");
    return true;
  } };
  t.after(() => fs.rmSync(workspace, { recursive: true, force: true }));
  return { workspace, root, vscode, managed, publisher: createAnalysisGenerations(vscode, workspace, root) };
}

const files = text => new Map([["graphql_desk_generated", [
  { relative_path: "graphql_desk_generated/default.pyi", text: `selected: ${text}\n` },
  { relative_path: "graphql_desk_generated/_documents/default/d0.pyi", text: `class Result: value: ${text}\n` },
]]]);

test("publishes immutable complete generations and recovers the latest verified one", async t => {
  const f = fixture(t);
  assert.equal(await f.publisher.publish(files("str"), "analysis-1", () => true, f.managed), true);
  const first = path.resolve(f.workspace, f.managed.selectedSetting);
  assert.ok(fs.existsSync(path.join(first, "complete.json")));
  const firstBytes = fs.readFileSync(path.join(first, "graphql_desk_generated", "default.pyi"));

  assert.equal(await f.publisher.publish(files("int"), "analysis-2", () => true, f.managed), true);
  const second = path.resolve(f.workspace, f.managed.selectedSetting);
  assert.notEqual(first, second);
  assert.deepEqual(fs.readFileSync(path.join(first, "graphql_desk_generated", "default.pyi")), firstBytes);
  assert.match(JSON.parse(fs.readFileSync(path.join(second, "complete.json"), "utf8")).owner, /graphql-desk/);

  const restarted = { ...f.managed, selectedSetting: ".graphql-desk/typings" };
  const afterRestart = createAnalysisGenerations(f.vscode, f.workspace, f.root);
  assert.equal(await afterRestart.recover(restarted), true);
  assert.equal(path.resolve(f.workspace, restarted.selectedSetting), second);
  assert.equal(fs.readFileSync(path.join(f.root, "user.txt"), "utf8"), "keep");
});

test("incomplete publication leaves the active complete generation selected", async t => {
  const f = fixture(t);
  await f.publisher.publish(files("str"), "analysis-1", () => true, f.managed);
  const active = f.managed.selectedSetting;
  const rename = fs.renameSync;
  fs.renameSync = (source, destination) => {
    if (destination.endsWith("complete.json")) throw new Error("injected manifest commit failure");
    return rename(source, destination);
  };
  try { await assert.rejects(f.publisher.publish(files("int"), "analysis-2", () => true, f.managed), /injected manifest/); }
  finally { fs.renameSync = rename; }
  assert.equal(f.managed.selectedSetting, active);
  const generations = fs.readdirSync(path.join(f.root, "generations"));
  assert.equal(generations.length, 1, "uncommitted staging files are removed when ownership is still provable");
  assert.ok(fs.existsSync(path.join(f.root, "generations", generations[0], "complete.json")));
});

test("recovery falls back from a changed newest generation and preserves it", async t => {
  const f = fixture(t);
  await f.publisher.publish(files("str"), "analysis-1", () => true, f.managed);
  const first = path.resolve(f.workspace, f.managed.selectedSetting);
  await f.publisher.publish(files("int"), "analysis-2", () => true, f.managed);
  const second = path.resolve(f.workspace, f.managed.selectedSetting);
  const artifact = path.join(second, "graphql_desk_generated", "default.pyi");
  fs.writeFileSync(artifact, "user bytes");
  const restarted = { ...f.managed, selectedSetting: path.relative(f.workspace, second).split(path.sep).join("/") };
  const recovery = createAnalysisGenerations(f.vscode, f.workspace, f.root);
  assert.equal(await recovery.recover(restarted), true);
  assert.equal(path.resolve(f.workspace, restarted.selectedSetting), first);
  assert.equal(fs.readFileSync(artifact, "utf8"), "user bytes");
});

test("retention never deletes a generation with dirty artifacts", async t => {
  const f = fixture(t);
  await f.publisher.publish(files("str"), "analysis-1", () => true, f.managed);
  const first = path.resolve(f.workspace, f.managed.selectedSetting);
  const dirtyFile = path.join(first, "graphql_desk_generated", "default.pyi");
  f.vscode.workspace.textDocuments.push({ uri: f.vscode.Uri.file(dirtyFile), isDirty: true });
  await f.publisher.publish(files("int"), "analysis-2", () => true, f.managed);
  await f.publisher.publish(files("float"), "analysis-3", () => true, f.managed);
  assert.equal(fs.readFileSync(dirtyFile, "utf8").includes("selected: str"), true);
  assert.ok(fs.existsSync(path.join(first, "complete.json")));
});

test("retention preserves a generation containing an unlisted empty directory", async t => {
  const f = fixture(t);
  await f.publisher.publish(files("str"), "analysis-1", () => true, f.managed);
  const first = path.resolve(f.workspace, f.managed.selectedSetting);
  const foreignDirectory = path.join(first, "user-data");
  fs.mkdirSync(foreignDirectory);
  await f.publisher.publish(files("int"), "analysis-2", () => true, f.managed);
  await f.publisher.publish(files("float"), "analysis-3", () => true, f.managed);
  assert.equal(fs.existsSync(path.join(first, "complete.json")), true);
  assert.equal(fs.existsSync(foreignDirectory), true);
  assert.deepEqual(fs.readdirSync(foreignDirectory), []);
});

test("an ancestor symlink swap prevents publication outside the workspace", async t => {
  const f = fixture(t);
  await f.publisher.publish(files("str"), "analysis-1", () => true, f.managed);
  const active = path.resolve(f.workspace, f.managed.selectedSetting);
  const artifact = path.join(active, "graphql_desk_generated", "default.pyi");
  const originalBytes = fs.readFileSync(artifact);
  const outside = fs.mkdtempSync(path.join(os.tmpdir(), "graphql-desk-generation-outside-"));
  const movedRoot = path.join(outside, "moved-graphql-desk");
  const managedParent = path.join(f.workspace, ".graphql-desk");
  t.after(() => fs.rmSync(outside, { recursive: true, force: true }));
  fs.renameSync(managedParent, movedRoot);
  fs.symlinkSync(movedRoot, managedParent, process.platform === "win32" ? "junction" : "dir");
  await assert.rejects(f.publisher.publish(files("int"), "analysis-2", () => true, f.managed), /root changed/);
  assert.deepEqual(fs.readFileSync(artifact), originalBytes);
  assert.equal(fs.readdirSync(path.join(movedRoot, "typings", "generations")).length, 1);
});

test("preflight rejects a foreign generation entry before deleting any owned bytes", async t => {
  const f = fixture(t);
  await f.publisher.publish(files("str"), "analysis-1", () => true, f.managed);
  const first = path.resolve(f.workspace, f.managed.selectedSetting);
  await f.publisher.publish(files("int"), "analysis-2", () => true, f.managed);
  const ownedFile = path.join(first, "graphql_desk_generated", "default.pyi");
  const ownedBefore = fs.readFileSync(ownedFile);
  const foreign = path.join(first, "foreign.txt");
  const readdir = fs.readdirSync;
  const rename = fs.renameSync;
  let committed = false;
  let injected = false;
  fs.renameSync = (source, destination) => {
    const result = rename(source, destination);
    if (destination.endsWith("complete.json")) committed = true;
    return result;
  };
  fs.readdirSync = (...args) => {
    const result = readdir(...args);
    if (committed && !injected && path.resolve(args[0]) === first) {
      injected = true;
      fs.writeFileSync(foreign, "keep foreign data");
    }
    return result;
  };
  try { await f.publisher.publish(files("float"), "analysis-3", () => true, f.managed); }
  finally { fs.readdirSync = readdir; fs.renameSync = rename; }
  assert.equal(injected, true);
  assert.deepEqual(fs.readFileSync(ownedFile), ownedBefore);
  assert.equal(fs.readFileSync(foreign, "utf8"), "keep foreign data");
  assert.ok(fs.existsSync(path.join(first, "complete.json")));
});

test("duplicate paths and abandoned incomplete generations are never adopted or overwritten", async t => {
  const f = fixture(t);
  const partial = path.join(f.root, "generations", "g-7-00000000-0000-0000-0000-000000000000");
  fs.mkdirSync(path.join(partial, "graphql_desk_generated"), { recursive: true });
  const partialFile = path.join(partial, "graphql_desk_generated", "default.pyi");
  fs.writeFileSync(partialFile, "partial user-visible bytes");
  assert.equal(f.publisher.current().length, 0);
  const duplicate = files("str").get("graphql_desk_generated");
  await assert.rejects(f.publisher.publish([
    ["graphql_desk_generated", duplicate], ["graphql_desk_generated", duplicate],
  ], "analysis-1", () => true, f.managed), /Invalid or oversized generation artifact/);
  assert.equal(fs.readFileSync(partialFile, "utf8"), "partial user-visible bytes");
  assert.equal(fs.readdirSync(path.join(f.root, "generations")).length, 1);
  assert.equal(await f.publisher.publish(files("str"), "analysis-2", () => true, f.managed), true);
  assert.equal(fs.readFileSync(partialFile, "utf8"), "partial user-visible bytes");
  assert.equal(fs.readdirSync(path.join(f.root, "generations")).length, 2);
});
