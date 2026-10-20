"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs/promises");
const os = require("node:os");
const path = require("node:path");
const { runTests, runVSCodeCommand } = require("@vscode/test-electron");

async function main() {
  const version = process.argv[2];
  assert.ok(["1.141.0", "1.142.0"].includes(version), "Choose a pinned host version");
  const suite = process.argv[3] || "features";
  assert.ok(["features", "discovery", "activation"].includes(suite), "Choose a known isolated suite");
  const temporary = await fs.mkdtemp(path.join(os.tmpdir(), "graphql-desk-features-"));
  const workspace = path.join(temporary, "workspace");
  const profile = path.join(temporary, "profile");
  const extensions = path.resolve(__dirname, "..", ".vscode-test", `features-${version}`, "extensions");
  const result = path.join(temporary, "result.json");
  const python = process.env.GRAPHQL_DESK_PYTHON || path.resolve(__dirname, "..", "..", ".venv", process.platform === "win32" ? "Scripts/python.exe" : "bin/python");
  assert.ok(path.isAbsolute(python), "Choose an absolute trusted Python interpreter");
  await fs.mkdir(path.join(workspace, ".vscode"), { recursive: true });
  await fs.writeFile(path.join(workspace, ".vscode", "settings.json"), JSON.stringify({
    "extensions.autoUpdate": false,
    "extensions.autoCheckUpdates": false,
    "telemetry.telemetryLevel": "off",
    "files.autoSave": "off",
    "python.defaultInterpreterPath": python,
    "graphqlDesk.pythonPath": "",
    "python.experiments.enabled": false,
    "python.analysis.aiHoverSummaries": false,
  }));
  const isolation = [`--user-data-dir=${profile}`, `--extensions-dir=${extensions}`];
  for (const extension of ["ms-python.python@2026.10.0", "ms-python.vscode-pylance@2026.4.1"]) {
    await runVSCodeCommand([...isolation, "--install-extension", extension, "--force"], { version });
  }
  await runTests({
    version,
    extensionDevelopmentPath: path.resolve(__dirname, ".."),
    extensionTestsPath: path.join(__dirname, `${suite}.host.cjs`),
    extensionTestsEnv: {
      GRAPHQL_DESK_TEST_WORKSPACE: workspace,
      GRAPHQL_DESK_RESULT_PATH: result,
      GRAPHQL_DESK_PYTHON: python,
      GRAPHQL_DESK_HOST_VERSION: version,
    },
    launchArgs: [workspace, ...isolation, "--disable-updates", "--disable-telemetry", "--skip-welcome", "--skip-release-notes", "--disable-workspace-trust"],
  });
  const observed = JSON.parse(await fs.readFile(result, "utf8"));
  assert.equal(observed.outcome, `read-only-${suite}-observed`);
  console.log(`Read-only ${suite} evidence: ${result}`);
}

main().catch(error => { console.error(error); process.exitCode = 1; });
