"use strict";
const assert = require("node:assert/strict");
const { randomUUID } = require("node:crypto");
const fsSync = require("node:fs");
const fs = require("node:fs/promises");
const os = require("node:os");
const path = require("node:path");
const { spawn, spawnSync } = require("node:child_process");
const { once } = require("node:events");
const { runTests, runVSCodeCommand } = require("@vscode/test-electron");
let mappingFixtureCleanup;

function generatedFactoryFiles(python, workspace, sourcePath, typingMatrix, externalMappings) {
  const base = { workspace, candidates: [sourcePath], buffers: [], documents: [], editor_fallback: null };
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
  const shapeFields = typingMatrix ? " nested: [[String!]!]! nullable: [String]" : "";
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
  const file = path.join(directory, "__init__.pyi");
  const modelsFile = path.join(directory, "models.pyi");
  const text = "from .models import BaseModel as BaseModel, Color as Color\n";
  const modelsText = "from enum import Enum\nclass BaseModel: ...\nclass Color(Enum):\n    RED = ...\n    BLUE = ...\n";
  await fs.mkdir(directory);
  let modelsCreated = false;
  try {
    await fs.writeFile(file, text, { flag: "wx" });
    await fs.writeFile(modelsFile, modelsText, { flag: "wx" });
    modelsCreated = true;
  } catch (error) {
    try {
      if (await fs.readFile(file, "utf8") === text) await fs.unlink(file);
      if (modelsCreated && await fs.readFile(modelsFile, "utf8") === modelsText) await fs.unlink(modelsFile);
      await fs.rmdir(directory);
    } catch { /* Preserve anything that is not the exact fixture created above. */ }
    throw error;
  }
  const fixture = {
    packageName,
    async cleanup() {
      assert.equal(await fs.readFile(file, "utf8"), text, "Do not remove a changed mapping fixture");
      assert.equal(await fs.readFile(modelsFile, "utf8"), modelsText,
        "Do not remove a changed external base fixture");
      await fs.unlink(modelsFile);
      await fs.unlink(file);
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
  assert.ok(["features", "discovery", "activation", "symbols", "ownedAnalysis", "factoryArtifacts", "typingMatrix", "externalMappings", "parserOverlay", "malformedRecovery", "workspaceRemoval", "restartRecovery", "restartRecoveryChangedInputs", "lifecycle"].includes(suite), "Choose a known isolated suite");
  const factorySuite = ["factoryArtifacts", "typingMatrix", "externalMappings", "parserOverlay", "malformedRecovery", "workspaceRemoval", "restartRecovery", "restartRecoveryChangedInputs", "lifecycle"].includes(suite);
  const temporary = await fs.mkdtemp(path.join(os.tmpdir(), "graphql-desk-features-"));
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
  assert.ok(path.isAbsolute(python), "Choose an absolute trusted Python interpreter");
  const mappingFixture = suite === "externalMappings" ? await installMappingStub(python) : null;
  await fs.mkdir(path.join(workspace, ".vscode"), { recursive: true });
  await fs.writeFile(path.join(workspace, ".vscode", "settings.json"), JSON.stringify({
    "extensions.autoUpdate": false,
    "extensions.autoCheckUpdates": false,
    "telemetry.telemetryLevel": "off",
    "files.autoSave": "off",
    "python.defaultInterpreterPath": python,
    "graphqlDesk.pythonPath": factorySuite ? python : "",
    "python.experiments.enabled": false,
    "python.analysis.aiHoverSummaries": false,
    ...(suite === "ownedAnalysis" ? { "python.analysis.typeCheckingMode": "strict", "python.analysis.stubPath": stubRoot } : {}),
    ...(factorySuite ? { "python.analysis.typeCheckingMode": "strict" } : {}),
    ...(suite === "workspaceRemoval" ? { "python.analysis.stubPath": ".graphql-desk/typings" } : {}),
  }));
  if (suite === "ownedAnalysis") {
    await fs.mkdir(stubRoot);
    const files = {
      "owned_analysis_stubs/preexisting.txt": "preserve",
      "generated.pyi": "from typing import TypedDict\nclass Result(TypedDict):\n    selected: str\ndef execute() -> Result: ...\n",
      "generated.py": "def execute():\n    raise NotImplementedError\n",
      "consumer.py": "import generated\nresult = generated.execute()\nselected = result['selected']\nneeds_str: str = selected\n",
      "user.py": "value = 1\n",
    };
    await Promise.all(Object.entries(files).map(([name, text]) => fs.writeFile(path.join(workspace, name), text)));
  }
  if (factorySuite) {
    await fs.mkdir(path.join(workspace, ".graphql-desk"));
    await fs.writeFile(path.join(workspace, ".graphql-desk", "keep.txt"), "preserve this user file");
    const sourcePath = path.join(workspace, "consumer.py");
    const source = [
      "from graphql_desk import TypedDocument",
      "from graphql_desk_generated.default import graphql",
      "from typing import TypeVar",
      "R = TypeVar('R')",
      "V = TypeVar('V')",
      "def execute(document: TypedDocument[R, V], variables: V) -> R: ...",
      "doc = graphql('query Pick($limit: Int!) { selected count(limit: $limit) }')",
      "result = execute(doc, {'limit': 1})",
      "selected = result['selected']",
      "count = result['count']",
      "needs_str: str = selected",
      "wrong = execute(doc, {'limit': 'bad'})",
      ...(suite === "externalMappings" ? [
        "from models import DateInput, DateOutput",
        `from ${mappingFixture.packageName} import Color`,
        "mapped_doc = graphql('query Mapped($date: ExternalDate!, $color: ExternalColor!) { externalDate(value: $date) externalColor(value: $color) }')",
        "mapped_result = execute(mapped_doc, {'date': DateInput(), 'color': Color()})",
        "mapped_date = mapped_result['externalDate']",
        "mapped_color = mapped_result['externalColor']",
        "mapped_date_ok: DateOutput = mapped_date",
        "mapped_color_ok: Color = mapped_color",
        "mapped_date_bad: str = mapped_date",
        "mapped_color_bad: str = mapped_color",
        "mapped_wrong_date = execute(mapped_doc, {'date': DateOutput(), 'color': Color()})",
        "mapped_wrong_color = execute(mapped_doc, {'date': DateInput(), 'color': 'RED'})",
      ] : []),
      "other_doc = graphql('query Other { user { id } }')",
      "other_result = execute(other_doc, {})",
      "other_id = other_result['user']['id']",
      "other_needs_id: str = other_id",
      ...(suite === "typingMatrix" ? [
        "nested_doc = graphql('query Nested { alias: nested }')",
        "nested_result = execute(nested_doc, {})",
        "nested_value = nested_result['alias']",
        "nested_bad: str = nested_value",
        "nullable_doc = graphql('query Nullable { nullable }')",
        "nullable_result = execute(nullable_doc, {})",
        "nullable_value = nullable_result['nullable']",
        "nullable_bad: list[str] = nullable_value",
        "defaults_doc = graphql('query Defaults($limit: Int! = 1) { count(limit: $limit) }')",
        "defaults_result = execute(defaults_doc, {})",
        "defaults_bad = execute(defaults_doc, {'limit': None})",
      ] : []),
      "",
    ].join("\n");
    const shapeFields = suite === "typingMatrix" ? " nested: [[String!]!]! nullable: [String]" : "";
    await Promise.all([
      ...(suite === "externalMappings" ? [fs.writeFile(path.join(workspace, "models.py"),
        `from ${mappingFixture.packageName} import BaseModel\nclass DateInput(BaseModel): ...\nclass DateOutput(BaseModel): ...\n`)] : []),
      fs.writeFile(path.join(workspace, "pyproject.toml"), '[tool.graphql-desk]\nschema="schema.graphql"\nschema_output="graphql_desk_generated/default.pyi"\n' + (suite === "externalMappings" ? `scalars = { ExternalDate = { input = "models.DateInput", output = "models.DateOutput" } }\nenums = { ExternalColor = "${mappingFixture.packageName}.Color" }\n` : "")),
      fs.writeFile(path.join(workspace, "schema.graphql"), `${suite === "externalMappings" ? "scalar ExternalDate\nenum ExternalColor { RED BLUE }\n" : ""}type User { id: ID! name: String! }\ntype Query { selected: String! count(limit: Int!): Int! user: User!${suite === "externalMappings" ? " externalDate(value: ExternalDate!): ExternalDate! externalColor(value: ExternalColor!): ExternalColor!" : ""}${shapeFields} }\n`),
      fs.writeFile(sourcePath, source),
      ...(["typingMatrix", "parserOverlay"].includes(suite) ? [fs.writeFile(path.join(workspace, "parser_consumer.py"),
        "from graphql_desk import parse\ndocument = parse('query ParserQuery { selected }')\nparsed_name = document.definitions[0].name.value\n")] : []),
      fs.writeFile(path.join(workspace, "fragments.py"), "from graphql_desk_generated.default import graphql\ncard = graphql('fragment Card on User { name }')\n"),
      ...(suite === "lifecycle" ? [fs.writeFile(path.join(workspace, "lifecycle_calls.py"), "from graphql_desk_generated.default import graphql\nextra = graphql('query Extra { user { id } }')\n")] : []),
      ...(suite === "lifecycle" ? [fs.writeFile(path.join(workspace, "preserved_user.py"), "value = 'saved'\n")] : []),
    ]);
    const runtimePackage = path.join(workspace, "graphql_desk_generated");
    await fs.mkdir(path.join(runtimePackage, "sources"), { recursive: true });
    await Promise.all([
      fs.writeFile(path.join(runtimePackage, "__init__.py"), ""),
      fs.writeFile(path.join(runtimePackage, "default.py"), 'from graphql_desk import init_graphql\ngraphql = init_graphql("default")\n'),
      fs.writeFile(path.join(runtimePackage, "sources", "__init__.py"), ""),
      fs.writeFile(path.join(runtimePackage, "sources", "fragments.py"), [
        "from graphql_desk_generated.default import graphql",
        "card = graphql('fragment Card on User { name }')",
        "def read_card(data): return data",
        "",
      ].join("\n")),
    ]);
    const generated = generatedFactoryFiles(python, workspace, sourcePath, suite === "typingMatrix", suite === "externalMappings");
    await fs.writeFile(artifacts, JSON.stringify(generated));
    console.log(`Captured ${generated.initial.length} initial and ${generated.nullable.length} nullable factory stub files.`);
  }
  const isolation = [`--user-data-dir=${profile}`, `--extensions-dir=${extensions}`];
  let launchTarget = workspace;
  if (suite === "workspaceRemoval") {
    const removedWorkspace = path.join(temporary, "removed-workspace");
    await fs.mkdir(removedWorkspace);
    launchTarget = path.join(temporary, "lifecycle.code-workspace");
    await fs.writeFile(launchTarget, JSON.stringify({
      folders: [{ path: workspace }, { path: removedWorkspace }],
      settings: JSON.parse(await fs.readFile(path.join(workspace, ".vscode", "settings.json"), "utf8")),
    }));
  }
  if (suite !== "discovery") {
    for (const extension of ["ms-python.python@2026.10.0", "ms-python.vscode-pylance@2026.4.1"]) {
      await runVSCodeCommand([...isolation, "--install-extension", extension, "--force"], { version });
    }
  }
  const options = {
    version,
    extensionDevelopmentPath: path.resolve(__dirname, ".."),
    extensionTestsPath: path.join(__dirname, `${suite === "symbols" ? "activation" : suite === "restartRecoveryChangedInputs" ? "restartRecovery" : ["parserOverlay", "externalMappings"].includes(suite) ? "typingMatrix" : suite}.host.cjs`),
    extensionTestsEnv: {
      GRAPHQL_DESK_TEST_WORKSPACE: workspace,
      GRAPHQL_DESK_RESULT_PATH: result,
      GRAPHQL_DESK_PYTHON: python,
      GRAPHQL_DESK_ALTERNATE_PYTHON: process.env.GRAPHQL_DESK_ALTERNATE_PYTHON || "",
      GRAPHQL_DESK_HOST_VERSION: version,
      GRAPHQL_DESK_TEST_SUITE: suite,
      GRAPHQL_DESK_STUB_ROOT: stubRoot,
      GRAPHQL_DESK_ARTIFACTS_PATH: artifacts,
      GRAPHQL_DESK_SYMBOLS_ONLY: suite === "symbols" ? "1" : "",
    },
    launchArgs: [launchTarget, ...isolation, "--disable-updates", "--disable-telemetry", "--skip-welcome", "--skip-release-notes", "--disable-workspace-trust"],
  };
  const sessions = suite === "restartRecovery" ? ["before-restart", "after-restart"]
    : suite === "restartRecoveryChangedInputs" ? ["before-restart", "after-input-change"] : [null];
  let beforeRestart;
  let interruptedGenerationFile;
  let interruptedPublication;
  let selectedGenerationBeforeRestart;
  let selectedGenerationFilesBeforeRestart;
  let userFilesBeforeInterruption;
  for (const phase of sessions) {
    const sessionEnv = phase ? { ...options.extensionTestsEnv, GRAPHQL_DESK_RESTART_PHASE: phase } : options.extensionTestsEnv;
    try {
      await runTests({ ...options, extensionTestsEnv: sessionEnv });
    } finally {
      if (mappingFixtureCleanup) {
        await mappingFixtureCleanup();
        mappingFixtureCleanup = undefined;
      }
    }
    if (phase === "before-restart") {
      beforeRestart = JSON.parse(await fs.readFile(result, "utf8"));
      assert.equal(beforeRestart.outcome, "owned-output-restart-observed");
      const oldStubPath = beforeRestart.lifecycle.stubPath;
      assert.ok(oldStubPath.includes("generations"), "The first host must select a complete owned generation");
      selectedGenerationBeforeRestart = path.resolve(workspace, oldStubPath);
      await fs.access(path.join(selectedGenerationBeforeRestart, "complete.json"));
      selectedGenerationFilesBeforeRestart = await readStubFiles(selectedGenerationBeforeRestart);
      if (suite === "restartRecovery") {
        userFilesBeforeInterruption = new Map(await Promise.all([
          ".vscode/settings.json", "pyproject.toml", "schema.graphql", "consumer.py",
          "graphql_desk_generated/default.py", "graphql_desk_generated/sources/fragments.py", ".graphql-desk/keep.txt",
        ].map(async name => [name, await fs.readFile(path.join(workspace, name))])));
        const settings = JSON.parse(userFilesBeforeInterruption.get(".vscode/settings.json"));
        delete settings["python.analysis.stubPath"];
        userFilesBeforeInterruption.set(".vscode/settings.json", Buffer.from(JSON.stringify(settings)));
      }
      interruptedPublication = await crashDuringPublication(workspace, stubRoot, temporary);
      interruptedGenerationFile = interruptedPublication.file;
      if (suite === "restartRecoveryChangedInputs") {
        await fs.writeFile(path.join(workspace, "schema.graphql"),
          "type User { id: ID! name: String! }\ntype Query { count(limit: Int!): Int! user: User! }\n");
      }
    }
  }
  const observed = JSON.parse(await fs.readFile(result, "utf8"));
  if (observed.lifecycle) observed.lifecycle.freshWorkspaceUserDataAndExtensions = true;
  else observed.isolatedHostDirectoriesFresh = true;
  if (suite === "restartRecovery") {
    observed.lifecycle.beforeRestartHost = beforeRestart.host;
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
    if (["typingMatrix", "externalMappings", "malformedRecovery"].includes(suite)) {
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
  assert.equal(observed.outcome, suite === "ownedAnalysis" ? "bounded-owned-analysis-observed" : suite === "factoryArtifacts" ? "generated-factory-artifacts-observed" : suite === "typingMatrix" ? "live-typing-matrix-observed" : suite === "externalMappings" ? "external-mappings-observed" : suite === "parserOverlay" ? "parser-overlay-observed" : suite === "malformedRecovery" ? "malformed-graphql-recovery-observed" : suite === "workspaceRemoval" ? "managed-workspace-removal-observed" : suite === "restartRecovery" || suite === "restartRecoveryChangedInputs" ? "owned-output-restart-observed" : suite === "lifecycle" ? "managed-output-lifecycle-observed" : `read-only-${suite}-observed`);
  console.log(`${suite} evidence: ${result}`);
}

main().catch(async error => {
  try { await mappingFixtureCleanup?.(); }
  catch (cleanupError) { console.error(`Mapping fixture cleanup failed safely: ${String(cleanupError)}`); }
  console.error(error);
  process.exitCode = 1;
});
