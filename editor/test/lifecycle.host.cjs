"use strict";

const assert = require("node:assert/strict");
const { createHash } = require("node:crypto");
const fs = require("node:fs/promises");
const path = require("node:path");
const vscode = require("vscode");
const workerModule = require("../src/analysisWorker");
const artifactModule = require("../src/ownedAnalysisArtifacts");
const workerRequests = [];
const publications = [];
const freshnessChecks = [];
const refreshCallbacks = [];
const artifactCalls = [];
const skipReasons = [];
let crashNextArtifacts = false;
let delayedArtifact = null;
const createWorker = workerModule.createAnalysisWorker;
workerModule.createAnalysisWorker = options => {
  const worker = createWorker(options);
  return new Proxy(worker, { get(target, property) {
    if (property === "request") return async (message, requestOptions) => {
      const record = { op: message.op, id: message.id, buffers: message.buffers?.map(item => ({
        path: item.path, dirty: item.is_dirty, hasSelected: item.text?.includes("{ selected count(limit: $limit) }"),
        hasChangedQuery: item.text?.includes("{ user { id } count(limit: $limit) }")
      })), schemas: message.schema_buffers?.map(item => ({ path: item.path, hasBoolean: item.text?.includes("selected: Boolean!") })) };
      try {
        if (message.op === "artifacts" && crashNextArtifacts) {
          crashNextArtifacts = false;
          const pending = target.request(message, requestOptions);
          target.invalidate();
          await pending;
        }
        const reply = await target.request(message, requestOptions);
        record.ok = reply.ok;
        record.current = reply.current;
        workerRequests.push(record);
        return reply;
      } catch (error) { record.error = String(error); workerRequests.push(record); throw error; }
    };
    const value = Reflect.get(target, property, target);
    return typeof value === "function" ? value.bind(target) : value;
  } });
};
const controllerModule = require("../src/analysisController");
const createController = controllerModule.createAnalysisController;
controllerModule.createAnalysisController = (...args) => {
  const [vscodeApi, options] = args;
  const originalChanged = options.changed;
  const controller = createController(vscodeApi, { ...options, changed: () => {
    refreshCallbacks.push({ visible: vscodeApi.window.visibleTextEditors.map(editor => editor.document.uri.fsPath),
      documents: vscodeApi.workspace.textDocuments.map(document => ({ path: document.uri.fsPath, language: document.languageId, dirty: document.isDirty })) });
    originalChanged();
  } });
  const artifacts = controller.artifacts;
  controller.artifacts = async (...artifactArgs) => {
    const onSkip = artifactArgs[2];
    artifactArgs[2] = reason => { skipReasons.push(reason); onSkip?.(reason); };
    const result = await artifacts(...artifactArgs);
    artifactCalls.push({ result: Boolean(result), id: result?.id, cancelled: artifactArgs[1]?.isCancellationRequested });
    if (result) {
      if (delayedArtifact) {
        const delay = delayedArtifact;
        delayedArtifact = null;
        delay.started({ id: result.id });
        await delay.release;
      }
      const isCurrent = result.isCurrent;
      result.isCurrent = () => {
        const current = isCurrent();
        freshnessChecks.push({ id: result.id, current });
        return current;
      };
    }
    return result;
  };
  return controller;
};
const createArtifacts = artifactModule.createOwnedAnalysisArtifacts;
artifactModule.createOwnedAnalysisArtifacts = (...args) => {
  const handle = createArtifacts(...args);
  const publish = handle.publish;
  handle.publish = async (...publishArgs) => {
    const changed = await publish(...publishArgs);
    publications.push({ count: publishArgs[0].length, paths: publishArgs[0].map(file => file.relative_path), changed });
    return changed;
  };
  return handle;
};

async function until(check, label, timeout = 45000) {
  const deadline = Date.now() + timeout;
  while (Date.now() < deadline) {
    if (await check()) return;
    await new Promise(resolve => setTimeout(resolve, 100));
  }
  assert.fail(label);
}

exports.run = async () => {
  const workspace = process.env.GRAPHQL_DESK_TEST_WORKSPACE;
  const results = { host: vscode.version, lifecycle: {} };
  let releaseDelayedArtifact = null;
  let delayedArtifactTimer = null;
  const sourcePath = path.join(workspace, "consumer.py");
  const sourceUri = vscode.Uri.file(sourcePath);
  const initialManagedStubRoot = path.resolve(workspace, ".graphql-desk/typings");
  const initialStubRoots = new Set([
    path.resolve(workspace, "typings"),
    initialManagedStubRoot,
  ]);
  const source = await fs.readFile(sourcePath, "utf8");
  let sourceChanges = 0;
  const outputMessages = [];
  const documentEvents = [];
  for (const [eventName, subscribe] of [
    ["open", handler => vscode.workspace.onDidOpenTextDocument(handler)],
    ["close", handler => vscode.workspace.onDidCloseTextDocument(handler)],
    ["change", handler => vscode.workspace.onDidChangeTextDocument(handler)],
  ]) subscribe(document => {
    const item = document.document ?? document;
    if (item.uri?.scheme === "file") documentEvents.push({ eventName, path: item.uri.fsPath,
      language: item.languageId, dirty: item.isDirty, source: item.uri.toString() === sourceUri.toString() });
  });
  const createOutputChannel = vscode.window.createOutputChannel.bind(vscode.window);
  vscode.window.createOutputChannel = (name, ...args) => {
    const channel = createOutputChannel(name, ...args);
    if (name !== "GraphQL Desk") return channel;
    return new Proxy(channel, { get(target, property) {
      if (property === "appendLine") return value => { outputMessages.push(value); return target.appendLine(value); };
      const value = Reflect.get(target, property, target);
      return typeof value === "function" ? value.bind(target) : value;
    } });
  };
  const changedSubscription = vscode.workspace.onDidChangeTextDocument(event => {
    if (event.document.uri.toString() === sourceUri.toString()) sourceChanges += 1;
  });
  const oldQuery = "query Pick($limit: Int!) { selected count(limit: $limit) }";
  const newQuery = "query Pick($limit: Int!) { user { id } count(limit: $limit) }";
  const stubPath = () => vscode.workspace.getConfiguration("python", sourceUri)
    .get("analysis.stubPath", ".graphql-desk/typings");
  let generationObserved = false;
  const readGeneration = async (selectedOverride = null) => {
    for (let attempt = 0; attempt < 5; attempt += 1) {
      const selected = selectedOverride ?? stubPath();
      const root = path.resolve(workspace, selected);
      const found = [];
      let stale = false;
      let rootMissing = false;
      let manifestMissing = false;
      async function visit(directory) {
        let entries;
        try {
          entries = await fs.readdir(directory, { withFileTypes: true });
        } catch (error) {
          if (error.code !== "ENOENT") throw error;
          if (directory === root) rootMissing = true;
          else stale = true;
          return;
        }
        for (const entry of entries) {
          const file = path.join(directory, entry.name);
          if (entry.isDirectory()) await visit(file);
          else if (entry.name.endsWith(".pyi")) {
            try {
              found.push([file, await fs.readFile(file, "utf8")]);
            } catch (error) {
              if (error.code !== "ENOENT") throw error;
              stale = true;
              return;
            }
          }
        }
      }
      await visit(root);
      let manifest;
      if (!rootMissing && !stale) {
        try {
          manifest = JSON.parse(await fs.readFile(path.join(root, "complete.json"), "utf8"));
        } catch (error) {
          if (error.code !== "ENOENT") throw error;
          manifestMissing = true;
          stale = true;
        }
      }
      if (!selectedOverride && stubPath() !== selected) continue;
      if (rootMissing) {
        assert.ok(initialStubRoots.has(root), "A selected generated typing directory is missing");
        assert.equal(generationObserved, false, "The selected generated typing directory disappeared");
        return { stubPath: selected, files: found, manifest };
      }
      if (manifestMissing && !generationObserved && root === initialManagedStubRoot && found.length === 0) {
        return { stubPath: selected, files: found };
      }
      assert.equal(stale, false, "The selected generated typing directory is incomplete");
      assert.equal(manifest?.owner, "graphql-desk-analysis-generation", "The selected generation has no valid manifest");
      const expected = manifest.files.filter(item => item.path.endsWith(".pyi")).map(item => item.path).sort();
      const actual = found.map(([file]) => path.relative(root, file).replaceAll("\\", "/")).sort();
      assert.deepEqual(actual, expected, "The selected generation has missing or unexpected stubs");
      for (const [file, text] of found) {
        const relative = path.relative(root, file).replaceAll("\\", "/");
        const record = manifest.files.find(item => item.path === relative);
        assert.equal(createHash("sha256").update(text).digest("hex"), record.sha256,
          `Generated stub ${relative} does not match its manifest`);
      }
      if (found.length) generationObserved = true;
      return { stubPath: selected, files: found, manifest };
    }
    throw new Error("The selected generated typing directory changed repeatedly while being read");
  };
  const hasSelected = generation => generation.files.some(([file, text]) =>
    file.includes(`${path.sep}_documents${path.sep}`) && text.split("\n").some(line =>
      line.startsWith("class GraphqlDeskDocument(") && line.includes("FixtureDocument[") && !line.includes("QuarantineTyping.NoReturn")));
  const hasUnavailable = (generation, operationName) => generation.manifest?.operationManifest
    ?.some(group => group.operations.some(operation => operation.operation_name === operationName && operation.status === "unavailable")) ?? false;
  const hasExtra = generation => generation.files.some(([, text]) => text.includes("query Extra { user { id } }"));
  const selectedHover = async () => {
    const items = await vscode.commands.executeCommand("vscode.executeHoverProvider", sourceUri, new vscode.Position(8, 2)) || [];
    return items.flatMap(item => item.contents.map(content => typeof content === "string" ? content : content.value)).join("\n");
  };
  const selectedDiagnostic = () => vscode.languages.getDiagnostics(sourceUri)
    .some(item => item.severity === vscode.DiagnosticSeverity.Error && item.range.start.line === 8);
  const replaceQuery = async (editor, fromQuery, value) => {
    const text = editor.document.getText();
    const offset = text.indexOf(fromQuery);
    assert.notEqual(offset, -1, "The fixture query remains present");
    assert.equal(await editor.edit(edit => edit.replace(
      new vscode.Range(editor.document.positionAt(offset), editor.document.positionAt(offset + fromQuery.length)),
      `query Pick($limit: Int!) { ${value} }`)), true);
  };
  const runEditorCommand = async (document, command) => {
    await vscode.window.showTextDocument(document, { preview: false });
    await vscode.commands.executeCommand(command);
  };
  try {
    assert.equal(vscode.version, process.env.GRAPHQL_DESK_HOST_VERSION);
    for (const [id, version] of [["ms-python.python", "2026.10.0"], ["ms-python.vscode-pylance", "2026.4.1"]]) {
      const extension = vscode.extensions.getExtension(id);
      assert.ok(extension);
      assert.equal(extension.packageJSON.version, version);
      await extension.activate();
      results.extensions ??= {};
      results.extensions[id] = version;
    }
    const document = await vscode.workspace.openTextDocument(sourceUri);
    let editor = await vscode.window.showTextDocument(document, { preview: false });
    const extension = vscode.extensions.getExtension("graphql-desk.graphql-desk-editor");
    assert.ok(extension);
    await extension.activate();
    await until(async () => hasSelected(await readGeneration()), "initial saved operation is published");
    await until(async () => /selected:\s*(?:str|String)\b/.test(await selectedHover()), "Pylance sees the saved result field type");
    const savedGeneration = await readGeneration();
    results.lifecycle.savedBaselinePublished = true;

    const schemaUri = vscode.Uri.file(path.join(workspace, "schema.graphql"));
    const schemaDocument = await vscode.workspace.openTextDocument(schemaUri);
    const baselineSelectedHover = await selectedHover();
    const replaceSelectedSchemaType = async (from, to) => {
      const text = schemaDocument.getText();
      const offset = text.indexOf(`selected: ${from}!`);
      assert.notEqual(offset, -1, `Schema contains selected: ${from}!`);
      const start = schemaDocument.positionAt(offset + "selected: ".length);
      const end = schemaDocument.positionAt(offset + "selected: ".length + from.length);
      const edit = new vscode.WorkspaceEdit();
      edit.replace(schemaUri, new vscode.Range(start, end), to);
      assert.equal(await vscode.workspace.applyEdit(edit), true);
    };
    crashNextArtifacts = true;
    await replaceSelectedSchemaType("String", "Boolean");
    await until(() => workerRequests.some(request => request.op === "artifacts" && request.error),
      "a real worker failure interrupts artifact generation");
    const failedArtifact = workerRequests.find(request => request.op === "artifacts" && request.error);
    assert.ok(failedArtifact.schemas?.some(schema => schema.hasBoolean),
      "The interrupted artifact request contains the changed schema snapshot");
    let failedGeneration;
    let failedQuarantineGeneration;
    await until(async () => {
      const active = await readGeneration();
      if (active.stubPath === savedGeneration.stubPath || hasSelected(active)) return false;
      const generationsRoot = path.join(workspace, ".graphql-desk", "typings", "generations");
      for (const entry of await fs.readdir(generationsRoot, { withFileTypes: true })) {
        if (!entry.isDirectory()) continue;
        try {
          const candidate = await readGeneration(path.join(".graphql-desk", "typings", "generations", entry.name));
          if (candidate.manifest?.analysisId?.startsWith("quarantine-") && hasUnavailable(candidate, "Pick")) {
            failedQuarantineGeneration = candidate;
          }
        } catch { /* Ignore a generation that has not finished publishing. */ }
      }
      if (!failedQuarantineGeneration) return false;
      failedGeneration = active;
      return true;
    }, "worker failure publishes unavailable typing and leaves no stale type selected");
    assert.notEqual(failedGeneration.stubPath, savedGeneration.stubPath,
      "The affected operation switches away from stale typing before analysis succeeds");
    assert.match(failedGeneration.manifest?.analysisId ?? "", /^quarantine-/,
      "The active generation remains in quarantine after worker failure");
    assert.equal(hasUnavailable(failedGeneration, "Pick") || failedGeneration.files.length === 0, true,
      "The active generation either marks Pick unavailable or publishes no stubs");
    assert.equal(hasUnavailable(failedQuarantineGeneration, "Pick"), true,
      "A complete quarantine generation marks the failed Pick operation unavailable");
    assert.equal(hasSelected(failedQuarantineGeneration), false,
      "The quarantine generation does not retain the stale selected-field type");
    assert.equal(hasSelected(failedGeneration), false,
      "The active generation does not retain the stale selected-field type");
    await fs.access(path.join(path.resolve(workspace, failedQuarantineGeneration.stubPath), "complete.json"));
    let failedRefreshHover;
    await until(async () => {
      failedRefreshHover = await selectedHover();
      return failedGeneration.files.length === 0
        ? /\bAny\b/.test(failedRefreshHover)
        : /Never|NoReturn|Unknown/.test(failedRefreshHover);
    },
      "Pylance consumes the unavailable type after worker failure");
    assert.match(failedRefreshHover, failedGeneration.files.length === 0 ? /\bAny\b/ : /Never|NoReturn|Unknown/);
    results.lifecycle.workerFailureKeepsAffectedTypingUnavailable = true;

    await replaceSelectedSchemaType("Boolean", "Int");
    await until(async () => {
      const current = await readGeneration();
      return current.stubPath !== savedGeneration.stubPath && /selected:\s*(?:int|Int)\b/i.test(await selectedHover());
    }, "a fresh schema edit restarts analysis and publishes the current selected type");
    const otherIdHover = async () => {
      const items = await vscode.commands.executeCommand("vscode.executeHoverProvider", sourceUri, new vscode.Position(14, 2)) || [];
      return items.flatMap(item => item.contents.map(content => typeof content === "string" ? content : content.value)).join("\n");
    };
    await until(async () => /other_id:\s*ID\b/.test(await otherIdHover()),
      "the independent ID sibling remains precisely typed after recovery");
    assert.match(await otherIdHover(), /other_id:\s*ID\b/,
      "The unaffected sibling keeps its exact GraphQL ID hover");
    assert.match(await selectedHover(), /selected:\s*Int\b/,
      "The recovered operation uses the new GraphQL Int type");
    results.lifecycle.workerFailureRecoversOnFreshEdit = true;
    results.lifecycle.workerFailurePreservesSiblingId = true;
    results.lifecycle.workerFailureTyping = {
      selectedBeforeFailure: baselineSelectedHover,
      selectedDuringFailure: failedRefreshHover,
      selectedAfterRecovery: await selectedHover(),
      siblingIdAfterRecovery: await otherIdHover(),
    };
    const failedArtifactIndex = workerRequests.findIndex(request => request.op === "artifacts" && request.error);
    const recoveryRequests = workerRequests.slice(failedArtifactIndex + 1);
    assert.ok(recoveryRequests.some(request => request.op === "prepare" && request.ok),
      "Recovery starts a fresh worker session");
    assert.ok(recoveryRequests.some(request => request.op === "artifacts" && request.ok),
      "The fresh worker completes artifact generation");
    results.lifecycle.workerFailureRequests = workerRequests.filter(request => request.op === "artifacts").slice(-3);
    results.lifecycle.workerRecoveryOperations = recoveryRequests.filter(request => ["prepare", "analyze", "artifacts"].includes(request.op))
      .map(({ op, ok, error }) => ({ op, ok: Boolean(ok), ...(error ? { error } : {}) }));

    let delayedArtifactStarted;
    let rejectDelayedArtifact;
    const delayedArtifactReady = new Promise((resolve, reject) => {
      delayedArtifactStarted = resolve;
      rejectDelayedArtifact = reject;
    });
    delayedArtifactTimer = setTimeout(() => rejectDelayedArtifact(
      new Error("The host never produced a result for the delayed schema snapshot"),
    ), 45000);
    const delayedArtifactRelease = new Promise(resolve => { releaseDelayedArtifact = resolve; });
    delayedArtifact = { started: delayedArtifactStarted, release: delayedArtifactRelease };
    await replaceSelectedSchemaType("Int", "Boolean");
    let delayed;
    try { delayed = await delayedArtifactReady; }
    finally { clearTimeout(delayedArtifactTimer); delayedArtifactTimer = null; }
    const delayedRequest = workerRequests.find(request => request.op === "artifacts" && request.id === delayed.id);
    assert.ok(delayedRequest?.schemas?.some(schema => schema.hasBoolean),
      "The held worker result captured the first schema edit");
    await replaceSelectedSchemaType("Boolean", "Int");
    await until(async () => {
      const generation = await readGeneration();
      return generation.files.some(([, text]) => /'selected': 'Int'/.test(text)) &&
        /selected:\s*Int\b/.test(await selectedHover());
    }, "the newer schema result publishes while the earlier worker callback is held");
    const publicationsBeforeStaleRelease = publications.length;
    releaseDelayedArtifact();
    await new Promise(resolve => setTimeout(resolve, 500));
    const afterStaleRelease = await readGeneration();
    assert.ok(afterStaleRelease.files.some(([, text]) => /'selected': 'Int'/.test(text)),
      "A delayed earlier callback cannot replace the newer generated type");
    assert.match(await selectedHover(), /selected:\s*Int\b/);
    assert.equal(publications.length, publicationsBeforeStaleRelease,
      "The stale callback is rejected before managed artifacts are published");
    results.lifecycle.delayedStaleCallbackRejected = {
      staleRequestId: delayed.id,
      newerTypeRemainedSelected: true,
      stalePublicationCount: 0,
    };

    await replaceSelectedSchemaType("Int", "String");
    await until(async () => /selected:\s*(?:str|String)\b/.test(await selectedHover()),
      "restore the lifecycle fixture schema after the recovery proof");

    await replaceQuery(editor, oldQuery, "user { id } count(limit: $limit)");
    await until(async () => {
      const current = await readGeneration();
      return !hasSelected(current);
    }, "unsaved GraphQL edit publishes a complete changed generation");
    assert.equal(document.isDirty, true);
    await until(selectedDiagnostic, "Pylance rejects the removed result field in the unsaved edit");
    results.lifecycle.unsavedEditRotatesGeneration = true;

    await runEditorCommand(document, "undo");
    await until(async () => document.getText() === source && hasSelected(await readGeneration()), "undo restores the saved query typing");
    assert.equal(document.isDirty, false);
    await until(async () => !selectedDiagnostic() && /selected:\s*(?:str|String)\b/.test(await selectedHover()), "Pylance restores the saved result field after undo");
    results.lifecycle.undoRestoresSavedTyping = true;

    await runEditorCommand(document, "redo");
    await until(async () => document.getText().includes(newQuery) && !hasSelected(await readGeneration()), "redo republishes the changed query typing");
    assert.equal(document.isDirty, true);
    await until(selectedDiagnostic, "Pylance rejects the removed result field after redo");
    results.lifecycle.redoRepublishesChangedTyping = true;

    assert.equal(await document.save(), true);
    const savedChangedText = document.getText();
    assert.equal(await fs.readFile(sourcePath, "utf8"), savedChangedText);
    await until(async () => !hasSelected(await readGeneration()), "saved edit remains published");
    results.lifecycle.savePersistsSourceAndTyping = true;

    await replaceQuery(editor, newQuery, "selected count(limit: $limit)");
    await until(async () => hasSelected(await readGeneration()), "a later unsaved edit publishes before discard");
    await runEditorCommand(document, "workbench.action.files.revert");
    await until(async () => document.getText() === savedChangedText && !hasSelected(await readGeneration()), "revert discards only the active source buffer edit");
    assert.equal(document.isDirty, false);
    assert.equal(await fs.readFile(sourcePath, "utf8"), savedChangedText);
    results.lifecycle.discardRestoresSavedTyping = true;

    await vscode.commands.executeCommand("workbench.action.closeActiveEditor");
    const reopened = await vscode.workspace.openTextDocument(sourceUri);
    editor = await vscode.window.showTextDocument(reopened, { preview: false });
    assert.equal(reopened.getText(), savedChangedText);
    await until(async () => !hasSelected(await readGeneration()), "reopened saved source retains the generated type state");
    await until(selectedDiagnostic, "Pylance restores the saved changed result type after close and reopen");
    results.lifecycle.savedDocumentCloseReopenRetainsTyping = true;

    const extraPath = path.join(workspace, "lifecycle_calls.py");
    const renamedPath = path.join(workspace, "renamed_lifecycle_calls.py");
    await until(async () => hasExtra(await readGeneration()), "the additional operation is indexed");
    const beforeRename = await readGeneration();
    await fs.rename(extraPath, renamedPath);
    await until(async () => {
      const current = await readGeneration();
      return current.stubPath !== beforeRename.stubPath && hasExtra(current);
    }, "rename triggers a replacement generation with the operation retained");
    results.lifecycle.sourceRenameReindexes = true;

    const beforeDelete = await readGeneration();
    await fs.unlink(renamedPath);
    await until(async () => {
      const current = await readGeneration();
      return current.stubPath !== beforeDelete.stubPath && !hasExtra(current);
    }, "delete removes the deleted source operation from generated artifacts");
    results.lifecycle.sourceDeleteRemovesOwnedTyping = true;

    const keep = path.join(workspace, ".graphql-desk", "keep.txt");
    assert.equal(await fs.readFile(keep, "utf8"), "preserve this user file");
    assert.equal(await fs.readFile(sourcePath, "utf8"), savedChangedText);
    assert.equal(savedGeneration.files.length > 0, true);
    const finalGeneration = await readGeneration();
    results.lifecycle.activeStubRoot = path.resolve(workspace, finalGeneration.stubPath);
    results.lifecycle.configuredStubPath = finalGeneration.stubPath;
    results.lifecycle.activeStubFiles = finalGeneration.files.map(([file, text]) => [
      path.relative(results.lifecycle.activeStubRoot, file).replaceAll("\\", "/"), text,
    ]).sort(([left], [right]) => left.localeCompare(right));
    results.lifecycle.userSourceAndNeighborFilePreserved = true;
    results.outcome = "managed-output-lifecycle-observed";
  } catch (error) {
    results.error = String(error.stack || error);
    let current = { stubPath: stubPath(), files: [] };
    try { current = await readGeneration(); } catch (readError) {
      current.readError = String(readError.stack || readError);
    }
    results.lifecycle.debug = { sourceChanges, stubPath: current.stubPath,
      ...(current.readError ? { readError: current.readError } : {}),
      types: current.files.map(([file, text]) => ({ file: path.relative(workspace, file), hasSelected: text.includes("'selected': 'String'"), hasChangedQuery: text.includes(newQuery) })),
      text: vscode.workspace.textDocuments.find(document => document.uri.toString() === sourceUri.toString())?.getText(), outputMessages,
      requests: workerRequests, publications, freshnessChecks, documentEvents: documentEvents.slice(-80),
      refreshCallbacks, artifactCalls, skipReasons };
    results.outcome = "managed-output-lifecycle-failed";
    throw error;
  } finally {
    if (delayedArtifactTimer) clearTimeout(delayedArtifactTimer);
    releaseDelayedArtifact?.();
    changedSubscription.dispose();
    await fs.writeFile(process.env.GRAPHQL_DESK_RESULT_PATH, JSON.stringify(results, null, 2));
  }
};
