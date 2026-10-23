"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { createHash } = require("node:crypto");
const { createAnalysisGenerations } = require("../src/analysisGenerations");

function fixture(t) {
  const workspace = fs.mkdtempSync(path.join(os.tmpdir(), "graphql-desk-generations-"));
  const root = path.join(workspace, ".graphql-desk", "typings");
  fs.mkdirSync(root, { recursive: true });
  fs.writeFileSync(path.join(root, "user.txt"), "keep");
  const owned = new Map();
  const vscode = { Uri: { file: value => ({ scheme: "file", fsPath: value, toString: () => value }) }, workspace: {
    isTrusted: true, textDocuments: [], getWorkspaceFolder: () => ({ uri: { fsPath: workspace } }),
  } };
  const managed = { ownsSetting: true, selectedSetting: ".graphql-desk/typings",
    ownsGeneration(directory, fingerprint) { return owned.get(path.basename(directory)) === fingerprint; },
    async recordGeneration(directory, fingerprint) { owned.set(path.basename(directory), fingerprint); return true; },
    async forgetGeneration(directory, fingerprint) { if (owned.get(path.basename(directory)) === fingerprint) owned.delete(path.basename(directory)); return true; },
    async selectGeneration(directory, isCurrent = () => true, verification) {
    if (isCurrent() !== true) return false;
    if (!this.ownsGeneration(directory, verification.manifestFingerprint)) return false;
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
const fingerprint = packages => createHash("sha256").update(JSON.stringify(
  [...packages].flatMap(([, values]) => values.map(({ relative_path, text }) => [relative_path, text])).sort(([a], [b]) => a.localeCompare(b)),
)).digest("hex");
const publish = (f, packages, id, isCurrent = () => true, inputFingerprint = fingerprint(packages)) =>
  f.publisher.publish(packages, id, isCurrent, f.managed, inputFingerprint);

test("publishes immutable complete generations and recovers the latest verified one", async t => {
  const f = fixture(t);
  assert.equal(await publish(f, files("str"), "analysis-1"), true);
  const first = path.resolve(f.workspace, f.managed.selectedSetting);
  assert.ok(fs.existsSync(path.join(first, "complete.json")));
  const firstBytes = fs.readFileSync(path.join(first, "graphql_desk_generated", "default.pyi"));

  assert.equal(await publish(f, files("int"), "analysis-2"), true);
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

test("deduplicates only when both captured inputs and generated stubs match", async t => {
  const f = fixture(t);
  const input1 = createHash("sha256").update("source/schema/config 1").digest("hex");
  const input2 = createHash("sha256").update("source/schema/config 2").digest("hex");
  assert.equal(await publish(f, files("str"), "analysis-1", () => true, input1), true);
  assert.equal(await publish(f, files("str"), "analysis-2", () => true, input1), false);
  assert.equal(await publish(f, files("int"), "analysis-3", () => true, input1), true);
  assert.equal(await publish(f, files("int"), "analysis-4", () => true, input2), true);
  const latest = f.publisher.current(f.managed)[0];
  assert.equal(latest.manifest.inputFingerprint, input2);
  assert.equal(latest.manifest.files[0].path, "graphql_desk_generated/_documents/default/d0.pyi");
});

test("serializes overlapping publication so stale output cannot win recovery ordering", async t => {
  const f = fixture(t);
  const record = f.managed.recordGeneration.bind(f.managed);
  let entered;
  const registrationStarted = new Promise(resolve => { entered = resolve; });
  let release;
  const registrationGate = new Promise(resolve => { release = resolve; });
  let first = true;
  f.managed.recordGeneration = async (...args) => {
    if (first) {
      first = false;
      entered();
      await registrationGate;
    }
    return record(...args);
  };

  let staleIsCurrent = true;
  const stale = publish(f, files("str"), "old-analysis", () => staleIsCurrent);
  await registrationStarted;
  const fresh = publish(f, files("int"), "new-analysis");
  staleIsCurrent = false;
  release();
  assert.equal(await stale, false);
  assert.equal(await fresh, true);
  const current = f.publisher.current(f.managed);
  assert.equal(current.length, 2);
  assert.equal(current[0].manifest.sequence, 2);
  assert.equal(current[0].manifest.analysisId, "new-analysis");

  f.managed.selectedSetting = ".graphql-desk/typings";
  assert.equal(await f.publisher.recover(f.managed), true);
  assert.equal(path.resolve(f.workspace, f.managed.selectedSetting), current[0].directory);
});

test("incomplete publication leaves the active complete generation selected", async t => {
  const f = fixture(t);
  await publish(f, files("str"), "analysis-1");
  const active = f.managed.selectedSetting;
  const rename = fs.renameSync;
  fs.renameSync = (source, destination) => {
    if (destination.endsWith("complete.json")) throw new Error("injected manifest commit failure");
    return rename(source, destination);
  };
  try { await assert.rejects(publish(f, files("int"), "analysis-2"), /injected manifest/); }
  finally { fs.renameSync = rename; }
  assert.equal(f.managed.selectedSetting, active);
  const generations = fs.readdirSync(path.join(f.root, "generations"));
  assert.equal(generations.length, 1, "uncommitted staging files are removed when ownership is still provable");
  assert.ok(fs.existsSync(path.join(f.root, "generations", generations[0], "complete.json")));
});

test("recovery falls back from a changed newest generation and preserves it", async t => {
  const f = fixture(t);
  await publish(f, files("str"), "analysis-1");
  const first = path.resolve(f.workspace, f.managed.selectedSetting);
  await publish(f, files("int"), "analysis-2");
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
  await publish(f, files("str"), "analysis-1");
  const first = path.resolve(f.workspace, f.managed.selectedSetting);
  const dirtyFile = path.join(first, "graphql_desk_generated", "default.pyi");
  f.vscode.workspace.textDocuments.push({ uri: f.vscode.Uri.file(dirtyFile), isDirty: true });
  await publish(f, files("int"), "analysis-2");
  await publish(f, files("float"), "analysis-3");
  assert.equal(fs.readFileSync(dirtyFile, "utf8").includes("selected: str"), true);
  assert.ok(fs.existsSync(path.join(first, "complete.json")));
});

test("retention preserves a generation containing an unlisted empty directory", async t => {
  const f = fixture(t);
  await publish(f, files("str"), "analysis-1");
  const first = path.resolve(f.workspace, f.managed.selectedSetting);
  const foreignDirectory = path.join(first, "user-data");
  fs.mkdirSync(foreignDirectory);
  await publish(f, files("int"), "analysis-2");
  await publish(f, files("float"), "analysis-3");
  assert.equal(fs.existsSync(path.join(first, "complete.json")), true);
  assert.equal(fs.existsSync(foreignDirectory), true);
  assert.deepEqual(fs.readdirSync(foreignDirectory), []);
});

test("an ancestor symlink swap prevents publication outside the workspace", async t => {
  const f = fixture(t);
  await publish(f, files("str"), "analysis-1");
  const active = path.resolve(f.workspace, f.managed.selectedSetting);
  const artifact = path.join(active, "graphql_desk_generated", "default.pyi");
  const originalBytes = fs.readFileSync(artifact);
  const outside = fs.mkdtempSync(path.join(os.tmpdir(), "graphql-desk-generation-outside-"));
  const movedRoot = path.join(outside, "moved-graphql-desk");
  const managedParent = path.join(f.workspace, ".graphql-desk");
  t.after(() => fs.rmSync(outside, { recursive: true, force: true }));
  fs.renameSync(managedParent, movedRoot);
  fs.symlinkSync(movedRoot, managedParent, process.platform === "win32" ? "junction" : "dir");
  await assert.rejects(publish(f, files("int"), "analysis-2"), /root changed/);
  assert.deepEqual(fs.readFileSync(artifact), originalBytes);
  assert.equal(fs.readdirSync(path.join(movedRoot, "typings", "generations")).length, 1);
});

test("preflight rejects a foreign generation entry before deleting any owned bytes", async t => {
  const f = fixture(t);
  await publish(f, files("str"), "analysis-1");
  const first = path.resolve(f.workspace, f.managed.selectedSetting);
  await publish(f, files("int"), "analysis-2");
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
  try { await publish(f, files("float"), "analysis-3"); }
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
  assert.equal(f.publisher.current(f.managed).length, 0);
  const duplicate = files("str").get("graphql_desk_generated");
  await assert.rejects(f.publisher.publish([
    ["graphql_desk_generated", duplicate], ["graphql_desk_generated", duplicate],
  ], "analysis-1", () => true, f.managed, fingerprint(files("str"))), /Invalid or oversized generation artifact/);
  assert.equal(fs.readFileSync(partialFile, "utf8"), "partial user-visible bytes");
  assert.equal(fs.readdirSync(path.join(f.root, "generations")).length, 1);
  assert.equal(await publish(f, files("str"), "analysis-2"), true);
  assert.equal(fs.readFileSync(partialFile, "utf8"), "partial user-visible bytes");
  assert.equal(fs.readdirSync(path.join(f.root, "generations")).length, 2);
});

test("forged complete generations without an extension ownership record are never adopted or deleted", async t => {
  const f = fixture(t);
  const id = "g-99-00000000-0000-0000-0000-000000000000";
  const directory = path.join(f.root, "generations", id);
  const artifactPath = "graphql_desk_generated/default.pyi";
  const artifact = path.join(directory, ...artifactPath.split("/"));
  fs.mkdirSync(path.dirname(artifact), { recursive: true });
  const bytes = Buffer.from(`# Generated by GraphQL Desk; analysis artifact ${"a".repeat(64)}\nselected: user\n`);
  fs.writeFileSync(artifact, bytes);
  const digest = value => createHash("sha256").update(value).digest("hex");
  const manifestFiles = [{ path: artifactPath, sha256: digest(bytes) }];
  const manifest = { owner: "graphql-desk-analysis-generation", format: 1, id, sequence: 99,
    analysisId: null, inputFingerprint: digest(Buffer.from("forged")),
    artifactFingerprint: digest(Buffer.from(JSON.stringify(manifestFiles))), files: manifestFiles };
  fs.writeFileSync(path.join(directory, "complete.json"), `${JSON.stringify(manifest, null, 2)}\n`);

  assert.equal(f.publisher.current(f.managed).length, 0);
  assert.equal(await f.publisher.recover(f.managed), false);
  assert.equal(f.managed.selectedSetting, ".graphql-desk/typings");
  assert.equal(await publish(f, files("str"), "analysis-1"), true);
  assert.equal(fs.readFileSync(artifact, "utf8"), bytes.toString("utf8"));
  assert.equal(fs.existsSync(path.join(directory, "complete.json")), true);
});
