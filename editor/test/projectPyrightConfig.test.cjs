"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const { parseJsonConfig, modifyJsonConfig, updateProjectConfig, createManagedProjectConfig, MANAGED_PATH, stateKey } = require("../src/projectPyrightConfig");

function stateStore() {
  const values = new Map();
  return { get: key => values.get(key), update: async (key, value) => value === undefined ? values.delete(key) : values.set(key, value) };
}

function fixture(t, { json, toml, dirty = false } = {}) {
  const localTemp = path.join(__dirname, "..", ".cache", "issue-329-unit");
  fs.mkdirSync(localTemp, { recursive: true });
  const root = fs.mkdtempSync(path.join(localTemp, "pyright-config-"));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const folder = { uri: { scheme: "file", fsPath: root, toString: () => `file://${root.replaceAll("\\", "/")}` } };
  if (json !== undefined) fs.writeFileSync(path.join(root, "pyrightconfig.json"), json);
  if (toml !== undefined) fs.writeFileSync(path.join(root, "pyproject.toml"), toml);
  const vscode = {
    workspace: {
      isTrusted: true,
      textDocuments: dirty ? [{ isDirty: true, uri: { scheme: "file", fsPath: path.join(root, "pyrightconfig.json") } }] : [],
      getWorkspaceFolder(uri) { return path.resolve(uri.fsPath).startsWith(path.resolve(root)) ? folder : undefined; },
    },
    Uri: { file: fsPath => ({ scheme: "file", fsPath }) },
  };
  const worker = async (_folder, text, action, managedPath) => {
    const start = text.indexOf("[tool.pyright]");
    if (start < 0) return { ok: true, found: false, text, stub_path: null, extra_paths: [], has_execution_environments: false,
      execution_environments: [], ownership: { top_level: true, execution_environment_roots: [] } };
    const section = text.slice(start).split("\n").filter(line => !line.trimStart().startsWith("#"));
    const stub = /^stubPath\s*=\s*"([^"]*)"/m.exec(section.join("\n"))?.[1] ?? null;
    const pathLine = /^extraPaths\s*=\s*\[([^\]]*)\]/m.exec(section.join("\n"));
    const extra = [...(pathLine?.[1].matchAll(/"([^"]*)"/g) ?? [])].map(match => match[1]);
    const hasEnvs = /^executionEnvironments\s*=\s*\[/m.test(section.join("\n"));
    let output = text;
    if (action === "add" && !extra.includes(managedPath)) {
      const line = pathLine?.[0];
      output = line
        ? text.replace(line, line.replace(/\]\s*$/, `${extra.length ? ", " : ""}\"${managedPath}\"]`))
        : `${text.trimEnd()}\nextraPaths = [\"${managedPath}\"]\n`;
    } else if (action === "remove" && extra.includes(managedPath)) {
      const line = pathLine[0].replace(new RegExp(`,?\\s*\\"${managedPath.replace(/[.*+?^${}()|[\\]\\\\]/g, "\\\\$&")}\\"`), "");
      output = text.replace(pathLine[0], line);
    }
    const nextExtra = action === "add" ? [...extra, ...(extra.includes(managedPath) ? [] : [managedPath])] :
      action === "remove" ? extra.filter(item => item !== managedPath) : extra;
    return { ok: true, found: true, text: output, stub_path: stub, extra_paths: nextExtra, has_execution_environments: hasEnvs,
      execution_environments: [], ownership: { top_level: !pathLine, execution_environment_roots: [] } };
  };
  return { root, folder, vscode, worker };
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
  assert.deepEqual(parseJsonConfig(fs.readFileSync(path.join(f.root, "pyrightconfig.json"), "utf8")).extraPaths,
    ["vendor", MANAGED_PATH]);
  assert.equal(managed.stillOwnsSetting(), true);
  assert.equal(managed.validateGeneratedPackages(["graphql_desk_generated"]), true);
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

test("managed config fails closed when existing stubs shadow generated package", async t => {
  const f = fixture(t, { json: '{"stubPath":"custom-stubs"}' });
  const shadow = path.join(f.root, "custom-stubs", "graphql_desk_generated");
  fs.mkdirSync(shadow, { recursive: true });
  fs.writeFileSync(path.join(shadow, "default.pyi"), "# user stub\n");
  const managed = await createManagedProjectConfig(f.vscode, f.folder, stateStore(), f.worker);
  assert.throws(() => managed.validateGeneratedPackages(["graphql_desk_generated"]), /shadows GraphQL Desk/);
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
