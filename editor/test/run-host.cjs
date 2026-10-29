Warning: truncated output (original token count: 9370)
Total output lines: 566

"use strict";
const assert = require("node:assert/strict");
const { randomUUID } = require("node:crypto");
const fsSync = require("node:fs");
const fs = require("node:fs/promises");
const path = require("node:path");
const { spawn, spawnSync } = require("node:child_process");
const { once } = require("node:events");
const { runTests, runVSCodeCommand } = require("@vscode/test-electron");
let mappingFixtureCleanup;

function generatedFactoryFiles(python, workspace, sourcePath, typingMatrix, externalMappings, mixedLists = false, fragmentSourcePath = null) {
  const base = { workspace, candidates: [sourcePath, ...(fragmentSourcePath ? [fragmentSourcePath] : [])], buffers: [], documents: [], editor_fallback: null };
  const script = [
    "import json, sys",
    "from graphql_desk.editor_worker import EditorWorker",
    "base = json.loads(sys.argv[1])",
    "worker = EditorWorker()",
    "prepared = worker.handle({'op': 'prepare', **base, 'remote_revisions': []})",
    "assert prepared['ok'], prepared",
    "request = {**base, 'id': prepared['id'], 'schema_buffers': [], 'remote_revisions': []}",
    "analyzed = worker.handle({'op': 'analyze', **request})",
    "assert analyzed['ok'], analyzed",
    "print(json.dumps(worker.handle({'op': 'artifacts', **request})))",
  ].join("\n");
  function generate() {
    const child = spawnSync(python, ["-c", script, JSON.stringify(base)], {
      cwd: workspace,
      env: { ...process.env, PYTHONPATH: path.resolve(__dirname, "..", "..", "src") },
      encoding: "utf8",
    });
    assert.equal(child.status, 0, child.stderr);
    const result = JSON.parse(child.stdout);
    assert.equal(result.ok, true, JSON.stringify(result));
    const files = result.projects[0].files.filter(file => file.relative_path.endsWith(".pyi"));
    assert.ok(files.length > 1, JSON.stringify(result.projects[0]));
    return files;
  }
  const initial = generate();
  const shapeFields = typingMatrix ? ` nested: [[String!]!]! nullable: [String]${mixedLists ? " nullableList: [String!] nullableItems: [String]!" : ""}` : "";
  const mappedSchema = externalMappings ? "scalar ExternalDate\nenum ExternalColor { RED BLUE }\n" : "";
  const mappedFields = externalMappings ? " externalDate(value: ExternalDate!): ExternalDate! externalColor(value: ExternalColor!): ExternalColor!" : "";
  fsSync.writeFileSync(path.join(workspace, "schema.graphql"), `${mappedSchema}type User { id: ID! name: String! }\ntype Query { selected: String count(limit: Int!): Int! user: User!${mappedFields}${shapeFields} }\n`);
  const nullable = generate();
  fsSync.writeFileSync(path.join(workspace, "schema.graphql"), `${mappedSchema}type User { id: ID! name: String! }\ntype Query { selected: String! count(limit: Int!): Int! user: User!${mappedFields}${shapeFields} }\n`);
  return { initial, nullable };
}

async function readStubFiles(root) {
  const files = [];
  async function visit(directory, relative = "") {
    for (const entry of await fs.readdir(directory, { withFileTypes: true })) {
      const child = path.join(directory, entry.name);
      const childRelative = relative ? `${relative}/${entry.name}` : entry.name;
      if (entry.isDirectory()) await visit(child, childRelative);
      else if (entry.isFile() && entry.name.endsWith(".pyi")) {
        files.push([childRelative, await fs.readFile(child, "utf8")]);
      }
    }
  }
  await visit(root);
  return files.sort(([left], [right]) => left.localeCompare(right));
}

async function installMappingStub(python) {
  const site = spawnSync(python, ["-I", "-X", "utf8", "-c", "import sysconfig; print(sysconfig.get_path('purelib'))"], { encoding: "utf8" });
  assert.equal(site.status, 0, site.stderr);
  const purelib = path.resolve(site.stdout.trim());
  assert.ok(path.isAbsolute(purelib), "Selected interpreter must report an absolute purelib path");
  const packageName = `graphql_desk_mapping_fixture_${randomUUID().replaceAll("-", "")}`;
  const directory = path.join(purelib, `${packageName}-stubs`);
  assert.equal(path.dirname(directory), purelib, "The fixture must be directly under the selected interpreter's purelib");
  const files = new Map([
    ["__init__.pyi", ""],
    ["alpha.pyi", "from enum import Enum\nclass AlphaDateInput: ...\nclass AlphaDateOutput: ...\nclass AlphaColor(Enum):\n    RED = ...\n    BLUE = ...\n"],
    ["beta.pyi", "from enum import Enum\nclass BetaDateInput: ...\nclass BetaDateOutput: ...\nclass BetaColor(Enum):\n    RED = ...\n    BLUE = ...\n"],
  ]);
  await fs.mkdir(directory);
  const created = [];
  try {
    for (const [name, text] of files) {
      await fs.writeFile(path.join(directory, name), text, { flag: "wx" });
      created.push(name);
    }
  } catch (error) {
    try {
      for (const name of created) {
        if (await fs.readFile(path.join(directory, name), "utf8") === files.get(name)) {
          await fs.unlink(path.join(directory, name));
        }
      }
      await fs.rmdir(directory);
    } catch { /* Preserve anything that is not the exact fixture created above. */ }
    throw error;
  }
  const fixture = {
    packageName,
    async cleanup() {
      for (const [name, text] of files) {
        assert.equal(await fs.readFile(path.join(directory, name), "utf8"), text,
          "Do not remove a changed mapping fixture");
      }
      for (const name of [...files.keys()].reverse()) await fs.unlink(path.join(directory, name));
      await fs.rmdir(directory);
    },
  };
  mappingFixtureCleanup = fixture.cleanup;
  return fixture;
}

async function crashDuringPublication(workspace, stubRoot, temporary) {
  const marker = path.join(temporary, "interrupted-publication.json");
  const child = spawn(process.execPath, [path.join(__dirname, "crashDuringGeneration.cjs"), workspace, stubRoot, marker], {
    stdio: ["ignore", "ignore", "pipe"],
  });
  let stderr = "";
  let spawnError;
  child.stderr.setEncoding("utf8");
  child.stderr.on("data", value => { stderr += value; });
  child.on("error", error => { spawnError = error; });
  const stopChild = async () => {
    if (spawnError) return child.exitCode;
    if (child.exitCode !== null || child.signalCode !== null) return child.exitCode;
    const exited = once(child, "exit");
    child.kill("SIGKILL");
    const [code] = await exited;
    return code;
  };
  const deadline = Date.now() + 15000;
  while (Date.now() < deadline && child.exitCode === null && !fsSync.existsSync(marker)) {
    await new Promise(resolve => setTimeout(resolve, 25));
  }
  if (!fsSync.existsSync(marker)) {
    await stopChild();
    assert.fail(`The real generation writer did not reach the crash point: ${spawnError ?? stderr}`);
  }
  const interrupted = JSON.parse(await fs.readFile(marker, "utf8"));
  const generations = path.resolve(stubRoot, "generations");
  const relative = path.relative(generations, interrupted.directory);
  assert.ok(relative && !path.isAbsolute(relative) && relative !== ".." && !relative.startsWith(`..${path.sep}`),
    "The interrupted generation must stay under GraphQL Desk's managed directory");
  const code = await stopChild();
  assert.notEqual(code, 0, "The writer process must end before it can finish the manifest");
  assert.equal(fsSync.existsSync(path.join(interrupted.directory, "complete.json")), false,
    "The hard-stopped writer must leave an uncommitted generation");
  return { ...interrupted, bytes: await fs.readFile(interrupted.file) };
}

async function main() {
  const version = process.argv[2];
  assert.ok(["1.141.0", "1.142.0"].includes(version), "Choose a pinned host version");
  const suite = process.argv[3] || "features";
  assert.ok(["features", "discovery", "activation", "symbols", "ownedAnalysis", "factoryArtifacts", "typingMatrix", "mixedLists", "fragmentTyping", "externalMappings", "parserOverlay", "malformedRecovery", "workspaceRemoval", "restartRecovery", "restartRecoveryChangedInputs", "lifecycle"].includes(suite), "Choose a known isolated suite");
  const factorySuite = ["factoryArtifacts", "typingMatrix", "mixedLists", "fragmentTyping", "externalMappings", "parserOverlay", "malformedRecovery", "workspaceRemoval", "restartRecovery", "restartRecoveryChangedInputs", "lifecycle"].includes(suite);
  const temporaryRoot = path.resolve(__dirname, "../test-host-workspaces");
  await fs.mkdir(temporaryRoot, { recursive: true });
  const temporary = await fs.mkdtemp(path.join(temporaryRoot, "graphql-desk-features-"));
  try {
    await runHost(version, suite, factorySuite, temporary);
  } finally {
    await fs.rm(temporary, { recursive: true, force: true });
    await assert.rejects(fs.access(temporary), { code: "ENOENT" }, "Host-test temporary data must be removed");
  }
}

async function runHost(version, suite, factorySuite, temporary) {
  assert.deepEqual(await fs.readdir(temporary), [], "Every host run starts from a new temporary root");
  const workspace = path.join(temporary, "workspace");
  const profile = path.join(temporary, "profile");
  assert.equal(fsSync.existsSync(workspace), false, "The host workspace must not be reused");
  assert.equal(fsSync.existsSync(profile), false, "The host user-data directory must not be reused");
  const extensions = path.join(temporary, `extensions-${version}`);
  await fs.mkdir(extensions);
  assert.deepEqual(await fs.readdir(extensions), [], "Every host run starts with an empty extensions directory");
  const result = path.join(temporary, "result.json");
  const artifacts = path.join(temporary, "artifacts.json");
  const stubRoot = factorySuite ? path.join(workspace, ".graphql-desk", "typings") : path.join(workspace, "owned_analysis_stubs");
  const python = process.env.GRAPHQL_DESK_PYTHON || path.resolve(__dirname, "..", "..", ".venv", process.platform === "win32" ? "Scripts/python.exe" : "bin/python");
  assert.ok(path.isAbsolute(pytho…4370 tokens truncated… = beforeRestart.host;
    observed.lifecycle.afterRestartHost = observed.host;
    observed.lifecycle.beforeRestartStubPath = beforeRestart.lifecycle.stubPath;
    observed.lifecycle.writerProcessKilledDuringPublication = true;
    const selectedAfterRestart = path.resolve(workspace, observed.lifecycle.stubPath);
    assert.notEqual(selectedAfterRestart, interruptedPublication.directory, "Restart cannot select an incomplete generation");
    await fs.access(path.join(selectedAfterRestart, "complete.json"));
    assert.deepEqual(await readStubFiles(selectedGenerationBeforeRestart), selectedGenerationFilesBeforeRestart,
      "The previous complete generation remains unchanged across the interrupted publication");
    observed.lifecycle.processRestartSelectedCompleteGeneration = true;
    observed.lifecycle.previousCompleteGenerationUnchanged = true;
    assert.deepEqual(await fs.readFile(interruptedGenerationFile), interruptedPublication.bytes,
      "Recovery must ignore and preserve the artifact written before the publisher process was killed");
    await assert.rejects(fs.access(path.join(interruptedPublication.directory, "complete.json")), { code: "ENOENT" });
    observed.lifecycle.abandonedGenerationIgnoredAndPreserved = true;
    assert.equal(await fs.readFile(path.join(workspace, ".graphql-desk", "keep.txt"), "utf8"), "preserve this user file");
    observed.lifecycle.userFilePreserved = true;
    for (const [name, bytes] of userFilesBeforeInterruption) {
      let actual = await fs.readFile(path.join(workspace, name));
      if (name === ".vscode/settings.json") {
        const settings = JSON.parse(actual);
        assert.match(settings["python.analysis.stubPath"].replaceAll("\\", "/"),
          /^\.graphql-desk\/typings\/generations\/g-/,
          "Only GraphQL Desk's managed stubPath may rotate during restart recovery");
        delete settings["python.analysis.stubPath"];
        actual = Buffer.from(JSON.stringify(settings));
      }
      assert.deepEqual(actual, bytes, `${name} must survive interrupted publication and restart`);
    }
    observed.lifecycle.preExistingFilesPreserved = [...userFilesBeforeInterruption.keys()];
    await fs.writeFile(result, JSON.stringify(observed, null, 2));
  } else if (suite === "restartRecoveryChangedInputs") {
    assert.equal(observed.outcome, "owned-output-restart-observed");
    observed.lifecycle.beforeRestartHost = beforeRestart.host;
    observed.lifecycle.afterRestartHost = observed.host;
    observed.lifecycle.writerProcessKilledDuringPublication = true;
    const selectedAfterRestart = path.resolve(workspace, observed.lifecycle.stubPath);
    assert.notEqual(selectedAfterRestart, interruptedPublication.directory, "Restart cannot select an incomplete generation");
    await fs.access(path.join(selectedAfterRestart, "complete.json"));
    observed.lifecycle.processRestartSelectedCompleteGeneration = true;
    assert.deepEqual(await readStubFiles(selectedGenerationBeforeRestart), selectedGenerationFilesBeforeRestart,
      "The previous complete generation remains unchanged across the interrupted publication");
    observed.lifecycle.previousCompleteGenerationUnchanged = true;
    assert.ok(observed.lifecycle.staleInputQuarantined && observed.lifecycle.changedInputRepair,
      "Changed-input restart must quarantine stale output and recover after repair");
    assert.deepEqual(await fs.readFile(interruptedGenerationFile), interruptedPublication.bytes,
      "Recovery must ignore and preserve the artifact written before the publisher process was killed");
    await assert.rejects(fs.access(path.join(interruptedPublication.directory, "complete.json")), { code: "ENOENT" });
    observed.lifecycle.abandonedGenerationIgnoredAndPreserved = true;
    assert.equal(await fs.readFile(path.join(workspace, ".graphql-desk", "keep.txt"), "utf8"), "preserve this user file");
    observed.lifecycle.userFilePreserved = true;
    await fs.writeFile(result, JSON.stringify(observed, null, 2));
  } else if (factorySuite) {
    if (["typingMatrix", "mixedLists", "fragmentTyping", "externalMappings", "malformedRecovery"].includes(suite)) {
      await fs.access(path.join(observed.lifecycle.activeStubRoot, "complete.json"));
      await fs.access(path.join(observed.lifecycle.activeStubRoot, "graphql_desk_generated", "default.pyi"));
    } else if (suite === "parserOverlay") {
      await fs.access(path.join(observed.lifecycle.activeStubRoot, "complete.json"));
      await fs.access(path.join(observed.lifecycle.activeStubRoot, "graphql_desk-stubs", "__init__.pyi"));
      assert.equal(await fs.readFile(path.join(observed.lifecycle.activeStubRoot, "graphql_desk-stubs", "py.typed"), "utf8"), "partial\n");
    } else if (suite === "lifecycle") {
      assert.match(observed.lifecycle.configuredStubPath.replaceAll("\\", "/"), /^\.graphql-desk\/typings\/generations\/g-/,
        "live typing uses GraphQL Desk's managed immutable generation path");
    } else {
      await assert.rejects(fs.access(path.join(observed.lifecycle.activeStubRoot, "graphql_desk_generated")), { code: "ENOENT" }, "Test cleanup must remove only its owned generated package");
    }
    if (suite !== "lifecycle") {
      const generationsRoot = path.join(workspace, ".graphql-desk", "typings", "generations");
      const generations = await fs.readdir(generationsRoot);
      assert.ok(generations.some(name => name.startsWith("g-") && fsSync.existsSync(path.join(generationsRoot, name, "complete.json"))), "A complete immutable generation survives shutdown for restart recovery");
      await assert.rejects(fs.access(path.join(workspace, ".graphql-desk", "typings", "graphql_desk_generated")), { code: "ENOENT" }, "Generated artifacts stay isolated in immutable generations");
    }
    assert.equal(await fs.readFile(path.join(workspace, ".graphql-desk", "keep.txt"), "utf8"), "preserve this user file");
    const settings = JSON.parse(await fs.readFile(path.join(workspace, ".vscode", "settings.json"), "utf8"));
    const persistedStubPath = settings["python.analysis.stubPath"];
    if (suite === "lifecycle") {
      assert.match(persistedStubPath.replaceAll("\\", "/"), /^\.graphql-desk\/typings\/generations\/g-\d+-[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}$/i,
        "The persisted path remains a GraphQL Desk managed generation");
      const persistedRoot = path.resolve(workspace, persistedStubPath);
      await fs.access(path.join(persistedRoot, "complete.json"));
      const persistedFiles = await readStubFiles(persistedRoot);
      const withoutFingerprint = files => files.map(([file, text]) => [
        file, text.replace(/^# Generated by GraphQL Desk; analysis artifact [a-f0-9]{64}\n/, ""),
      ]);
      assert.deepEqual(withoutFingerprint(persistedFiles), withoutFingerprint(observed.lifecycle.activeStubFiles),
        "A later selected lifecycle generation keeps the generated typing observed after deletion");
      const persistedText = persistedFiles.map(([, text]) => text).join("\n");
      assert.equal(persistedText.includes("query Extra { user { id } }"), false,
        "The persisted generation does not restore the deleted source operation");
      assert.equal(persistedText.includes("'selected': 'String'"), false,
        "The persisted generation does not restore the removed operation field");
      if (persistedStubPath !== observed.lifecycle.configuredStubPath) {
        observed.lifecycle.persistedStubPath = persistedStubPath;
      }
    } else if ((suite === "typingMatrix" || suite === "externalMappings" || suite === "malformedRecovery") && persistedStubPath !== observed.lifecycle.configuredStubPath) {
      assert.match(persistedStubPath.replaceAll("\\", "/"), /^\.graphql-desk\/typings\/generations\/g-\d+-[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}$/i,
        "A later typing refresh may select another managed generation before shutdown");
      const persistedRoot = path.resolve(workspace, persistedStubPath);
      await fs.access(path.join(persistedRoot, "complete.json"));
      assert.equal(
        await fs.readFile(path.join(persistedRoot, "graphql_desk_generated", "default.pyi"), "utf8"),
        await fs.readFile(path.join(observed.lifecycle.activeStubRoot, "graphql_desk_generated", "default.pyi"), "utf8"),
        "The persisted generation keeps the exact typing Pylance observed",
      );
      observed.lifecycle.persistedStubPath = persistedStubPath;
    } else {
      assert.equal(persistedStubPath, observed.lifecycle.configuredStubPath);
    }
    assert.equal(settings["files.autoSave"], "off");
    observed.lifecycle.ownedArtifactsRemoved = true;
    observed.lifecycle.userConfigurationPreserved = true;
    await fs.writeFile(result, JSON.stringify(observed, null, 2));
  }
  assert.equal(observed.outcome, suite === "ownedAnalysis" ? "bounded-owned-analysis-observed" : suite === "factoryArtifacts" ? "generated-factory-artifacts-observed" : suite === "typingMatrix" ? "live-typing-matrix-observed" : suite === "mixedLists" ? "mixed-list-nullability-observed" : suite === "fragmentTyping" ? "fragment-masking-live-typing-observed" : suite === "externalMappings" ? "external-mappings-observed" : suite === "parserOverlay" ? "parser-overlay-observed" : suite === "malformedRecovery" ? "malformed-graphql-recovery-observed" : suite === "workspaceRemoval" ? "managed-workspace-removal-observed" : suite === "restartRecovery" || suite === "restartRecoveryChangedInputs" ? "owned-output-restart-observed" : suite === "lifecycle" ? "managed-output-lifecycle-observed" : `read-only-${suite}-observed`);
  console.log(`${suite} evidence: ${result}`);
}

main().catch(async error => {
  try { await mappingFixtureCleanup?.(); }
  catch (cleanupError) { console.error(`Mapping fixture cleanup failed safely: ${String(cleanupError)}`); }
  console.error(error);
  process.exitCode = 1;
});

