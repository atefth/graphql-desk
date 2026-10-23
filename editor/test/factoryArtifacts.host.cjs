"use strict";
const assert = require("node:assert/strict");
const path = require("node:path");
const vscode = require("vscode");
const fs = require("node:fs/promises");
const { createOwnedAnalysisArtifacts } = require("../src/ownedAnalysisArtifacts");

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
    const consumerOnDisk = await fs.readFile(consumerUri.fsPath, "utf8");
    const originalSource = consumer.getText();
    const generatedDocumentPath = path.join(stubRoot, "graphql_desk_generated", "_documents", "default", "d0.pyi");
    const documentStubBefore = await fs.readFile(generatedDocumentPath, "utf8");
    results.lifecycle.unsavedEditBefore = {
      bufferVersion: consumer.version,
      dirty: consumer.isDirty,
      pythonBuffer: originalSource,
      pythonFile: consumerOnDisk,
      embeddedOperation: originalSource.match(/graphql\('([^']+)'\)/)?.[1] ?? null,
      documentStubBytes: documentStubBefore,
      pylanceHover: observed.selected,
      stubPath: vscode.workspace.getConfiguration("python", consumerUri).get("analysis.stubPath"),
    };
    const changedSource = originalSource.replace("selected count(limit:", "count(limit:").replace("result['selected']", "result['count']");
    assert.notEqual(changedSource, originalSource);
    const sourceEdit = new vscode.WorkspaceEdit();
    sourceEdit.replace(consumer.uri, new vscode.Range(consumer.positionAt(0), consumer.positionAt(consumer.getText().length)), changedSource);
    assert.equal(await vscode.workspace.applyEdit(sourceEdit), true);
    results.lifecycle.unsavedArtifactRefresh = await until(async () => {
      const stub = await fs.readFile(generatedDocumentPath, "utf8").catch(() => "");
      return /'count': 'Int'/.test(stub) && !/'selected': 'String'/.test(stub);
    }, "unsaved embedded GraphQL edit republishes the changed result shape");
    results.pylance.unsavedOperationRefresh = results.lifecycle.unsavedArtifactRefresh && await until(async () => /Int/.test(await hover(8, 2)) && !/String/.test(await hover(8, 2)), "Pylance hover updates after the unsaved operation edit");
    if (!results.pylance.unsavedOperationRefresh) results.lifecycle.unsavedEditProbe = {
      buffer: consumer.getText(),
      disk: await fs.readFile(consumerUri.fsPath, "utf8"),
      hover: await hover(8, 2),
      documentStub: await fs.readFile(generatedDocumentPath, "utf8").catch(() => null),
    };
    assert.ok(results.pylance.unsavedOperationRefresh);
    results.lifecycle.unsavedPythonBufferPreserved = consumer.isDirty && await fs.readFile(consumerUri.fsPath, "utf8").then(text => text === consumerOnDisk);
    assert.ok(results.lifecycle.unsavedPythonBufferPreserved, "The GraphQL edit must remain unsaved and leave the Python file unchanged on disk");
    const documentStubAfter = await fs.readFile(generatedDocumentPath, "utf8");
    results.lifecycle.unsavedEditAfter = {
      bufferVersion: consumer.version,
      dirty: consumer.isDirty,
      pythonBuffer: consumer.getText(),
      pythonFile: await fs.readFile(consumerUri.fsPath, "utf8"),
      embeddedOperation: consumer.getText().match(/graphql\('([^']+)'\)/)?.[1] ?? null,
      documentStubBytes: documentStubAfter,
      pylanceHover: await hover(8, 2),
      stubPath: vscode.workspace.getConfiguration("python", consumerUri).get("analysis.stubPath"),
    };
    assert.notEqual(results.lifecycle.unsavedEditAfter.embeddedOperation, results.lifecycle.unsavedEditBefore.embeddedOperation);
    assert.notEqual(documentStubAfter, documentStubBefore, "The owned operation stub must change for the unsaved GraphQL edit");
    const restoreSource = new vscode.WorkspaceEdit();
    restoreSource.replace(consumer.uri, new vscode.Range(consumer.positionAt(0), consumer.positionAt(consumer.getText().length)), originalSource);
    assert.equal(await vscode.workspace.applyEdit(restoreSource), true);
    assert.ok(await until(async () => /String/.test(await hover(8, 2)) && !/None/.test(await hover(8, 2)), "restored operation republishes its original type"));
    const lifecycleUri = vscode.Uri.file(path.join(workspace, "lifecycle_source.py"));
    const renamedUri = vscode.Uri.file(path.join(workspace, "lifecycle_renamed.py"));
    const lifecycleSource = "from graphql_desk_generated.default import graphql\ndoc = graphql('query Lifecycle { selected }')\n";
    await fs.writeFile(lifecycleUri.fsPath, lifecycleSource);
    const sourceAlias = name => path.join(stubRoot, "graphql_desk_generated", "sources", `${name}.pyi`);
    const exists = file => fs.access(file).then(() => true, () => false);
    const factoryText = () => fs.readFile(path.join(stubRoot, "graphql_desk_generated", "default.pyi"), "utf8").catch(() => "");
    assert.ok(await until(async () => await exists(sourceAlias("lifecycle_source")) && (await factoryText()).includes("query Lifecycle { selected }"), "new closed source publishes its companion"));
    const lifecycleDocument = await vscode.workspace.openTextDocument(lifecycleUri);
    await vscode.window.showTextDocument(lifecycleDocument, { preview: false });
    const lifecycleEdit = new vscode.WorkspaceEdit();
    lifecycleEdit.replace(lifecycleUri, new vscode.Range(lifecycleDocument.positionAt(0), lifecycleDocument.positionAt(lifecycleSource.length)), lifecycleSource.replace("{ selected }", "{ count(limit: 1) }"));
    assert.equal(await vscode.workspace.applyEdit(lifecycleEdit), true);
    assert.ok(await until(async () => (await factoryText()).includes("query Lifecycle { count(limit: 1) }"), "unsaved companion operation update"));
    await vscode.commands.executeCommand("undo");
    assert.ok(await until(async () => (await factoryText()).includes("query Lifecycle { selected }"), "undo restores the captured operation"));
    await vscode.commands.executeCommand("redo");
    assert.ok(await until(async () => (await factoryText()).includes("query Lifecycle { count(limit: 1) }"), "redo restores the new captured operation"));
    await vscode.commands.executeCommand("undo");
    assert.equal(lifecycleDocument.getText(), lifecycleSource);
    assert.equal(lifecycleDocument.isDirty, false);
    assert.equal(await fs.readFile(lifecycleUri.fsPath, "utf8"), lifecycleSource);
    await vscode.commands.executeCommand("workbench.action.closeActiveEditor");
    await vscode.window.showTextDocument(consumer, { preview: false });
    assert.ok(await until(async () => (await factoryText()).includes("query Lifecycle { selected }"), "source close keeps the saved operation"));
    await fs.writeFile(lifecycleUri.fsPath, lifecycleSource.replace("{ selected }", "{ count(limit: 1) }"));
    assert.ok(await until(async () => (await factoryText()).includes("query Lifecycle { count(limit: 1) }"), "closed source disk edit refreshes its operation"));
    await fs.rename(lifecycleUri.fsPath, renamedUri.fsPath);
    const renamed = await until(async () => await exists(sourceAlias("lifecycle_renamed")) && !await exists(sourceAlias("lifecycle_source")), "rename replaces only the owned obsolete companion");
    if (!renamed) results.lifecycle.renameProbe = {
      documents: vscode.workspace.textDocuments.map(document => ({ uri: document.uri.toString(), dirty: document.isDirty, closed: document.isClosed })),
      aliases: await fs.readdir(path.dirname(sourceAlias("lifecycle_source"))),
      factory: await factoryText(),
    };
    assert.ok(renamed);
    assert.ok(await until(async () => /String/.test(await hover(8, 2)), "unaffected consumer converges after source rename"));
    await fs.unlink(renamedUri.fsPath);
    assert.ok(await until(async () => !await exists(sourceAlias("lifecycle_renamed")) && !(await factoryText()).includes("query Lifecycle"), "deletion removes the owned companion and overload"));
    assert.ok(await until(async () => /String/.test(await hover(8, 2)), "unaffected consumer converges after source deletion"));
    results.lifecycle.sourceUndoRedoCloseRenameDelete = true;
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
    if (!results.pylance.nullableRefresh) results.lifecycle.nullableProbe = {
      stub: await fs.readFile(path.join(stubRoot, "graphql_desk_generated", "_documents", "default", "d0.pyi"), "utf8").catch(() => null),
      hover: await hover(8, 2),
      diagnostics: diagnostics().map(item => ({ line: item.range.start.line, message: item.message })),
      stubPath: vscode.workspace.getConfiguration("python", consumerUri).get("analysis.stubPath"),
    };
    assert.ok(results.pylance.nullableRefresh);
    results.lifecycle.nullableArtifactsPublished = true;
    await vscode.commands.executeCommand("python.analysis.restartLanguageServer");
    results.pylance.checkerRestart = await until(async () =>
      (await hover(8, 2)).includes("None") && diagnostics().some(item => item.range.start.line === 10),
    "Pylance retains current managed types after an explicit analysis-server restart");
    assert.ok(results.pylance.checkerRestart);
    assert.equal(consumer.getText(), await fs.readFile(consumerUri.fsPath, "utf8"), "The saved consumer must remain unchanged before close/reopen");
    assert.equal(await consumer.save(), true);
    assert.equal(consumer.isDirty, false, "Close/reopen coverage must not discard an unsaved consumer edit");
    let consumerClosed = false;
    const closedListener = vscode.workspace.onDidCloseTextDocument(document => {
      if (document.uri.toString() === consumerUri.toString()) consumerClosed = true;
    });
    await vscode.window.showTextDocument(consumer, { preview: false });
    await vscode.commands.executeCommand("workbench.action.closeActiveEditor");
    assert.ok(await until(async () => !vscode.window.visibleTextEditors.some(editor => editor.document.uri.toString() === consumerUri.toString()), "saved consumer editor closes"));
    results.lifecycle.documentCloseEventObserved = consumerClosed;
    closedListener.dispose();
    const reopenedConsumer = vscode.workspace.textDocuments.find(document => document.uri.toString() === consumerUri.toString()) ?? await vscode.workspace.openTextDocument(consumerUri);
    await vscode.window.showTextDocument(reopenedConsumer, { preview: false });
    assert.ok(await until(async () => (await hover(8, 2)).includes("None") && diagnostics().some(item => item.range.start.line === 10), "saved consumer reopens with current Pylance types"));
    assert.equal(reopenedConsumer.isDirty, false);
    results.lifecycle.checkerRestartAndSavedConsumerReopen = true;
    const dirtyFiles = [{ relative_path: "dirty_fixture/default.pyi", text: "value: str\n" }];
    const dirtyHandle = createOwnedAnalysisArtifacts(vscode, workspace, stubRoot, "dirty_fixture", dirtyFiles);
    const dirtyUri = vscode.Uri.file(path.join(stubRoot, "dirty_fixture", "default.pyi"));
    const dirtyDocument = await vscode.workspace.openTextDocument(dirtyUri);
    const dirtyEditor = await vscode.window.showTextDocument(dirtyDocument, { preview: false });
    const dirtyDisk = await fs.readFile(dirtyUri.fsPath);
    assert.equal(await dirtyEditor.edit(edit => edit.insert(new vscode.Position(0, 0), "# user edit\n")), true);
    const dirtyState = { text: dirtyDocument.getText(), version: dirtyDocument.version };
    await assert.rejects(dirtyHandle.publish(dirtyFiles, () => true), /user edits/);
    await assert.rejects(dirtyHandle.cleanup(), /user edits/);
    assert.equal(dirtyDocument.getText(), dirtyState.text);
    assert.equal(dirtyDocument.version, dirtyState.version);
    assert.equal(dirtyDocument.isDirty, true);
    assert.deepEqual(await fs.readFile(dirtyUri.fsPath), dirtyDisk);
    results.lifecycle.foreignDirtyGeneratedBufferPreserved = true;
    await vscode.window.showTextDocument(consumer, { preview: false });
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
