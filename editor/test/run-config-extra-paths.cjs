"use strict";

const assert = require("node:assert/strict");
const fsSync = require("node:fs");
const fs = require("node:fs/promises");
const path = require("node:path");
const { randomUUID } = require("node:crypto");
const { runTests, runVSCodeCommand } = require("@vscode/test-electron");

async function main() {
  const version = "1.141.0";
  const editor = path.resolve(__dirname, "..");
  const cachePath = path.join(editor, ".cache", "vscode-test");
  const temporaryRoot = path.join(editor, ".cache", "issue-329-extra-paths");
  await fs.mkdir(temporaryRoot, { recursive: true });
  const temporary = path.join(temporaryRoot, randomUUID());
  const workspace = path.join(temporary, "workspace");
  const profile = path.join(temporary, "profile");
  const extensions = path.join(temporary, "extensions");
  const result = path.join(temporary, "result.json");
  const python = process.env.GRAPHQL_DESK_PYTHON || path.resolve(editor, "..", ".venv",
    process.platform === "win32" ? "Scripts/python.exe" : "bin/python");
  assert.ok(path.isAbsolute(python), "The host must use an absolute Python interpreter");
  await fs.mkdir(workspace, { recursive: true });
  await fs.mkdir(extensions, { recursive: true });
  await fs.mkdir(path.join(workspace, "custom_stubs", "custom_pkg"), { recursive: true });
  for (const [generation, annotation] of [["g-1", "str"]]) {
    const root = path.join(workspace, ".graphql-desk", "generations", generation, "graphql_desk_generated");
    await fs.mkdir(root, { recursive: true });
    await fs.writeFile(path.join(root, "__init__.pyi"), "", { flag: "wx" });
    await fs.writeFile(path.join(root, "default.pyi"), `from typing import TypedDict\nclass QueryResult(TypedDict):\n    value: ${annotation}\n`, { flag: "wx" });
  }
  await fs.writeFile(path.join(workspace, "custom_stubs", "custom_pkg", "__init__.pyi"), "answer: int\n", { flag: "wx" });
  await fs.writeFile(path.join(workspace, "consumer.py"),
    "from custom_pkg import answer\nfrom graphql_desk_generated.default import QueryResult\nresult: QueryResult = {\"value\": \"ok\"}\nvalue = result[\"value\"]\nneeds_text: str = value\n");
  const projectConfig = `// Project configuration and execution environments take precedence over workspace Python settings.\n{\n  "stubPath": "custom_stubs",\n  "extraPaths": [".graphql-desk/generations/g-1"],\n  "executionEnvironments": [{ "root": ".", "extraPaths": [".graphql-desk/generations/g-1"] }],\n  "typeCheckingMode": "strict"\n}\n`;
  await fs.writeFile(path.join(workspace, "pyrightconfig.json"), projectConfig, { flag: "wx" });
  await fs.mkdir(path.join(workspace, ".vscode"), { recursive: true });
  await fs.writeFile(path.join(workspace, ".vscode", "settings.json"), JSON.stringify({
    "python.defaultInterpreterPath": python,
    "python.analysis.stubPath": "workspace-shadow",
    "python.analysis.typeCheckingMode": "off",
    "extensions.autoUpdate": false,
    "extensions.autoCheckUpdates": false,
    "telemetry.telemetryLevel": "off",
  }, null, 2));
  const isolation = [`--user-data-dir=${profile}`, `--extensions-dir=${extensions}`];
  try {
    for (const identifier of ["ms-python.python@2026.10.0", "ms-python.vscode-pylance@2026.4.1"]) {
      await runVSCodeCommand([...isolation, "--install-extension", identifier, "--force"], { version, cachePath });
    }
    const code = await runTests({
      version,
      cachePath,
      extensionDevelopmentPath: editor,
      extensionTestsPath: path.join(__dirname, "configExtraPaths.host.cjs"),
      extensionTestsEnv: { GRAPHQL_DESK_TEST_WORKSPACE: workspace, GRAPHQL_DESK_RESULT_PATH: result },
      launchArgs: [workspace, ...isolation, "--disable-updates", "--disable-telemetry", "--skip-welcome", "--skip-release-notes"],
    });
    assert.equal(code, 0);
    const evidence = JSON.parse(await fs.readFile(result, "utf8"));
    assert.match(evidence.before, /value:\s*str\b/);
    assert.match(evidence.after, /value:\s*(?:str\s*\|\s*None|Optional\[str\])/i);
    assert.equal(evidence.configuredProjectStubPath, "custom_stubs");
    assert.equal(evidence.stableExtraPath, true);
    assert.equal(evidence.generatedDocumentClean, true);
    assert.match(evidence.persistedGeneration, /value: str \| None/);
    console.log(JSON.stringify(evidence, null, 2));
  } finally {
    if (fsSync.existsSync(temporary)) await fs.rm(temporary, { recursive: true, force: true, maxRetries: 8, retryDelay: 250 });
    await assert.rejects(fs.access(temporary), { code: "ENOENT" });
  }
}

main().catch(error => { console.error(error); process.exitCode = 1; });
