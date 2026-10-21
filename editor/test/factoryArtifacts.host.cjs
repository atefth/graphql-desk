"use strict";
const assert = require("node:assert/strict");
const path = require("node:path");
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

exports.run = async () => {
  const results = { host: vscode.version, pylance: {}, generatedFiles: true, lifecycle: {} };
  try {
    for (const id of ["ms-python.python", "ms-python.vscode-pylance"]) {
      const extension = vscode.extensions.getExtension(id);
      assert.ok(extension);
      await extension.activate();
      results.pylance[id] = extension.packageJSON.version;
      assert.equal(extension.packageJSON.version, id === "ms-python.python" ? "2026.10.0" : "2026.4.1");
    }
    const generated = JSON.parse(await fs.readFile(process.env.GRAPHQL_DESK_ARTIFACTS_PATH, "utf8"));
    assert.ok(generated.initial.length > 1 && generated.nullable.length > 1);
    const workspace = process.env.GRAPHQL_DESK_TEST_WORKSPACE;
    let stubRoot = process.env.GRAPHQL_DESK_STUB_ROOT;
    const extension = vscode.extensions.getExtension("graphql-desk.graphql-desk-editor");
    assert.ok(extension, "GraphQL Desk extension must be loaded in the host");
    await extension.activate();
    const consumerUri = vscode.Uri.file(path.join(process.env.GRAPHQL_DESK_TEST_WORKSPACE, "consumer.py"));
    const consumer = await vscode.workspace.openTextDocument(consumerUri);
    await vscode.window.showTextDocument(consumer, { preview: false });
    const hover = async (line, character) => {
      const items = await vscode.commands.executeCommand("vscode.executeHoverProvider", consumerUri, new vscode.Position(line, character)) || [];
      return items.flatMap(item => item.contents.map(content => typeof content === "string" ? content : content.value)).join("\n");
    };
    const diagnostics = () => vscode.languages.getDiagnostics(consumerUri);
    const observed = { result: "", selected: "", count: "", diagnostics: [] };
    const ready = await until(async () => {
      observed.result = await hover(7, 2);
      observed.selected = await hover(8, 2);
      observed.count = await hover(9, 2);
      observed.diagnostics = diagnostics().filter(item => item.range.start.line === 11).map(item => item.message);
      try {
        results.lifecycle.publishedPaths = await fs.readdir(path.join(stubRoot, "graphql_desk_generated"));
        results.lifecycle.defaultStub = await fs.readFile(path.join(stubRoot, "graphql_desk_generated", "default.pyi"), "utf8");
      } catch { results.lifecycle.publishedPaths = []; }
      results.pylance = { ...results.pylance, ...observed };
      try { await fs.access(path.join(stubRoot, "graphql_desk_generated", "__init__.pyi")); } catch { return false; }
      return /String|str/.test(observed.selected) && observed.diagnostics.length > 0;
    }, "generated result and variable types");
    results.pylance = { ...results.pylance, ...observed };
    assert.ok(ready, JSON.stringify(observed));
    results.pylance.resultType = observed.result;
    results.pylance.selectedType = observed.selected;
    results.pylance.countType = observed.count;
    results.pylance.variableError = observed.diagnostics;
    assert.match(results.pylance.selectedType, /String|str/);
    assert.match(results.pylance.countType, /Int|int/);
    assert.ok(results.pylance.variableError.length);
    results.lifecycle.publishedRealWorkerArtifacts = generated.initial.length;
    const settingsPath = path.join(workspace, ".vscode", "settings.json");
    const settings = JSON.parse(await fs.readFile(settingsPath, "utf8"));
    assert.equal(settings["python.analysis.stubPath"], ".graphql-desk/typings");
    assert.equal(settings["files.autoSave"], "off", "Automatic stub setup must preserve unrelated workspace settings");
    results.lifecycle.settingsPreserved = true;
    const customRoot = path.join(workspace, "custom_stubs");
    await vscode.workspace.getConfiguration("python", consumerUri).update("analysis.stubPath", "custom_stubs", vscode.ConfigurationTarget.WorkspaceFolder);
    const moved = await until(async () => {
      const exists = async target => fs.access(target).then(() => true, () => false);
      return await exists(path.join(customRoot, "graphql_desk_generated", "default.pyi")) &&
        !await exists(path.join(stubRoot, "graphql_desk_generated")) && (await hover(8, 2)).includes("String");
    }, "runtime workspace stubPath change republishes into the new managed root");
    assert.ok(moved);
    stubRoot = customRoot;
    results.lifecycle.runtimeStubPathChange = true;
    const schemaUri = vscode.Uri.file(path.join(workspace, "schema.graphql"));
    const schemaDocument = await vscode.workspace.openTextDocument(schemaUri);
    const schemaText = "type Query { selected: String count(limit: Int!): Int! }\n";
    const schemaEdit = new vscode.WorkspaceEdit();
    schemaEdit.replace(schemaUri, new vscode.Range(schemaDocument.positionAt(0), schemaDocument.positionAt(schemaDocument.getText().length)), schemaText);
    assert.equal(await vscode.workspace.applyEdit(schemaEdit), true);
    assert.equal(await schemaDocument.save(), true);
    results.pylance.nullableRefresh = await until(async () => {
      const stub = await fs.readFile(path.join(stubRoot, "graphql_desk_generated", "_documents", "default", "d0.pyi"), "utf8").catch(() => "");
      return stub.includes("'selected': 'String | None'") && (await hover(8, 2)).includes("None") && diagnostics().some(item => item.range.start.line === 10);
    }, "nullable output and Pylance refresh from GraphQL edit");
    assert.ok(results.pylance.nullableRefresh);
    results.lifecycle.nullableArtifactsPublished = true;
    const externalRoot = path.join(path.dirname(workspace), "external-stubs");
    await fs.mkdir(externalRoot, { recursive: true });
    await fs.writeFile(path.join(externalRoot, "keep.txt"), "preserve foreign root");
    const externalSetting = path.relative(workspace, externalRoot).split(path.sep).join("/");
    await vscode.workspace.getConfiguration("python", consumerUri).update("analysis.stubPath", externalSetting, vscode.ConfigurationTarget.WorkspaceFolder);
    const retired = await until(async () => {
      try { await fs.access(path.join(customRoot, "graphql_desk_generated")); return false; } catch {}
      try { await fs.access(path.join(externalRoot, "graphql_desk_generated")); return false; } catch {}
      return true;
    }, "external stubPath conflict retires old owned artifacts without publishing into the foreign root");
    assert.ok(retired);
    assert.equal(await fs.readFile(path.join(externalRoot, "keep.txt"), "utf8"), "preserve foreign root");
    const retryEdit = new vscode.WorkspaceEdit();
    const latestSchema = await vscode.workspace.openTextDocument(schemaUri);
    retryEdit.insert(schemaUri, latestSchema.positionAt(latestSchema.getText().length), "\n");
    assert.equal(await vscode.workspace.applyEdit(retryEdit), true);
    assert.equal(await latestSchema.save(), true);
    await new Promise(resolve => setTimeout(resolve, 800));
    await assert.rejects(fs.access(path.join(customRoot, "graphql_desk_generated")), { code: "ENOENT" }, "A later document refresh must not fall back to the retired managed root");
    await assert.rejects(fs.access(path.join(externalRoot, "graphql_desk_generated")), { code: "ENOENT" }, "A later document refresh must not publish into an external root");
    results.lifecycle.externalRootConflictPreserved = true;
    results.lifecycle.configuredStubPath = externalSetting;
    results.lifecycle.activeStubRoot = stubRoot;
    results.lifecycle.ownedArtifactsRemoved = "verified after actual extension-host shutdown by the parent harness";
    results.lifecycle.userConfigurationPreserved = true;
    results.lifecycle.userFilesUntouched = await fs.readFile(path.join(workspace, "schema.graphql"), "utf8").then(text => text === "type Query { selected: String count(limit: Int!): Int! }\n\n");
    assert.ok(results.lifecycle.userFilesUntouched, "The extension must leave the user's GraphQL edit intact");
    results.outcome = "generated-factory-artifacts-observed";
  } catch (error) {
    results.error = String(error.stack || error);
    results.outcome = "generated-factory-artifacts-failed";
    throw error;
  } finally {
    require("node:fs").writeFileSync(process.env.GRAPHQL_DESK_RESULT_PATH, JSON.stringify(results, null, 2));
    console.log(`GRAPHQL_DESK_FACTORY_ARTIFACTS ${JSON.stringify(results)}`);
  }
};
