"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { createHash } = require("node:crypto");
const { createAnalysisGenerations, validOperationManifest } = require("../src/analysisGenerations");
const { renderOwnedAnalysisArtifact } = require("../src/ownedAnalysisArtifacts");

function fixture(t) {
  const workspace = fs.mkdtempSync(path.join(os.tmpdir(), "graphql-desk-generations-"));
  const root = path.join(workspace, ".graphql-desk", "typings");
  fs.mkdirSync(root, { recursive: true });
  fs.writeFileSync(path.join(root, "user.txt"), "keep");
  const owned = new Map();
  const stagingOwned = new Map();
  const vscode = { Uri: { file: value => ({ scheme: "file", fsPath: value, toString: () => value }) }, workspace: {
    isTrusted: true, textDocuments: [], getWorkspaceFolder: () => ({ uri: { fsPath: workspace } }),
  } };
  const managed = { ownsSetting: true, selectedSetting: ".graphql-desk/typings",
    ownsGeneration(directory, fingerprint) { return owned.get(path.basename(directory)) === fingerprint; },
    async recordGeneration(directory, fingerprint) { owned.set(path.basename(directory), fingerprint); return true; },
    async forgetGeneration(directory, fingerprint) { if (owned.get(path.basename(directory)) === fingerprint) owned.delete(path.basename(directory)); return true; },
    ownsStaging(directory, fingerprint) { return stagingOwned.get(path.basename(directory))?.fingerprint === fingerprint; },
    async recordStaging(directory, fingerprint, markerTemporary) { stagingOwned.set(path.basename(directory), { fingerprint, markerTemporary }); return this.ownsStaging(directory, fingerprint); },
    async forgetStaging(directory, fingerprint) { if (stagingOwned.get(path.basename(directory))?.fingerprint === fingerprint) stagingOwned.delete(path.basename(directory)); return true; },
    stagingEntries() { return Object.fromEntries(stagingOwned); },
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
  { relative_path: "graphql_desk_generated/default.py", text: "from graphql import parse\n" },
  { relative_path: "graphql_desk_generated/default.pyi", text: `selected: ${text}\n` },
  { relative_path: "graphql_desk_generated/_documents/default/d0.pyi", text: `class Result: value: ${text}\n` },
]], ["graphql_desk-stubs", [
  { relative_path: "graphql_desk-stubs/__init__.pyi", text: "from graphql import DocumentNode as _DocumentNode\ndef parse(source: str) -> _DocumentNode: ...\n" },
  { relative_path: "graphql_desk-stubs/testing.pyi", text: "def read_result(...): ...\n" },
  { relative_path: "graphql_desk-stubs/py.typed", text: "partial\n" },
]]]);
const fingerprint = packages => createHash("sha256").update(JSON.stringify(
  [...packages].flatMap(([, values]) => values.map(({ relative_path, text }) => [relative_path, text])).sort(([a], [b]) => a.localeCompare(b)),
)).digest("hex");
const savedSourceFingerprint = createHash("sha256").update("saved sources").digest("hex");
const operationManifest = (binding = "selected") => [{ project: "p", generated_package: "graphql_desk_generated", operations: [{
  owner: { module: { project: "p", module: "consumer", uri: "file:///workspace/consumer.py", version: 1 },
    codepoint: { start: 0, end: 12 }, utf16: { start: 0, end: 12 } },
  selector: null, operation_name: "Selected", operation_kind: "query", schema_identity: ["p", "main"],
  context_token: "schema-1", binding, status: "precise",
  declaration_path: "graphql_desk_generated/_documents/default/d0.pyi", declaration_symbol: "SelectedDocument",
  source_exports: [], dependencies: [], direct_fragments: [], definition_owners: [], canonical_owner: null,
  selector_known: true, source_dependencies_known: true, mapping_dependencies: [],
  schema_dependencies: [{ coordinate: "field:Query.id", fingerprint: "a".repeat(64) }],
  config_dependencies: [{ kind: "scalar-output", name: "String", value: null }],
}] }];
const publish = (f, packages, id, isCurrent = () => true, inputFingerprint = fingerprint(packages), manifest = operationManifest()) =>
  f.publisher.publish(packages, id, isCurrent, f.managed, inputFingerprint, savedSourceFingerprint, manifest);
const recover = (publisher, managed, packages, isCurrent = () => true, manifest = operationManifest()) =>
  publisher.recover(managed, isCurrent, fingerprint(packages), savedSourceFingerprint, manifest);
const markerBytes = (id, files, token = "11111111-1111-4111-8111-111111111111") => Buffer.from(`${JSON.stringify({
  owner: "graphql-desk-analysis-staging", format: 1, id,
  sequence: Number(/^g-(\d+)-/.exec(id)?.[1]), token, files,
}, null, 2)}\n`);
const markerPath = (root, id) => path.join(root, "generations", `${id}.staging.json`);
const markerTemporaryName = ".graphql-desk-11111111-1111-4111-8111-111111111111-marker-22222222-2222-4222-8222-222222222222.tmp";

test("publishes immutable complete generations and recovers the latest verified one", async t => {
  const f = fixture(t);
  assert.equal(await publish(f, files("str"), "analysis-1"), true);
  const first = path.resolve(f.workspace, f.managed.selectedSetting);
  assert.ok(fs.existsSync(path.join(first, "complete.json")));
  const firstBytes = fs.readFileSync(path.join(first, "graphql_desk_generated", "default.pyi"));
  assert.ok(fs.existsSync(path.join(first, "graphql_desk_generated", "default.py")));
  assert.deepEqual(f.publisher.current(f.managed)[0].manifest.operationManifest, operationManifest());

  assert.equal(await publish(f, files("int"), "analysis-2"), true);
  const second = path.resolve(f.workspace, f.managed.selectedSetting);
  assert.notEqual(first, second);
  assert.deepEqual(fs.readFileSync(path.join(first, "graphql_desk_generated", "default.pyi")), firstBytes);
  assert.match(JSON.parse(fs.readFileSync(path.join(second, "complete.json"), "utf8")).owner, /graphql-desk/);

  const restarted = { ...f.managed, selectedSetting: ".graphql-desk/typings" };
  const afterRestart = createAnalysisGenerations(f.vscode, f.workspace, f.root);
  assert.equal(await recover(afterRestart, restarted, files("int")), true);
  assert.equal(path.resolve(f.workspace, restarted.selectedSetting), second);
  assert.equal(fs.readFileSync(path.join(f.root, "user.txt"), "utf8"), "keep");
});

test("publication skips unowned generation directories without adopting or changing them", async t => {
  const f = fixture(t);
  assert.equal(await publish(f, files("str"), "analysis-1"), true);
  const leftover = path.join(f.root, "generations", "g-2-00000000-0000-0000-0000-000000000000");
  fs.mkdirSync(leftover);
  fs.writeFileSync(path.join(leftover, "keep.txt"), "foreign data");

  assert.equal(await publish(f, files("int"), "analysis-2"), true);
  const selected = path.resolve(f.workspace, f.managed.selectedSetting);
  assert.equal(path.basename(selected).split("-")[1], "3");
  assert.equal(fs.readFileSync(path.join(leftover, "keep.txt"), "utf8"), "foreign data");
  assert.equal(f.publisher.current(f.managed).some(item => item.directory === leftover), false);
});

test("rejects a runtime artifact without its stub before selecting a generation", async t => {
  const f = fixture(t);
  const unpaired = files("str");
  unpaired.set("graphql_desk_generated", unpaired.get("graphql_desk_generated").filter(file => !file.relative_path.endsWith(".pyi")));
  await assert.rejects(publish(f, unpaired, "analysis-unpaired"), /paired stub/);
  assert.equal(f.managed.selectedSetting, ".graphql-desk/typings");
  assert.equal(f.publisher.current(f.managed).length, 0);
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
  assert.match(latest.manifest.savedSourceFingerprint, /^[a-f0-9]{64}$/);
  assert.equal(latest.manifest.files[0].path, "graphql_desk_generated/_documents/default/d0.pyi");
});

test("recovery does not select a complete generation from older captured inputs", async t => {
  const f = fixture(t);
  await publish(f, files("str"), "analysis-1");
  f.managed.selectedSetting = ".graphql-desk/typings";
  assert.equal(await recover(f.publisher, f.managed, files("int")), false);
  assert.equal(f.managed.selectedSetting, ".graphql-desk/typings");
});

test("recovery requires matching operation provenance and rejects missing manifests", async t => {
  const f = fixture(t);
  assert.equal(await publish(f, files("str"), "analysis-1"), true);
  f.managed.selectedSetting = ".graphql-desk/typings";
  assert.equal(await recover(f.publisher, f.managed, files("str"), () => true, operationManifest("other")), false);
  assert.equal(await recover(f.publisher, f.managed, files("str")), true);
  assert.equal(await f.publisher.publish(files("int"), "missing-manifest", () => true, f.managed,
    fingerprint(files("int")), savedSourceFingerprint), false);
});

test("operation manifests reject duplicate owners, foreign declarations, and oversized payloads", async t => {
  const f = fixture(t);
  const duplicateOwner = operationManifest()[0].operations[0];
  assert.equal(await publish(f, files("str"), "duplicate", () => true, fingerprint(files("str")),
    [{ ...operationManifest()[0], operations: [duplicateOwner, duplicateOwner] }]), false);
  assert.equal(await publish(f, files("str"), "foreign", () => true, fingerprint(files("str")),
    [{ ...operationManifest()[0], operations: [{ ...duplicateOwner, declaration_path: "other/default.pyi" }] }]), false);
  assert.equal(await publish(f, files("str"), "bad-facts", () => true, fingerprint(files("str")),
    [{ ...operationManifest()[0], operations: [{ ...duplicateOwner, schema_dependencies: [
      { coordinate: "field:Query.name", fingerprint: "a".repeat(64) }, { coordinate: "field:Query.id", fingerprint: "b".repeat(64) },
    ] }] }]), false);
  assert.equal(await publish(f, files("str"), "bad-config", () => true, fingerprint(files("str")),
    [{ ...operationManifest()[0], operations: [{ ...duplicateOwner, config_dependencies: [
      { kind: "enum-mapping", name: "Color", value: false },
    ] }] }]), false);
  assert.equal(await publish(f, files("str"), "oversized", () => true, fingerprint(files("str")),
    [{ ...operationManifest()[0], operations: [{ ...duplicateOwner, source_exports: [{ relative_path: "x", symbol: "x", alias: "x" }], padding: "x".repeat(1024 * 1024) }] }]), false);
  assert.equal(f.publisher.current(f.managed).length, 0);
});

test("publishes the documented 2,000-operation manifest size", async t => {
  const f = fixture(t);
  const base = operationManifest()[0].operations[0];
  const operations = Array.from({ length: 2000 }, (_, index) => ({ ...base,
    binding: `document_${index}`, operation_name: `Operation${index}`,
    owner: { ...base.owner, codepoint: { start: index * 2, end: index * 2 + 1 },
      utf16: { start: index * 2, end: index * 2 + 1 } },
    schema_dependencies: Array.from({ length: 20 }, (_, field) => ({
      coordinate: `field:Type${String(field).padStart(2, "0")}.field${String(field).padStart(3, "0")}`,
      fingerprint: "a".repeat(64),
    })),
  }));
  const manifest = [{ project: "p", generated_package: "graphql_desk_generated", operations }];
  assert.ok(Buffer.byteLength(JSON.stringify(manifest)) > 1024 * 1024);
  assert.equal(await publish(f, files("str"), "large-manifest", () => true, fingerprint(files("str")), manifest), true);
  assert.equal(f.publisher.current(f.managed).length, 1);
});

test("accepts URI-backed project identities from the Python artifact manifest", () => {
  const manifest = operationManifest();
  const project = "file:///C:/Users/test/AppData/Local/Temp/graphql-desk/workspace/pyproject.toml";
  manifest[0].project = project;
  manifest[0].operations[0].owner.module.project = project;
  manifest[0].operations[0].schema_identity[0] = project;
  assert.equal(validOperationManifest(manifest), true);
  manifest[0].project = "";
  assert.equal(validOperationManifest(manifest), false);
});

test("manifest tampering and legacy generations cannot be recovered", async t => {
  const f = fixture(t);
  await publish(f, files("str"), "analysis-1");
  const selected = path.resolve(f.workspace, f.managed.selectedSetting);
  const complete = path.join(selected, "complete.json");
  const manifest = JSON.parse(fs.readFileSync(complete, "utf8"));
  manifest.operationManifest[0].operations[0].binding = "changed";
  fs.writeFileSync(complete, `${JSON.stringify(manifest, null, 2)}\n`);
  assert.equal(f.publisher.current(f.managed).length, 0);
  f.managed.selectedSetting = ".graphql-desk/typings";
  assert.equal(await recover(f.publisher, f.managed, files("str")), false);
  const legacy = { ...manifest, format: 1 };
  fs.writeFileSync(complete, `${JSON.stringify(legacy, null, 2)}\n`);
  assert.equal(f.publisher.current(f.managed).length, 0);
  assert.equal(await recover(f.publisher, f.managed, files("str")), false);
  assert.equal(fs.existsSync(complete), true, "unverified generations are preserved");
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
  assert.equal(await recover(f.publisher, f.managed, files("int")), true);
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

test("recovery never restores an older generation when the newest is changed", async t => {
  const f = fixture(t);
  await publish(f, files("str"), "analysis-1");
  const first = path.resolve(f.workspace, f.managed.selectedSetting);
  await publish(f, files("int"), "analysis-2");
  const second = path.resolve(f.workspace, f.managed.selectedSetting);
  const artifact = path.join(second, "graphql_desk_generated", "default.pyi");
  fs.writeFileSync(artifact, "user bytes");
  const restarted = { ...f.managed, selectedSetting: path.relative(f.workspace, second).split(path.sep).join("/") };
  const recovery = createAnalysisGenerations(f.vscode, f.workspace, f.root);
  assert.equal(await recover(recovery, restarted, files("int")), false);
  assert.equal(path.resolve(f.workspace, restarted.selectedSetting), second);
  assert.ok(fs.existsSync(first), "the older generation remains available but is not selected");
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
  ], "analysis-1", () => true, f.managed, fingerprint(files("str")), createHash("sha256").update("saved sources").digest("hex"), operationManifest()), /Invalid or oversized generation artifact/);
  assert.equal(fs.readFileSync(partialFile, "utf8"), "partial user-visible bytes");
  assert.equal(fs.readdirSync(path.join(f.root, "generations")).length, 1);
  assert.equal(await publish(f, files("str"), "analysis-2"), true);
  assert.equal(fs.readFileSync(partialFile, "utf8"), "partial user-visible bytes");
  assert.equal(fs.readdirSync(path.join(f.root, "generations")).length, 2);
});

test("restart removes only an owned incomplete generation and its named partial staging files", async t => {
  const f = fixture(t);
  const id = "g-7-00000000-0000-0000-0000-000000000000";
  const directory = path.join(f.root, "generations", id);
  const token = "11111111-1111-4111-8111-111111111111";
  const target = "graphql_desk_generated/default.pyi";
  const expected = renderOwnedAnalysisArtifact("graphql_desk_generated", "default.pyi", "selected: str\n");
  const entries = [{ path: target, sha256: createHash("sha256").update(expected).digest("hex") }];
  const bytes = markerBytes(id, entries, token);
  const marker = markerPath(f.root, id);
  fs.writeFileSync(marker, bytes);
  await f.managed.recordStaging(directory, createHash("sha256").update(bytes).digest("hex"), markerTemporaryName);
  const packageRoot = path.join(directory, "graphql_desk_generated");
  fs.mkdirSync(packageRoot, { recursive: true });
  const temporary = path.join(packageRoot,
    `.graphql-desk-${token}-${createHash("sha256").update(target).digest("hex").slice(0, 16)}-22222222-2222-4222-8222-222222222222.tmp`);
  fs.writeFileSync(temporary, "partial write before simulated process crash");

  assert.equal(await recover(f.publisher, f.managed, files("str")), false);
  assert.equal(fs.existsSync(directory), false);
  assert.equal(fs.existsSync(marker), false);
  assert.equal(f.managed.ownsStaging(directory, createHash("sha256").update(bytes).digest("hex")), false);
});

test("staging marker ordering is deterministic for case-distinct nested paths", async t => {
  const f = fixture(t);
  const id = "g-7-00000000-0000-0000-0000-000000000002";
  const directory = path.join(f.root, "generations", id);
  const paths = ["graphql_desk_generated/a_.pyi", "graphql_desk_generated/A.pyi"];
  const entries = paths.map(relative => {
    const bytes = renderOwnedAnalysisArtifact("graphql_desk_generated", relative.split("/").at(-1), "value: str\n");
    return { relative, bytes, sha256: createHash("sha256").update(bytes).digest("hex") };
  });
  const markerFiles = entries.map(({ relative, sha256 }) => ({ path: relative, sha256 }))
    .sort((left, right) => left.path < right.path ? -1 : left.path > right.path ? 1 : 0);
  const bytes = markerBytes(id, markerFiles);
  const marker = markerPath(f.root, id);
  fs.writeFileSync(marker, bytes);
  await f.managed.recordStaging(directory, createHash("sha256").update(bytes).digest("hex"), markerTemporaryName);
  for (const entry of entries) {
    const file = path.join(directory, ...entry.relative.split("/"));
    fs.mkdirSync(path.dirname(file), { recursive: true });
    fs.writeFileSync(file, entry.bytes);
  }

  assert.equal(await recover(f.publisher, f.managed, files("str")), false);
  assert.equal(fs.existsSync(directory), false);
  assert.equal(fs.existsSync(marker), false);
});

test("unowned or edited staging markers and outputs are preserved", async t => {
  const f = fixture(t);
  const id = "g-8-00000000-0000-0000-0000-000000000000";
  const directory = path.join(f.root, "generations", id);
  const target = "graphql_desk_generated/default.pyi";
  const expected = renderOwnedAnalysisArtifact("graphql_desk_generated", "default.pyi", "selected: str\n");
  const entries = [{ path: target, sha256: createHash("sha256").update(expected).digest("hex") }];
  const bytes = markerBytes(id, entries);
  const marker = markerPath(f.root, id);
  fs.writeFileSync(marker, bytes);
  const foreignFile = path.join(f.root, "generations", "foreign.txt");
  fs.writeFileSync(foreignFile, "foreign data");
  assert.equal(await recover(f.publisher, f.managed, files("str")), false);
  assert.equal(fs.readFileSync(marker).toString("utf8"), bytes.toString("utf8"));
  assert.equal(fs.readFileSync(foreignFile, "utf8"), "foreign data");

  fs.unlinkSync(marker);
  fs.unlinkSync(foreignFile);
  fs.mkdirSync(path.join(directory, "graphql_desk_generated"), { recursive: true });
  const artifact = path.join(directory, ...target.split("/"));
  fs.writeFileSync(artifact, "edited by another owner");
  fs.writeFileSync(marker, bytes);
  await f.managed.recordStaging(directory, createHash("sha256").update(bytes).digest("hex"), markerTemporaryName);
  assert.equal(await recover(f.publisher, f.managed, files("str")), false);
  assert.equal(fs.readFileSync(artifact, "utf8"), "edited by another owner");
  assert.ok(fs.existsSync(marker));
});

test("a syntactically valid foreign marker without durable ownership is preserved", async t => {
  const f = fixture(t);
  const id = "g-8-00000000-0000-0000-0000-000000000001";
  const target = "graphql_desk_generated/default.pyi";
  const expected = renderOwnedAnalysisArtifact("graphql_desk_generated", "default.pyi", "selected: str\n");
  const marker = markerPath(f.root, id);
  const bytes = markerBytes(id, [{ path: target, sha256: createHash("sha256").update(expected).digest("hex") }]);
  fs.writeFileSync(marker, bytes);

  assert.equal(await recover(f.publisher, f.managed, files("str")), false);
  assert.equal(fs.existsSync(path.join(f.root, "generations", id)), false);
  assert.deepEqual(fs.readFileSync(marker), bytes);
  assert.equal(f.managed.ownsStaging(path.join(f.root, "generations", id), createHash("sha256").update(bytes).digest("hex")), false);
});

test("startup cleans an owned truncated marker after a crash before atomic publication", async t => {
  const f = fixture(t);
  const id = "g-8-00000000-0000-0000-0000-000000000003";
  const directory = path.join(f.root, "generations", id);
  const bytes = markerBytes(id, []);
  const fingerprint = createHash("sha256").update(bytes).digest("hex");
  await f.managed.recordStaging(directory, fingerprint, markerTemporaryName);
  fs.writeFileSync(path.join(f.root, "generations", markerTemporaryName), "{\"owner\":");

  assert.equal(await recover(f.publisher, f.managed, files("str")), false);
  assert.equal(fs.existsSync(markerPath(f.root, id)), false);
  assert.equal(fs.existsSync(path.join(f.root, "generations", markerTemporaryName)), false);
  assert.equal(fs.existsSync(directory), false);
  assert.equal(f.managed.ownsStaging(directory, fingerprint), false);
});

test("startup preserves an owned marker temporary with an open dirty buffer", async t => {
  const f = fixture(t);
  const id = "g-8-00000000-0000-0000-0000-000000000004";
  const directory = path.join(f.root, "generations", id);
  const bytes = markerBytes(id, []);
  const fingerprint = createHash("sha256").update(bytes).digest("hex");
  const temporary = path.join(f.root, "generations", markerTemporaryName);
  const contents = Buffer.from("user-edited marker data");
  await f.managed.recordStaging(directory, fingerprint, markerTemporaryName);
  fs.writeFileSync(temporary, contents);
  f.vscode.workspace.textDocuments.push({ uri: f.vscode.Uri.file(temporary), isDirty: true });

  assert.equal(await recover(f.publisher, f.managed, files("str")), false);
  assert.deepEqual(fs.readFileSync(temporary), contents);
  assert.equal(fs.existsSync(directory), false);
});

test("staging marker cleanup preserves dirty buffers for missing, complete and incomplete generations", async t => {
  {
    const f = fixture(t);
    const id = "g-8-00000000-0000-0000-0000-000000000005";
    const directory = path.join(f.root, "generations", id);
    const bytes = markerBytes(id, []);
    const marker = markerPath(f.root, id);
    fs.writeFileSync(marker, bytes);
    await f.managed.recordStaging(directory, createHash("sha256").update(bytes).digest("hex"), markerTemporaryName);
    f.vscode.workspace.textDocuments.push({ uri: f.vscode.Uri.file(marker), isDirty: true });

    await recover(f.publisher, f.managed, files("str"));
    assert.deepEqual(fs.readFileSync(marker), bytes);
    assert.equal(fs.existsSync(directory), false);
  }

  {
    const f = fixture(t);
    await publish(f, files("str"), "complete-before-cleanup");
    const directory = path.resolve(f.workspace, f.managed.selectedSetting);
    const id = path.basename(directory);
    const manifest = JSON.parse(fs.readFileSync(path.join(directory, "complete.json"), "utf8"));
    const markerFiles = [...manifest.files].sort((left, right) => left.path < right.path ? -1 : left.path > right.path ? 1 : 0);
    const bytes = markerBytes(id, markerFiles);
    const marker = markerPath(f.root, id);
    fs.writeFileSync(marker, bytes);
    await f.managed.recordStaging(directory, createHash("sha256").update(bytes).digest("hex"), markerTemporaryName);
    f.vscode.workspace.textDocuments.push({ uri: f.vscode.Uri.file(marker), isDirty: true });

    await recover(f.publisher, f.managed, files("str"));
    assert.deepEqual(fs.readFileSync(marker), bytes);
    assert.equal(fs.existsSync(path.join(directory, "complete.json")), true);
  }

  {
    const f = fixture(t);
    const id = "g-8-00000000-0000-0000-0000-000000000006";
    const directory = path.join(f.root, "generations", id);
    const target = "graphql_desk_generated/default.pyi";
    const artifact = renderOwnedAnalysisArtifact("graphql_desk_generated", "default.pyi", "selected: str\n");
    const bytes = markerBytes(id, [{ path: target, sha256: createHash("sha256").update(artifact).digest("hex") }]);
    const marker = markerPath(f.root, id);
    fs.writeFileSync(marker, bytes);
    await f.managed.recordStaging(directory, createHash("sha256").update(bytes).digest("hex"), markerTemporaryName);
    const file = path.join(directory, ...target.split("/"));
    fs.mkdirSync(path.dirname(file), { recursive: true });
    fs.writeFileSync(file, artifact);
    f.vscode.workspace.textDocuments.push({ uri: f.vscode.Uri.file(marker), isDirty: true });

    await recover(f.publisher, f.managed, files("str"));
    assert.deepEqual(fs.readFileSync(marker), bytes);
    assert.equal(fs.existsSync(directory), false);
  }
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
  const manifest = { owner: "graphql-desk-analysis-generation", format: 2, id, sequence: 99,
    analysisId: null, inputFingerprint: digest(Buffer.from("forged")),
    artifactFingerprint: digest(Buffer.from(JSON.stringify(manifestFiles))), files: manifestFiles, operationManifest: [] };
  fs.writeFileSync(path.join(directory, "complete.json"), `${JSON.stringify(manifest, null, 2)}\n`);

  assert.equal(f.publisher.current(f.managed).length, 0);
  assert.equal(await f.publisher.recover(f.managed), false);
  assert.equal(f.managed.selectedSetting, ".graphql-desk/typings");
  assert.equal(await publish(f, files("str"), "analysis-1"), true);
  assert.equal(fs.readFileSync(artifact, "utf8"), bytes.toString("utf8"));
  assert.equal(fs.existsSync(path.join(directory, "complete.json")), true);
});
