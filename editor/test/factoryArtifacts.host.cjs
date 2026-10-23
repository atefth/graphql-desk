"use strict";
const assert = require("node:assert/strict");
const path = require("node:path");
const vscode = require("vscode");
const fs = require("node:fs/promises");
const artifactModule = require("../src/ownedAnalysisArtifacts");
const { createOwnedAnalysisArtifacts } = artifactModule;

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
  const started = Date.now();
  const results = { host: vscode.version, pylance: {}, generatedFiles: true, lifecycle: { coldTrace: [], skipTraces: [] } };
  const mark = (stage, details = {}) => results.lifecycle.coldTrace.push({ stage, elapsedMs: Date.now() - started, ...details });
  const workerCalls = new Map();
  const createOutputChannel = vscode.window.createOutputChannel.bind(vscode.window);
  vscode.window.createOutputChannel = (name, ...args) => {
    const channel = createOutputChannel(name, ...args);
    if (name !== "GraphQL Desk") return channel;
    return new Proxy(channel, { get(target, property) {
      if (property === "appendLine") return value => {
        results.lifecycle.skipTraces.push(value);
        return target.appendLine(value);
      };
      const value = Reflect.get(target, property, target);
      return typeof value === "function" ? value.bind(target) : value;
    } });
  };
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
    const workerModule = require("../src/analysisWorker");
    const createWorker = workerModule.createAnalysisWorker;
    workerModule.createAnalysisWorker = options => {
      const worker = createWorker(options);
      return new Proxy(worker, { get(target, property) {
        if (property === "request") return async (message, requestOptions) => {
          const count = (workerCalls.get(message.op) ?? 0) + 1;
          workerCalls.set(message.op, count);
          if (count === 1) mark(`worker.${message.op}.started`, { candidates: message.candidates?.length, buffers: message.buffers?.length });
          try {
            const reply = await target.request(message, requestOptions);
            if (count === 1) mark(`worker.${message.op}.returned`, { ok: reply.ok, projects: reply.projects?.length,
              files: reply.projects?.reduce((total, project) => total + (project.files?.length ?? 0), 0),
              unavailableSchemas: reply.unavailable_schemas?.length });
            return reply;
          } catch (error) { if (count === 1) mark(`worker.${message.op}.failed`, { error: String(error) }); throw error; }
        };
        const value = Reflect.get(target, property, target);
        return typeof value === "function" ? value.bind(target) : value;
      } });
    };
    const createArtifacts = artifactModule.createOwnedAnalysisArtifacts;
    artifactModule.createOwnedAnalysisArtifacts = (...args) => {
      mark("publisher.create.started", { files: args[4]?.length });
      try {
        const handle = createArtifacts(...args);
        mark("publisher.create.returned", { files: args[4]?.length });
        return handle;
      } catch (error) { mark("publisher.create.failed", { error: String(error) }); throw error; }
    };
    const extension = vscode.extensions.getExtension("graphql-desk.graphql-desk-editor");
    assert.ok(extension, "GraphQL Desk extension must be loaded in the host");
    const consumerUri = vscode.Uri.file(path.join(process.env.GRAPHQL_DESK_TEST_WORKSPACE, "consumer.py"));
    const consumer = await vscode.workspace.openTextDocument(consumerUri);
    await vscode.window.showTextDocument(consumer, { preview: false });
    const selectedStubRoot = () => path.resolve(workspace, vscode.workspace.getConfiguration("python", consumerUri).get("analysis.stubPath", ".graphql-desk/typings"));
    stubRoot = selectedStubRoot();
    const documentStubPath = () => path.join(selectedStubRoot(), "graphql_desk_generated", "_documents", "default", "d0.pyi");
    await extension.activate();
    mark("extension.activation.returned", { active: extension.isActive });
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
        stubRoot = selectedStubRoot();
        results.lifecycle.publishedPaths = await fs.readdir(path.join(stubRoot, "graphql_desk_generated"));
        results.lifecycle.defaultStub = await fs.readFile(path.join(stubRoot, "graphql_desk_generated", "default.pyi"), "utf8");
      } catch { results.lifecycle.publishedPaths = []; }
      results.pylance = { ...results.pylance, ...observed };
      if (/String|str/.test(observed.selected) && !results.lifecycle.coldTrace.some(item => item.stage === "pylance.hover.indexed")) {
        mark("pylance.hover.indexed", { hover: observed.selected });
      }
      if (observed.diagnostics.length && !results.lifecycle.coldTrace.some(item => item.stage === "pylance.diagnostic.indexed")) {
        mark("pylance.diagnostic.indexed", { count: observed.diagnostics.length });
      }
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
    const documentStubBefore = await fs.readFile(documentStubPath(), "utf8");
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
      const stub = await fs.readFile(documentStubPath(), "utf8").catch(() => "");
      return /'count': 'Int'/.test(stub) && !/'selected': 'String'/.test(stub);
    }, "unsaved embedded GraphQL edit republishes the changed result shape");
    results.pylance.unsavedOperationRefresh = results.lifecycle.unsavedArtifactRefresh && await until(async () => /Int/.test(await hover(8, 2)) && !/String/.test(await hover(8, 2)), "Pylance hover updates after the unsaved operation edit");
    if (!results.pylance.unsavedOperationRefresh) results.lifecycle.unsavedEditProbe = {
      buffer: consumer.getText(),
      disk: await fs.readFile(consumerUri.fsPath, "utf8"),
      hover: await hover(8, 2),
      documentStub: await fs.readFile(documentStubPath(), "utf8").catch(() => null),
    };
    assert.ok(results.pylance.unsavedOperationRefresh);
    results.lifecycle.unsavedPythonBufferPreserved = consumer.isDirty && await fs.readFile(consumerUri.fsPath, "utf8").then(text => text === consumerOnDisk);
    assert.ok(results.lifecycle.unsavedPythonBufferPreserved, "The GraphQL edit must remain unsaved and leave the Python file unchanged on disk");
    stubRoot = selectedStubRoot();
    const documentStubAfter = await fs.readFile(documentStubPath(), "utf8");
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
    const fragmentUri = vscode.Uri.file(path.join(workspace, "fragments.py"));
    const fragmentSource = "from graphql_desk_generated.default import graphql\ncard = graphql('fragment Card on User { name }')\n";
    const fragmentFinalSource = fragmentSource.replace("{ name }", "{ name id }");
    const fragmentDocument = await vscode.workspace.openTextDocument(fragmentUri);
    const fragmentEdit = new vscode.WorkspaceEdit();
    fragmentEdit.replace(fragmentUri, new vscode.Range(fragmentDocument.positionAt(0), fragmentDocument.positionAt(fragmentDocument.getText().length)), fragmentFinalSource);
    const fragmentConsumerSource = [
      "from graphql_desk import TypedDocument",
      "from graphql_desk_generated.default import graphql",
      "from graphql_desk_generated.sources.fragments import read_card",
      "from fragments import card",
      "from typing import TypeVar",
      "R = TypeVar('R')",
      "V = TypeVar('V')",
      "def execute(document: TypedDocument[R, V], variables: V) -> R: ...",
      "doc = graphql('query WithCard { user { ...Card } }', (card,))",
      "result = execute(doc, {})",
      "user_id = read_card(result['user'])['id']",
      "needs_str: str = user_id",
      "wrong: int = user_id",
      "",
    ].join("\n");
    const fragmentConsumerEdit = new vscode.WorkspaceEdit();
    fragmentConsumerEdit.replace(consumer.uri, new vscode.Range(consumer.positionAt(0), consumer.positionAt(consumer.getText().length)), fragmentConsumerSource);
    const restartCalls = [];
    const executeCommand = vscode.commands.executeCommand.bind(vscode.commands);
    vscode.commands.executeCommand = async (command, ...args) => {
      if (command === "python.analysis.restartLanguageServer") restartCalls.push({ time: Date.now() });
      return executeCommand(command, ...args);
    };
    try {
      assert.equal(await vscode.workspace.applyEdit(fragmentEdit), true);
      assert.equal(await vscode.workspace.applyEdit(fragmentConsumerEdit), true);
      await vscode.window.showTextDocument(consumer, { preview: false });
      const valueOffset = fragmentConsumerSource.indexOf("user_id = read_card") + 1;
      const valuePosition = consumer.positionAt(valueOffset);
      const fragmentHover = async () => {
        const items = await vscode.commands.executeCommand("vscode.executeHoverProvider", consumerUri, valuePosition) || [];
        return items.flatMap(item => item.contents.map(content => typeof content === "string" ? content : content.value)).join("\n");
      };
      const fragmentDiagnostics = () => diagnostics().map(item => ({
        code: item.code?.value ?? item.code ?? "", source: item.source, severity: item.severity, message: item.message,
      }));
      results.lifecycle.crossFileFragmentRefresh = await until(async () => {
        const fragmentStubs = path.join(selectedStubRoot(), "graphql_desk_generated");
        const defaultStub = await fs.readFile(path.join(fragmentStubs, "default.pyi"), "utf8").catch(() => "");
        const fragmentStub = await fs.readFile(path.join(fragmentStubs, "sources", "fragments.pyi"), "utf8").catch(() => "");
        const hoverText = await fragmentHover();
        const items = fragmentDiagnostics();
        results.lifecycle.crossFileFragmentProbe = {
          hover: hoverText,
          diagnostics: items.map(item => ({ code: item.code, severity: item.severity, message: item.message })),
          restartCount: restartCalls.length,
          generatedOperation: defaultStub.includes("query WithCard { user { ...Card } }"),
          generatedFragmentReader: fragmentStub.includes("GraphqlDeskReadFragment as read_card"),
        };
        return results.lifecycle.crossFileFragmentProbe.generatedOperation &&
          results.lifecycle.crossFileFragmentProbe.generatedFragmentReader &&
          /\bID\b/.test(hoverText) &&
          !items.some(item => /reportMissingImports|reportMissingModuleSource|could not be resolved|No overload/i.test(`${JSON.stringify(item.code)} ${item.message}`)) &&
          items.some(item => /reportAssignmentType|Incompatible/i.test(`${item.code} ${item.message}`));
      }, "cross-file fragment mutation converges in generated stubs, Pylance hover, and diagnostics", 45000);
      assert.ok(results.lifecycle.crossFileFragmentRefresh, JSON.stringify(results.lifecycle.crossFileFragmentProbe));
      await new Promise(resolve => setTimeout(resolve, 2000));
      results.lifecycle.fragmentLanguageServerRestarts = restartCalls.length;
      const settledRestartCount = restartCalls.length;
      assert.equal(settledRestartCount, 0, "Owned immutable stubPath rotation must refresh Pylance without an explicit restart command");
      await new Promise(resolve => setTimeout(resolve, 2000));
      assert.equal(restartCalls.length, settledRestartCount,
        "Pylance refreshes must settle after the generated fragment snapshot converges");
      const generationRoot = path.join(workspace, ".graphql-desk", "typings", "generations");
      const completeGenerationCount = async () => (await fs.readdir(generationRoot)).filter(name => /^g-\d+-/.test(name)).length;
      const countAfterConvergence = await completeGenerationCount();
      await new Promise(resolve => setTimeout(resolve, 2000));
      assert.equal(await completeGenerationCount(), countAfterConvergence,
        "A completed owned generation must not trigger repeated settings-driven analysis and publication");
      results.lifecycle.generationsStableAfterConvergence = countAfterConvergence;
      results.lifecycle.crossFileFragmentMutation = true;
    } finally {
      vscode.commands.executeCommand = executeCommand;
      const restoreFragment = new vscode.WorkspaceEdit();
      restoreFragment.replace(fragmentUri, new vscode.Range(fragmentDocument.positionAt(0), fragmentDocument.positionAt(fragmentDocument.getText().length)), fragmentSource);
      await vscode.workspace.applyEdit(restoreFragment);
      const restoreFragmentConsumer = new vscode.WorkspaceEdit();
      restoreFragmentConsumer.replace(consumer.uri, new vscode.Range(consumer.positionAt(0), consumer.positionAt(consumer.getText().length)), originalSource);
      await vscode.workspace.applyEdit(restoreFragmentConsumer);
      await vscode.window.showTextDocument(consumer, { preview: false });
      assert.ok(await until(async () => /String/.test(await hover(8, 2)) && !/None/.test(await hover(8, 2)), "fragment fixture cleanup restores original operation typing"));
    }
    const lifecycleUri = vscode.Uri.file(path.join(workspace, "lifecycle_source.py"));
    const renamedUri = vscode.Uri.file(path.join(workspace, "lifecycle_renamed.py"));
    const lifecycleSource = "from graphql_desk_generated.default import graphql\ndoc = graphql('query Lifecycle { selected }')\n";
    await fs.writeFile(lifecycleUri.fsPath, lifecycleSource);
    const sourceAlias = name => path.join(selectedStubRoot(), "graphql_desk_generated", "sources", `${name}.pyi`);
    const exists = file => fs.access(file).then(() => true, () => false);
    const factoryText = () => fs.readFile(path.join(selectedStubRoot(), "graphql_desk_generated", "default.pyi"), "utf8").catch(() => "");
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
    const savedLifecycleSource = lifecycleSource.replace("{ selected }", "{ count(limit: 1) }");
    const savedLifecycleEdit = new vscode.WorkspaceEdit();
    savedLifecycleEdit.replace(lifecycleUri, new vscode.Range(lifecycleDocument.positionAt(0), lifecycleDocument.positionAt(lifecycleDocument.getText().length)), savedLifecycleSource);
    assert.equal(await vscode.workspace.applyEdit(savedLifecycleEdit), true);
    assert.equal(await lifecycleDocument.save(), true);
    assert.ok(await until(async () => await fs.readFile(lifecycleUri.fsPath, "utf8") === savedLifecycleSource &&
      (await factoryText()).includes("query Lifecycle { count(limit: 1) }"), "saving an operation publishes its persisted companion"));
    await vscode.commands.executeCommand("workbench.action.closeActiveEditor");
    await vscode.window.showTextDocument(consumer, { preview: false });
    assert.ok(await until(async () => (await factoryText()).includes("query Lifecycle { count(limit: 1) }"), "source close keeps the saved operation"));
    const reopenedLifecycle = await vscode.workspace.openTextDocument(lifecycleUri);
    assert.equal(reopenedLifecycle.getText(), savedLifecycleSource);
    assert.ok((await factoryText()).includes("query Lifecycle { count(limit: 1) }"), "reopening the saved source keeps its generated operation current");
    results.lifecycle.savedOperationSurvivesCloseReopen = true;
    const restoreLifecycle = new vscode.WorkspaceEdit();
    restoreLifecycle.replace(lifecycleUri, new vscode.Range(reopenedLifecycle.positionAt(0), reopenedLifecycle.positionAt(reopenedLifecycle.getText().length)), lifecycleSource);
    assert.equal(await vscode.workspace.applyEdit(restoreLifecycle), true);
    assert.equal(await reopenedLifecycle.save(), true);
    assert.ok(await until(async () => (await factoryText()).includes("query Lifecycle { selected }"), "saving the restored source republishes its companion"));
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
    assert.match(settings["python.analysis.stubPath"], /^\.graphql-desk\/typings\/generations\/g-[0-9]+-/);
    assert.equal(settings["files.autoSave"], "off", "Automatic stub setup must preserve unrelated workspace settings");
    results.lifecycle.settingsPreserved = true;
    const customRoot = path.join(workspace, "custom_stubs");
    await vscode.workspace.getConfiguration("python", consumerUri).update("analysis.stubPath", "custom_stubs", vscode.ConfigurationTarget.WorkspaceFolder);
    const previouslySelected = selectedStubRoot();
    const moved = await until(async () => {
      const exists = async target => fs.access(target).then(() => true, () => false);
      return await exists(path.join(customRoot, "graphql_desk_generated", "default.pyi")) &&
        selectedStubRoot() === customRoot && await exists(path.join(previouslySelected, "graphql_desk_generated", "default.pyi")) && (await hover(8, 2)).includes("String");
    }, "runtime workspace stubPath change republishes into the new managed root");
    assert.ok(moved);
    stubRoot = customRoot;
    results.lifecycle.runtimeStubPathChange = true;
    const schemaUri = vscode.Uri.file(path.join(workspace, "schema.graphql"));
    const schemaDocument = await vscode.workspace.openTextDocument(schemaUri);
    const schemaText = "type User { id: ID! name: String! }\ntype Query { selected: String count(limit: Int!): Int! user: User! }\n";
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
    await new Promise(resolve => setTimeout(resolve, 250));
    results.lifecycle.documentCloseEventObserved = consumerClosed;
    closedListener.dispose();
    const reopenedConsumer = await vscode.workspace.openTextDocument(consumerUri);
    await vscode.window.showTextDocument(reopenedConsumer, { preview: false });
    assert.ok(await until(async () => (await hover(8, 2)).includes("None") && diagnostics().some(item => item.range.start.line === 10), "saved consumer reopens with current Pylance types"));
    assert.equal(reopenedConsumer.isDirty, false);
    results.lifecycle.savedConsumerEditorReopen = true;
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
    results.lifecycle.userFilesUntouched = await fs.readFile(path.join(workspace, "schema.graphql"), "utf8").then(text => text === "type User { id: ID! name: String! }\ntype Query { selected: String count(limit: Int!): Int! user: User! }\n\n");
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
