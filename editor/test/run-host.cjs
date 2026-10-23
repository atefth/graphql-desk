"use strict";
const assert = require("node:assert/strict");
const fsSync = require("node:fs");
const fs = require("node:fs/promises");
const os = require("node:os");
const path = require("node:path");
const { spawnSync } = require("node:child_process");
const { runTests, runVSCodeCommand } = require("@vscode/test-electron");

function generatedFactoryFiles(python, workspace, sourcePath, typingMatrix) {
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
    assert.ok(files.length > 1, "The host proof needs the real multi-file generator output");
    return files;
  }
  const initial = generate();
  const shapeFields = typingMatrix ? " nested: [[String!]!]! nullable: [String]" : "";
  fsSync.writeFileSync(path.join(workspace, "schema.graphql"), `type User { id: ID! name: String! }\ntype Query { selected: String count(limit: Int!): Int! user: User!${shapeFields} }\n`);
  const nullable = generate();
  fsSync.writeFileSync(path.join(workspace, "schema.graphql"), `type User { id: ID! name: String! }\ntype Query { selected: String! count(limit: Int!): Int! user: User!${shapeFields} }\n`);
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

async function main() {
  const version = process.argv[2];
  assert.ok(["1.141.0", "1.142.0"].includes(version), "Choose a pinned host version");
  const suite = process.argv[3] || "features";
  assert.ok(["features", "discovery", "activation", "symbols", "ownedAnalysis", "factoryArtifacts", "typingMatrix", "workspaceRemoval", "restartRecovery", "lifecycle"].includes(suite), "Choose a known isolated suite");
  const factorySuite = ["factoryArtifacts", "typingMatrix", "workspaceRemoval", "restartRecovery", "lifecycle"].includes(suite);
  const temporary = await fs.mkdtemp(path.join(os.tmpdir(), "graphql-desk-features-"));
  const workspace = path.join(temporary, "workspace");
  const profile = path.join(temporary, "profile");
  const extensions = path.resolve(__dirname, "..", ".vscode-test", `features-${version}`, "extensions");
  const result = path.join(temporary, "result.json");
  const artifacts = path.join(temporary, "artifacts.json");
  const stubRoot = factorySuite ? path.join(workspace, ".graphql-desk", "typings") : path.join(workspace, "owned_analysis_stubs");
  const python = process.env.GRAPHQL_DESK_PYTHON || path.resolve(__dirname, "..", "..", ".venv", process.platform === "win32" ? "Scripts/python.exe" : "bin/python");
  assert.ok(path.isAbsolute(python), "Choose an absolute trusted Python interpreter");
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
      fs.writeFile(path.join(workspace, "pyproject.toml"), '[tool.graphql-desk]\nschema="schema.graphql"\nschema_output="graphql_desk_generated/default.pyi"\n'),
      fs.writeFile(path.join(workspace, "schema.graphql"), `type User { id: ID! name: String! }\ntype Query { selected: String! count(limit: Int!): Int! user: User!${shapeFields} }\n`),
      fs.writeFile(sourcePath, source),
      fs.writeFile(path.join(workspace, "fragments.py"), "from graphql_desk_generated.default import graphql\ncard = graphql('fragment Card on User { name }')\n"),
      ...(suite === "lifecycle" ? [fs.writeFile(path.join(workspace, "lifecycle_calls.py"), "from graphql_desk_generated.default import graphql\nextra = graphql('query Extra { user { id } }')\n")] : []),
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
    const generated = generatedFactoryFiles(python, workspace, sourcePath, suite === "typingMatrix");
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
  for (const extension of ["ms-python.python@2026.10.0", "ms-python.vscode-pylance@2026.4.1"]) {
    await runVSCodeCommand([...isolation, "--install-extension", extension, "--force"], { version });
  }
  const options = {
    version,
    extensionDevelopmentPath: path.resolve(__dirname, ".."),
    extensionTestsPath: path.join(__dirname, `${suite === "symbols" ? "activation" : suite}.host.cjs`),
    extensionTestsEnv: {
      GRAPHQL_DESK_TEST_WORKSPACE: workspace,
      GRAPHQL_DESK_RESULT_PATH: result,
      GRAPHQL_DESK_PYTHON: python,
      GRAPHQL_DESK_ALTERNATE_PYTHON: process.env.GRAPHQL_DESK_ALTERNATE_PYTHON || "",
      GRAPHQL_DESK_HOST_VERSION: version,
      GRAPHQL_DESK_STUB_ROOT: stubRoot,
      GRAPHQL_DESK_ARTIFACTS_PATH: artifacts,
      GRAPHQL_DESK_SYMBOLS_ONLY: suite === "symbols" ? "1" : "",
    },
    launchArgs: [launchTarget, ...isolation, "--disable-updates", "--disable-telemetry", "--skip-welcome", "--skip-release-notes", "--disable-workspace-trust"],
  };
  const sessions = suite === "restartRecovery" ? ["before-restart", "after-restart"] : [null];
  let beforeRestart;
  let interruptedGenerationFile;
  for (const phase of sessions) {
    const sessionEnv = phase ? { ...options.extensionTestsEnv, GRAPHQL_DESK_RESTART_PHASE: phase } : options.extensionTestsEnv;
    await runTests({ ...options, extensionTestsEnv: sessionEnv });
    if (phase === "before-restart") {
      beforeRestart = JSON.parse(await fs.readFile(result, "utf8"));
      assert.equal(beforeRestart.outcome, "owned-output-restart-observed");
      const oldStubPath = beforeRestart.lifecycle.stubPath;
      assert.ok(oldStubPath.includes("generations"), "The first host must select a complete owned generation");
      await fs.access(path.join(workspace, oldStubPath, "complete.json"));
      const generationsRoot = path.join(workspace, ".graphql-desk", "typings", "generations");
      const interruptedGeneration = path.join(generationsRoot, "g-999-00000000-0000-0000-0000-000000000000");
      interruptedGenerationFile = path.join(interruptedGeneration, "graphql_desk_generated", "default.pyi");
      await fs.mkdir(path.dirname(interruptedGenerationFile), { recursive: true });
      await fs.writeFile(interruptedGenerationFile, "# Generated by GraphQL Desk; interrupted publication\nselected: partial\n");
    }
  }
  const observed = JSON.parse(await fs.readFile(result, "utf8"));
  if (suite === "restartRecovery") {
    observed.lifecycle.processRestartReusedCompleteGeneration = beforeRestart.lifecycle.stubPath === observed.lifecycle.stubPath;
    observed.lifecycle.beforeRestartHost = beforeRestart.host;
    observed.lifecycle.afterRestartHost = observed.host;
    observed.lifecycle.beforeRestartStubPath = beforeRestart.lifecycle.stubPath;
    assert.ok(observed.lifecycle.processRestartReusedCompleteGeneration, "Restart must recover the persisted complete generation");
    await fs.access(path.join(workspace, observed.lifecycle.stubPath, "complete.json"));
    assert.equal(await fs.readFile(interruptedGenerationFile, "utf8"),
      "# Generated by GraphQL Desk; interrupted publication\nselected: partial\n",
      "Recovery must ignore and preserve an abandoned incomplete generation");
    observed.lifecycle.abandonedGenerationIgnoredAndPreserved = true;
    assert.equal(await fs.readFile(path.join(workspace, ".graphql-desk", "keep.txt"), "utf8"), "preserve this user file");
    observed.lifecycle.userFilePreserved = true;
    await fs.writeFile(result, JSON.stringify(observed, null, 2));
  } else if (factorySuite) {
    if (suite === "typingMatrix") {
      await fs.access(path.join(observed.lifecycle.activeStubRoot, "complete.json"));
      await fs.access(path.join(observed.lifecycle.activeStubRoot, "graphql_desk_generated", "default.pyi"));
    } else if (suite === "lifecycle") {
      assert.match(observed.lifecycle.configuredStubPath.replaceAll("\\", "/"), /^\.graphql-desk\/typings\/generations\/g-/,
        "live typing uses GraphQL Desk's managed immutable generation path");
      await fs.access(path.join(observed.lifecycle.activeStubRoot, "complete.json"));
      await fs.access(path.join(observed.lifecycle.activeStubRoot, "graphql_desk_generated", "default.pyi"));
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
      assert.deepEqual(persistedFiles, observed.lifecycle.activeStubFiles,
        "A later selected lifecycle generation keeps the exact generated typing observed after deletion");
      const persistedText = persistedFiles.map(([, text]) => text).join("\n");
      assert.equal(persistedText.includes("query Extra { user { id } }"), false,
        "The persisted generation does not restore the deleted source operation");
      assert.equal(persistedText.includes("'selected': 'String'"), false,
        "The persisted generation does not restore the removed operation field");
      if (persistedStubPath !== observed.lifecycle.configuredStubPath) {
        observed.lifecycle.persistedStubPath = persistedStubPath;
      }
    } else if (suite === "typingMatrix" && persistedStubPath !== observed.lifecycle.configuredStubPath) {
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
  assert.equal(observed.outcome, suite === "ownedAnalysis" ? "bounded-owned-analysis-observed" : suite === "factoryArtifacts" ? "generated-factory-artifacts-observed" : suite === "typingMatrix" ? "live-typing-matrix-observed" : suite === "workspaceRemoval" ? "managed-workspace-removal-observed" : suite === "restartRecovery" ? "owned-output-restart-observed" : suite === "lifecycle" ? "managed-output-lifecycle-observed" : `read-only-${suite}-observed`);
  console.log(`${suite} evidence: ${result}`);
}

main().catch(error => { console.error(error); process.exitCode = 1; });
