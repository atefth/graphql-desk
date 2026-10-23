"use strict";
const assert = require("node:assert/strict");
const path = require("node:path");
const { spawnSync } = require("node:child_process");
const vscode = require("vscode");
const fs = require("node:fs/promises");

async function until(check, label, milliseconds = 30000) {
  const deadline = Date.now() + milliseconds;
  while (Date.now() < deadline) {
    if (await check()) return true;
    await new Promise(resolve => setTimeout(resolve, 100));
  }
  console.log(`NOT_OBSERVED: ${label}`);
  return false;
}

async function replace(document, text) {
  const edit = new vscode.WorkspaceEdit();
  edit.replace(document.uri, new vscode.Range(document.positionAt(0), document.positionAt(document.getText().length)), text);
  assert.equal(await vscode.workspace.applyEdit(edit), true);
}

function pythonVersion(python) {
  const result = spawnSync(python, ["-I", "-X", "utf8", "-c", "import sys; print(sys.version.split()[0])"], { encoding: "utf8" });
  assert.equal(result.status, 0, result.stderr);
  return result.stdout.trim();
}

exports.run = async () => {
  const results = { host: vscode.version, pylance: {}, lifecycle: { invalidationAndRepair: {} } };
  const workerRequests = [];
  try {
    for (const id of ["ms-python.python", "ms-python.vscode-pylance"]) {
      const extension = vscode.extensions.getExtension(id);
      assert.ok(extension, `${id} must be installed in the isolated host`);
      await extension.activate();
      const version = extension.packageJSON.version;
      assert.equal(version, id === "ms-python.python" ? "2026.10.0" : "2026.4.1");
      results.pylance[id] = version;
    }

    const workspace = process.env.GRAPHQL_DESK_TEST_WORKSPACE;
    const consumerUri = vscode.Uri.file(path.join(workspace, "consumer.py"));
    const extension = vscode.extensions.getExtension("graphql-desk.graphql-desk-editor");
    assert.ok(extension, "GraphQL Desk extension must be loaded in the host");
    const workerModule = require("../src/analysisWorker");
    const createWorker = workerModule.createAnalysisWorker;
    workerModule.createAnalysisWorker = options => {
      const worker = createWorker(options);
      return { ...worker, async request(message, settings) {
        const reply = await worker.request(message, settings);
        workerRequests.push({ python: options.python, op: message.op });
        return reply;
      } };
    };
    const consumer = await vscode.workspace.openTextDocument(consumerUri);
    await vscode.window.showTextDocument(consumer, { preview: false });
    await extension.activate();

    const uri = consumerUri;
    const document = consumer;
    const position = name => {
      const offset = document.getText().indexOf(name) + 1;
      assert.ok(offset > 0, `Expected fixture variable ${name}`);
      return document.positionAt(offset);
    };
    const hover = async name => {
      const items = await vscode.commands.executeCommand("vscode.executeHoverProvider", uri, position(name)) || [];
      return items.flatMap(item => item.contents.map(content => typeof content === "string" ? content : content.value)).join("\n");
    };
    const lineDiagnostics = name => vscode.languages.getDiagnostics(uri)
      .filter(item => item.range.start.line === position(name).line).map(item => item.message);
    const inferredType = (name, text) => text.match(new RegExp(`\\(variable\\) ${name}: ([^\\n]+)`))?.[1] || "";
    const observed = {};
    const ready = await until(async () => {
      observed.nested = await hover("nested_value");
      observed.nullable = await hover("nullable_value");
      observed.nestedType = inferredType("nested_value", observed.nested);
      observed.nullableType = inferredType("nullable_value", observed.nullable);
      observed.nestedErrors = lineDiagnostics("nested_bad");
      observed.nullableErrors = lineDiagnostics("nullable_bad");
      observed.defaultsErrors = lineDiagnostics("defaults_bad");
      observed.defaultAcceptedErrors = lineDiagnostics("defaults_result");
      return observed.nestedType === "list[list[String]]" &&
        observed.nullableType === "list[String | None] | None" &&
        observed.nestedErrors.length > 0 && observed.nullableErrors.length > 0 &&
        observed.defaultsErrors.length > 0 && observed.defaultAcceptedErrors.length === 0;
    }, "Pylance live typing for aliases, nested list wrappers, nullable lists and defaulted variables", 60000);
    assert.ok(ready, JSON.stringify(observed));
    results.pylance = { ...results.pylance, ...observed };

    const otherIdPosition = position("other_id");
    const otherIdHover = async () => {
      const items = await vscode.commands.executeCommand("vscode.executeHoverProvider", uri, otherIdPosition) || [];
      return items.flatMap(item => item.contents.map(content => typeof content === "string" ? content : content.value)).join("\n");
    };
    const selectedPosition = document.positionAt(document.getText().indexOf("selected = ") + 2);
    const selectedHover = async () => {
      const items = await vscode.commands.executeCommand("vscode.executeHoverProvider", uri, selectedPosition) || [];
      return items.flatMap(item => item.contents.map(content => typeof content === "string" ? content : content.value)).join("\n");
    };
    const schemaDocument = await vscode.workspace.openTextDocument(vscode.Uri.file(path.join(workspace, "schema.graphql")));
    const originalSchema = schemaDocument.getText();
    const schemaBoolean = originalSchema.replace("selected: String!", "selected: Boolean!");
    assert.notEqual(schemaBoolean, originalSchema, "Schema fixture must contain the selected String field");
    await replace(schemaDocument, schemaBoolean);
    results.lifecycle.invalidationAndRepair.schemaInvalidation = await until(async () => {
      const selectedType = await selectedHover();
      const unaffectedType = await otherIdHover();
      results.lifecycle.invalidationAndRepair.schemaInvalidationProbe = { selectedType, unaffectedType };
      return /Boolean|bool/.test(selectedType) && /\bID\b/.test(unaffectedType);
    }, "schema edit refreshes the selected operation while its sibling keeps the ID type", 60000);
    assert.ok(results.lifecycle.invalidationAndRepair.schemaInvalidation,
      JSON.stringify(results.lifecycle.invalidationAndRepair.schemaInvalidationProbe));
    await replace(schemaDocument, originalSchema);
    results.lifecycle.invalidationAndRepair.schemaRepair = await until(async () => {
      const selectedType = await selectedHover();
      const unaffectedType = await otherIdHover();
      results.lifecycle.invalidationAndRepair.schemaRepairProbe = { selectedType, unaffectedType };
      return /String/.test(selectedType) && /\bID\b/.test(unaffectedType);
    }, "schema repair restores the selected type without disturbing its sibling", 60000);
    assert.ok(results.lifecycle.invalidationAndRepair.schemaRepair,
      JSON.stringify(results.lifecycle.invalidationAndRepair.schemaRepairProbe));

    const configDocument = await vscode.workspace.openTextDocument(vscode.Uri.file(path.join(workspace, "pyproject.toml")));
    const originalConfig = configDocument.getText();
    const mappedConfig = originalConfig.replace("[tool.graphql-desk]\n",
      '[tool.graphql-desk]\nscalars = { String = { input = "builtins.str", output = "builtins.int" } }\n');
    assert.notEqual(mappedConfig, originalConfig, "Config fixture must have the GraphQL Desk table");
    await replace(configDocument, mappedConfig);
    results.lifecycle.invalidationAndRepair.configInvalidation = await until(async () => {
      const selectedType = await selectedHover();
      const unaffectedType = await otherIdHover();
      results.lifecycle.invalidationAndRepair.configInvalidationProbe = { selectedType, unaffectedType };
      return /StringOutput|\bint\b/.test(selectedType) && /\bID\b/.test(unaffectedType);
    }, "scalar mapping edit refreshes generated result typing and preserves the independent operation", 60000);
    assert.ok(results.lifecycle.invalidationAndRepair.configInvalidation,
      JSON.stringify(results.lifecycle.invalidationAndRepair.configInvalidationProbe));
    await replace(configDocument, originalConfig);
    results.lifecycle.invalidationAndRepair.configRepair = await until(async () => {
      const selectedType = await selectedHover();
      const unaffectedType = await otherIdHover();
      results.lifecycle.invalidationAndRepair.configRepairProbe = { selectedType, unaffectedType };
      return /\bString\b/.test(selectedType) && !/StringOutput|\bint\b/.test(selectedType) && /\bID\b/.test(unaffectedType);
    }, "config repair restores generated result typing and preserves the independent operation", 60000);
    assert.ok(results.lifecycle.invalidationAndRepair.configRepair,
      JSON.stringify(results.lifecycle.invalidationAndRepair.configRepairProbe));

    const alternatePython = process.env.GRAPHQL_DESK_ALTERNATE_PYTHON;
    assert.ok(path.isAbsolute(alternatePython ?? ""), "Set a second absolute Python environment for the interpreter-switch host proof");
    await fs.access(alternatePython);
    const deskConfiguration = vscode.workspace.getConfiguration("graphqlDesk", uri);
    const originalPython = deskConfiguration.get("pythonPath", "");
    const originalRequestCount = workerRequests.length;
    await deskConfiguration.update("pythonPath", alternatePython, vscode.ConfigurationTarget.Workspace);
    results.lifecycle.interpreterChange = {
      from: originalPython,
      to: alternatePython,
      fromPythonVersion: pythonVersion(originalPython || process.env.GRAPHQL_DESK_PYTHON),
      toPythonVersion: pythonVersion(alternatePython),
    };
    const interpreterSchema = await vscode.workspace.openTextDocument(vscode.Uri.file(path.join(workspace, "schema.graphql")));
    const interpreterOriginalSchema = interpreterSchema.getText();
    const interpreterBooleanSchema = interpreterOriginalSchema.replace("selected: String!", "selected: Boolean!");
    assert.notEqual(interpreterBooleanSchema, interpreterOriginalSchema);
    await replace(interpreterSchema, interpreterBooleanSchema);
    results.lifecycle.interpreterChange.refreshed = await until(async () => {
      const selectedType = await selectedHover();
      const unaffectedType = await otherIdHover();
      const requests = workerRequests.slice(originalRequestCount);
      const usedAlternate = requests.some(value => value.op === "artifacts" && path.resolve(value.python).toLowerCase() === path.resolve(alternatePython).toLowerCase());
      results.lifecycle.interpreterChange.probe = { selectedType, unaffectedType, usedAlternate, requests };
      return usedAlternate && /Boolean|bool/.test(selectedType) && /\bID\b/.test(unaffectedType);
    }, "Pylance typing recovers after switching the configured worker interpreter", 60000);
    assert.ok(results.lifecycle.interpreterChange.refreshed,
      JSON.stringify(results.lifecycle.interpreterChange.probe));
    const alternateRequestCount = workerRequests.length;
    await deskConfiguration.update("pythonPath", originalPython, vscode.ConfigurationTarget.Workspace);
    await replace(interpreterSchema, interpreterOriginalSchema);
    results.lifecycle.interpreterChange.restored = await until(async () => {
      const requests = workerRequests.slice(alternateRequestCount);
      const selectedType = await selectedHover();
      const unaffectedType = await otherIdHover();
      const usedOriginal = requests.some(value => value.op === "artifacts" && path.resolve(value.python).toLowerCase() ===
        path.resolve(originalPython || process.env.GRAPHQL_DESK_PYTHON).toLowerCase());
      results.lifecycle.interpreterChange.restoreProbe = { selectedType, unaffectedType, usedOriginal, requests };
      return usedOriginal && /String/.test(selectedType) && /\bID\b/.test(unaffectedType);
    }, "Pylance typing recovers after restoring the original worker interpreter", 60000);
    assert.ok(results.lifecycle.interpreterChange.restored);

    const configuration = vscode.workspace.getConfiguration("python", uri);
    results.lifecycle.configuredStubPath = configuration.get("analysis.stubPath");
    results.lifecycle.activeStubRoot = path.resolve(workspace, results.lifecycle.configuredStubPath || ".graphql-desk/typings");
    assert.ok(results.lifecycle.configuredStubPath);
    results.outcome = "live-typing-matrix-observed";
  } catch (error) {
    results.error = String(error.stack || error);
    results.outcome = "live-typing-matrix-failed";
    throw error;
  } finally {
    await fs.writeFile(process.env.GRAPHQL_DESK_RESULT_PATH, JSON.stringify(results, null, 2));
    console.log(`GRAPHQL_DESK_TYPING_MATRIX ${JSON.stringify(results)}`);
  }
};
