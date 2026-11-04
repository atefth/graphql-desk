"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs/promises");
const path = require("node:path");
const vscode = require("vscode");

async function until(action, accept, description, timeout = 45000) {
  const deadline = Date.now() + timeout;
  let value;
  do {
    value = await action();
    if (accept(value)) return value;
    await new Promise(resolve => setTimeout(resolve, 100));
  } while (Date.now() < deadline);
  assert.fail(`${description}: ${JSON.stringify(value)?.slice(0, 5000)}`);
}

async function run() {
  const workspace = process.env.GRAPHQL_DESK_TEST_WORKSPACE;
  const resultPath = process.env.GRAPHQL_DESK_RESULT_PATH;
  const configFile = path.join(workspace, "pyrightconfig.json");
  const schemaFile = path.join(workspace, "schema.graphql");
  const projectToml = process.env.GRAPHQL_DESK_TEST_SUITE === "projectConfigToml";
  const phase = process.env.GRAPHQL_DESK_RESTART_PHASE;
  const config = () => {
    const fsSync = require("node:fs");
    if (!projectToml) return JSON.parse(fsSync.readFileSync(configFile, "utf8"));
    const text = fsSync.readFileSync(path.join(workspace, "pyproject.toml"), "utf8");
    const setting = /^stubPath\s*=\s*"([^"]+)"/m.exec(text)?.[1];
    const extraPaths = [...(text.match(/^extraPaths\s*=\s*\[([^\]]*)\]/m)?.[1] ?? "").matchAll(/"([^"]+)"/g)].map(match => match[1]);
    const environmentPaths = [...(text.match(/\[\[tool\.pyright\.executionEnvironments\]\][\s\S]*?extraPaths\s*=\s*\[([^\]]*)\]/m)?.[1] ?? "").matchAll(/"([^"]+)"/g)].map(match => match[1]);
    return { stubPath: setting, extraPaths, executionEnvironments: [{ extraPaths: environmentPaths }] };
  };
  const pythonStubPath = async () => {
    const filename = path.join(workspace, ".vscode", "settings.json");
    for (let attempt = 0; attempt < 8; attempt += 1) {
      try { return JSON.parse(await fs.readFile(filename, "utf8"))["python.analysis.stubPath"]; }
      catch (error) {
        if (!(error instanceof SyntaxError) || attempt === 7) throw error;
        await new Promise(resolve => setTimeout(resolve, 20));
      }
    }
  };

  for (const [id, version] of [["ms-python.python", "2026.10.0"], ["ms-python.vscode-pylance", "2026.4.1"]]) {
    const native = vscode.extensions.getExtension(id);
    assert.ok(native);
    assert.equal(native.packageJSON.version, version);
    await native.activate();
  }
  const managedErrors = [];
  const publicationErrors = [];
  const skipReasons = [];
  const createOutputChannel = vscode.window.createOutputChannel;
  vscode.window.createOutputChannel = (name, ...args) => {
    const channel = createOutputChannel.call(vscode.window, name, ...args);
    if (name !== "GraphQL Desk") return channel;
    return new Proxy(channel, { get(target, property) {
      if (property === "appendLine") return value => {
        if (typeof value === "string" && value.startsWith("Managed stub root unavailable: ") && managedErrors.length < 20) managedErrors.push(value);
        if (typeof value === "string" && value.startsWith("Managed stub publication failed: ") && publicationErrors.length < 20) publicationErrors.push(value);
        if (typeof value === "string" && value.startsWith("Typing quarantine failed: ") && publicationErrors.length < 20) publicationErrors.push(value);
        if (typeof value === "string" && value.startsWith("Live typing skipped: ") && skipReasons.length < 40) skipReasons.push(value);
        if (typeof value === "string" && value.startsWith("GraphQL Desk trace:") && skipReasons.length < 40) skipReasons.push(value);
        return target.appendLine(value);
      };
      const value = Reflect.get(target, property, target);
      return typeof value === "function" ? value.bind(target) : value;
    } });
  };
  const extension = vscode.extensions.getExtension("graphql-desk.graphql-desk-editor");
  assert.ok(extension);
  await extension.activate();

  const file = path.join(workspace, "consumer.py");
  let document = await vscode.workspace.openTextDocument(vscode.Uri.file(file));
  if (document.languageId !== "python") document = await vscode.languages.setTextDocumentLanguage(document, "python");
  await vscode.window.showTextDocument(document);
  const selectedLine = document.getText().indexOf("needs_str: str = selected");
  assert.notEqual(selectedLine, -1);
  const selectedOffset = selectedLine + "needs_str: str = ".length;
  const hover = async () => {
    const position = document.positionAt(selectedOffset);
    return (await vscode.commands.executeCommand("vscode.executeHoverProvider", document.uri, position) ?? [])
      .flatMap(item => item.contents ?? []).map(item => typeof item === "string" ? item : item.value ?? "").join("\n");
  };
  const generation = async () => {
    const current = config();
    const setting = current.stubPath;
    if (typeof setting !== "string" || !setting.includes("generations")) return { setting, current, pythonSetting: await pythonStubPath(),
      managedErrors: [...managedErrors], publicationErrors: [...publicationErrors], skipReasons: [...skipReasons] };
    try {
      const text = await hover();
      const generationRoot = path.resolve(workspace, setting);
      const stubFile = path.join(generationRoot, "graphql_desk_generated", "_documents", "default", "d0.pyi");
      const manifest = JSON.parse(await fs.readFile(path.join(generationRoot, "complete.json"), "utf8"));
      const importLine = document.getText().split(/\r?\n/).find(line => line.startsWith("from graphql_desk_generated.default import graphql"));
      const graphqlOffset = document.getText().indexOf("graphql('query Pick");
      const definitions = graphqlOffset < 0 ? [] : await vscode.commands.executeCommand("vscode.executeDefinitionProvider", document.uri,
        document.positionAt(graphqlOffset));
      const generatedStub = await fs.readFile(stubFile, "utf8").catch(() => "");
      return { setting, pythonSetting: await pythonStubPath(), text,
        generatedStubExists: await fs.access(stubFile).then(() => true, () => false),
        generatedStub: generatedStub.split(/\r?\n/).filter(line => /selected/.test(line)).join("\n"),
        generatedFiles: manifest.files.map(item => item.path),
        operationCount: manifest.operationManifest?.reduce((sum, group) => sum + group.operations.length, 0) ?? 0,
        consumerImport: importLine,
        diagnostics: vscode.languages.getDiagnostics(document.uri).map(item => ({ message: item.message, code: item.code, severity: item.severity })),
        managedErrors: [...managedErrors], publicationErrors: [...publicationErrors], skipReasons: [...skipReasons],
        graphqlDefinitions: (definitions ?? []).map(item => ({ uri: item.uri?.toString?.(), range: item.range })) };
    } catch (error) {
      if (error.code === "ENOENT") return { setting, pythonSetting: await pythonStubPath(), missing: error.path,
        managedErrors: [...managedErrors], publicationErrors: [...publicationErrors], skipReasons: [...skipReasons] };
      throw error;
    }
  };
  let initialType = null;
  let refreshedType = null;
  let current;
  if (phase === "before-restart") {
    const configured = await until(generation, value => value?.generatedStubExists && value.operationCount > 0,
      "GraphQL Desk publishes a complete operation generation and resolves the generated package path", 45000);
    assert.equal(configured.consumerImport, "from graphql_desk_generated.default import graphql");
    await vscode.commands.executeCommand("python.analysis.restartLanguageServer");
    const first = await until(generation, value => value?.text.includes("String") && !value.text.includes("None"),
      "Pylance infers the non-null project-config result after its managed settings refresh");
    assert.ok(first.generatedStub.includes("selected"), `The generated operation result contains selected: ${JSON.stringify(first)}`);
    assert.ok(first.graphqlDefinitions.some(item => item.uri?.endsWith("graphql_desk_generated/default.py")),
      `Pylance resolves the GraphQL factory import to its runtime module while using the generated overload stub: ${JSON.stringify(first.graphqlDefinitions)}`);
    initialType = first.text;
    assert.match(first.generatedStub, /selected.*String/,
      `The generated operation stub has its expected field: ${JSON.stringify(first)}`);
    assert.match(first.setting.replaceAll("\\", "/"), /^\.graphql-desk\/live-typings\/generations\/g-/);
    assert.equal(await pythonStubPath(), first.setting, "The managed Pylance setting and effective project setting select the same complete generation");
    await fs.access(path.join(path.resolve(workspace, first.setting), "complete.json"));
    const before = await fs.readFile(schemaFile, "utf8");
    assert.match(before, /selected: String!/);
    await fs.writeFile(schemaFile, before.replace("selected: String!", "selected: String"), "utf8");
    current = await until(generation, value => value?.setting !== first.setting && value.text?.includes("None") && value.pythonSetting === value.setting,
      "Pylance refreshes live from a nullable schema edit and selects the same completed generation as Pyright");
    assert.equal(await pythonStubPath(), current.setting, "Pylance setting rotates with the completed project generation");
    refreshedType = current.text;
  } else {
    current = await until(generation, value => value?.text?.includes("None"),
      "Pylance recovers the selected project-config generation after a VS Code restart");
    initialType = current.text;
    refreshedType = current.text;
  }
  const beforeCleanupConfig = config();
  if (!projectToml) {
    assert.deepEqual(config().extraPaths, ["vendor", "custom-stubs"]);
    if (process.env.GRAPHQL_DESK_TEST_SUITE === "projectConfigEnvironment") {
      assert.deepEqual(config().executionEnvironments[0].extraPaths, ["environment-vendor"]);
    }
    const toml = await fs.readFile(path.join(workspace, "pyproject.toml"), "utf8");
    assert.match(toml, /stubPath = "toml-ignored"/, "pyrightconfig.json takes precedence over pyproject.toml");
  } else {
    assert.deepEqual(config().extraPaths, ["vendor", "custom-stubs"]);
    assert.deepEqual(config().executionEnvironments[0].extraPaths, ["environment-vendor"]);
  }
  await fs.access(path.join(path.resolve(workspace, current.setting), "complete.json"));
  const evidence = { outcome: "project-config-live-typing-observed", vscode: vscode.version,
    pythonExtension: vscode.extensions.getExtension("ms-python.python").packageJSON.version,
    pylance: vscode.extensions.getExtension("ms-python.vscode-pylance").packageJSON.version,
    managedConfigGeneration: current.setting, initialPylanceType: initialType, refreshedPylanceType: refreshedType };
  if (phase === "before-restart") {
    await fs.writeFile(resultPath, JSON.stringify(evidence, null, 2));
    return;
  }
  await require("../src/extension").deactivate();
  await until(async () => config(), value => value.stubPath === "custom-stubs" && value.extraPaths?.join("\0") === "vendor",
    "Deactivation restores the user's project config");
  await until(pythonStubPath, value => value === "python-custom-stubs", "Deactivation restores the user's Pylance setting");
  evidence.restoredProjectStubPath = config().stubPath;
  evidence.projectConfigBeforeCleanup = beforeCleanupConfig;
  evidence.projectConfigAfterCleanup = config();
  evidence.preservedExtraPaths = config().extraPaths;
  await fs.writeFile(resultPath, JSON.stringify(evidence, null, 2));
}

module.exports = { run };
