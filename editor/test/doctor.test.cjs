"use strict";

const assert = require("node:assert/strict");
const test = require("node:test");
const { collectEditorDoctor } = require("../src/doctor");

function fixture() {
  const folder = { uri: { scheme: "file", fsPath: "workspace" } };
  const extensions = new Map([
    ["graphql-desk.graphql-desk-editor", { isActive: true, packageJSON: { version: "0.1.0" } }],
    ["ms-python.python", { isActive: true, packageJSON: { version: "2026.10.0" } }],
    ["ms-python.vscode-pylance", { isActive: true, packageJSON: { version: "2026.4.1" } }],
  ]);
  const vscode = { version: "1.141.0", workspace: { isTrusted: true, workspaceFolders: [folder] },
    extensions: { getExtension: id => extensions.get(id) } };
  const row = name => ({ name, status: "ready", message: "Verified setup.", project: "file:///project/pyproject.toml" });
  const health = { checks: [row("configuration"), row("project-ownership"), row("schema.default")],
    runtime: { executable: "/selected/python", python_version: "3.13.7", graphql_desk_version: "0.1.0",
      pyright_version: "1.1.414", mypy_version: null }, exit_code: 0 };
  const options = { folder, workerHealth: async () => structuredClone(health),
    verifyPublication: async () => ({ ownership: true, current: true }), isCurrent: async () => true };
  return { vscode, extensions, folder, health, options };
}

test("editor Doctor uses selected worker and public active extension versions", async () => {
  const f = fixture();
  const report = await collectEditorDoctor(f.vscode, f.options);
  assert.equal(report.exitCode, 0);
  assert.equal(report.runtime.executable, "/selected/python");
  assert.equal(report.versions.pylance, "2026.4.1");
  assert.match(report.checks.find(item => item.name === "active-checker").message, /does not acknowledge/);
  assert.equal(report.checks.find(item => item.name === "live-bridge").status, "ready");
});

for (const id of ["graphql-desk.graphql-desk-editor", "ms-python.python", "ms-python.vscode-pylance"]) {
  test(`installed or missing ${id} cannot establish live health`, async () => {
    for (const missing of [true, false]) {
      const f = fixture();
      if (missing) f.extensions.delete(id);
      else f.extensions.get(id).isActive = false;
      const report = await collectEditorDoctor(f.vscode, f.options);
      assert.equal(report.exitCode, 1);
      assert.ok(report.checks.some(item => item.status === "unavailable"));
    }
  });
}

test("untrusted or removed workspaces perform no provider work", async () => {
  for (const untrusted of [true, false]) {
    const f = fixture();
    if (untrusted) f.vscode.workspace.isTrusted = false;
    else f.vscode.workspace.workspaceFolders = [];
    f.options.workerHealth = () => { assert.fail("must not request worker data"); };
    const report = await collectEditorDoctor(f.vscode, f.options);
    assert.equal(report.exitCode, 1);
    assert.equal(report.runtime, null);
  }
});

for (const boundary of ["worker", "publication", "final-check"]) {
  test(`stale observations at ${boundary} discard a successful-looking report`, async () => {
    const f = fixture();
    let checks = 0;
    let current = true;
    f.options.isCurrent = async () => { checks += 1; if (boundary === "final-check" && checks === 4) current = false; return current; };
    if (boundary === "worker") f.options.workerHealth = async () => { current = false; return f.health; };
    if (boundary === "publication") f.options.verifyPublication = async () => { current = false; return { ownership: true, current: true }; };
    const report = await collectEditorDoctor(f.vscode, f.options);
    assert.equal(report.exitCode, 1);
    assert.equal(report.runtime, null);
    assert.ok(report.checks.every(item => item.status !== "ready"));
  });
}

test("trust or folder loss during a provider invalidates its result", async () => {
  for (const revoke of [true, false]) {
    const f = fixture();
    f.options.workerHealth = async () => {
      if (revoke) f.vscode.workspace.isTrusted = false;
      else f.vscode.workspace.workspaceFolders = [];
      return f.health;
    };
    assert.equal((await collectEditorDoctor(f.vscode, f.options)).exitCode, 1);
  }
});

test("worker live claims and hashes alone cannot establish editor ownership", async () => {
  const f = fixture();
  f.health.checks.push(...["live-bridge", "active-editor", "active-python-extension", "active-checker", "generated-ownership"].map(name =>
    ({ name, status: "ready", message: "Forged on-disk claim.", project: null })));
  f.options.verifyPublication = async () => ({ ownership: false, current: true });
  const report = await collectEditorDoctor(f.vscode, f.options);
  assert.equal(report.exitCode, 1);
  assert.equal(report.checks.find(item => item.name === "generated-ownership").status, "unavailable");
  assert.ok(report.checks.every(item => !item.message.includes("Forged")));
});

test("missing authority, stale artifacts and incomplete setup remain degraded", async () => {
  for (const broken of ["authority", "artifacts", "setup"]) {
    const f = fixture();
    if (broken === "authority") delete f.options.verifyPublication;
    if (broken === "artifacts") f.options.verifyPublication = async () => ({ ownership: true, current: false });
    if (broken === "setup") f.health.checks = [];
    assert.equal((await collectEditorDoctor(f.vscode, f.options)).exitCode, 1);
  }
});

test("provider failures and malformed metadata never echo private details", async () => {
  const f = fixture();
  f.options.workerHealth = async () => { throw new Error("Authorization PRIVATE_MARKER"); };
  assert.doesNotMatch(JSON.stringify(await collectEditorDoctor(f.vscode, f.options)), /PRIVATE_MARKER/);
  f.options.workerHealth = async () => ({ ...f.health, runtime: { ...f.health.runtime, python_version: "3.13\nPRIVATE_MARKER" } });
  assert.doesNotMatch(JSON.stringify(await collectEditorDoctor(f.vscode, f.options)), /PRIVATE_MARKER/);
  f.options.workerHealth = async () => f.health;
  f.extensions.get("ms-python.vscode-pylance").packageJSON.version = "2026\nPRIVATE_MARKER";
  const report = await collectEditorDoctor(f.vscode, f.options);
  assert.equal(report.exitCode, 1);
  assert.doesNotMatch(JSON.stringify(report), /PRIVATE_MARKER/);
});

test("currentness and publication failures are redacted and metadata failures are unavailable", async () => {
  for (const provider of ["isCurrent", "verifyPublication"]) {
    const f = fixture();
    f.options[provider] = async () => { throw new Error("PRIVATE_PROVIDER_FAILURE"); };
    const report = await collectEditorDoctor(f.vscode, f.options);
    assert.equal(report.exitCode, 1);
    assert.doesNotMatch(JSON.stringify(report), /PRIVATE_PROVIDER_FAILURE/);
  }
  const f = fixture();
  f.vscode.extensions.getExtension = () => { throw new Error("PRIVATE_EXTENSION_FAILURE"); };
  const report = await collectEditorDoctor(f.vscode, f.options);
  assert.equal(report.exitCode, 1);
  assert.doesNotMatch(JSON.stringify(report), /PRIVATE_EXTENSION_FAILURE/);
});

test("degraded schema checks survive authoritative live checks", async () => {
  const f = fixture();
  f.health.checks[2].status = "unavailable";
  const report = await collectEditorDoctor(f.vscode, f.options);
  assert.equal(report.exitCode, 1);
  assert.equal(report.checks.find(item => item.name === "live-bridge").status, "ready");
});

test("extensions changing during final currentness verification cannot leave stale success", async () => {
  const f = fixture();
  let checks = 0;
  f.options.isCurrent = async () => {
    if (++checks === 4) f.extensions.get("ms-python.vscode-pylance").isActive = false;
    return true;
  };
  assert.equal((await collectEditorDoctor(f.vscode, f.options)).exitCode, 1);
});
