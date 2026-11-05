"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs/promises");
const path = require("node:path");
const vscode = require("vscode");

async function until(check, label, timeout = 60000) {
  const deadline = Date.now() + timeout;
  while (Date.now() < deadline) {
    if (await check()) return true;
    await new Promise(resolve => setTimeout(resolve, 100));
  }
  assert.fail(label);
}

async function replace(document, before, after) {
  const source = document.getText();
  const start = source.indexOf(before);
  assert.ok(start >= 0, `Expected source expression: ${before}`);
  const edit = new vscode.WorkspaceEdit();
  edit.replace(document.uri, new vscode.Range(document.positionAt(start), document.positionAt(start + before.length)), after);
  assert.equal(await vscode.workspace.applyEdit(edit), true);
}

function diagnostics(document) {
  return vscode.languages.getDiagnostics(document.uri);
}

function precisionDiagnostics(document) {
  return diagnostics(document).filter(item => item.source === "GraphQL Desk" && String(item.code) === "factory-precision");
}

function hover(document, name) {
  const offset = document.getText().indexOf(name) + 1;
  assert.ok(offset > 0, `Expected a source marker for ${name}`);
  return vscode.commands.executeCommand("vscode.executeHoverProvider", document.uri, document.positionAt(offset))
    .then(items => (items || []).flatMap(item => item.contents.map(content => typeof content === "string" ? content : content.value)).join("\n"));
}

function variableType(name, text) {
  return text.match(new RegExp(`\\(variable\\) ${name}: ([^\\n]+)`))?.[1] || "";
}

function lineDiagnostics(document, name) {
  const offset = document.getText().indexOf(name) + 1;
  assert.ok(offset > 0, `Expected a diagnostic marker for ${name}`);
  const line = document.positionAt(offset).line;
  return diagnostics(document).filter(item => item.range.start.line === line);
}

function warningContents(document, warning) {
  const text = document.getText();
  const contextStart = text.indexOf(warning.context);
  assert.ok(contextStart >= 0, `Expected warning context ${warning.context}`);
  const operandStart = warning.context.indexOf(warning.operand);
  assert.ok(operandStart >= 0, `Expected ${warning.operand} inside ${warning.context}`);
  const offset = contextStart + operandStart;
  assert.ok(offset >= 0, `Expected warning operand ${warning.operand}`);
  assert.equal(String(warning.item.code), "factory-precision");
  assert.equal(warning.item.severity, vscode.DiagnosticSeverity.Warning);
  assert.equal(warning.item.source, "GraphQL Desk");
  assert.ok(warning.item.range.isEqual(new vscode.Range(document.positionAt(offset),
    document.positionAt(offset + warning.operand.length))), `Wrong operand range for ${warning.operand}: ${JSON.stringify(warning.item.range)}`);
  assert.equal(warning.item.message, warning.message);
}

function assertNoQuarantineFailures(trace, label) {
  const failures = trace.filter(line => /Typing quarantine failed|quarantine (?:request|protocol).*(?:error|failed|rejected)/i.test(line));
  assert.deepEqual(failures, [], `${label}: ${failures.join("\n")}`);
}

async function assertPrecisionWarnings(document, expectedWarnings, label) {
  await until(() => {
    const current = precisionDiagnostics(document);
    if (current.length !== expectedWarnings.length) return false;
    return expectedWarnings.every(expected => {
      const contextStart = document.getText().indexOf(expected.context);
      const operandStart = expected.context.indexOf(expected.operand);
      if (contextStart < 0 || operandStart < 0) return false;
      const offset = contextStart + operandStart;
      const start = document.positionAt(offset);
      const end = document.positionAt(offset + expected.operand.length);
      return current.some(item => document.getText().slice(document.offsetAt(item.range.start), document.offsetAt(item.range.end)) === expected.operand &&
        item.range.isEqual(new vscode.Range(start, end)) && item.source === "GraphQL Desk" &&
        String(item.code) === "factory-precision" && item.severity === vscode.DiagnosticSeverity.Warning && item.message === expected.message);
    });
  }, label);
  const found = precisionDiagnostics(document);
  for (const expected of expectedWarnings) {
    const item = found.find(candidate => document.getText().slice(document.offsetAt(candidate.range.start), document.offsetAt(candidate.range.end)) === expected.operand);
    assert.ok(item, `Missing precision warning on ${expected.operand}: ${JSON.stringify(found.map(value => value.message))}`);
    warningContents(document, { ...expected, item });
  }
  assert.deepEqual(found.map(item => document.getText().slice(document.offsetAt(item.range.start), document.offsetAt(item.range.end))).sort(),
    expectedWarnings.map(item => item.operand).sort(), "Only the expected widened source, selector, and list operands receive precision warnings");
  return found;
}

async function assertPreciseControls(document, label) {
  let observed;
  await until(async () => {
    observed = {
      finalSource: await hover(document, "final_source_value"),
      literalSource: await hover(document, "literal_source_value"),
      finalOperation: await hover(document, "final_source_operation"),
      literalOperation: await hover(document, "literal_source_operation"),
      operationValue: await hover(document, "precise_operation_value"),
      operationName: await hover(document, "precise_operation_name"),
      fragmentId: await hover(document, "tuple_fragment_id"),
    };
    return /String|str/.test(observed.finalSource) && /String|str/.test(observed.literalSource) &&
      /Literal\[['"]SourceFinal['"]\]/.test(observed.finalOperation) &&
      /Literal\[['"]SourceLiteral['"]\]/.test(observed.literalOperation) &&
      /Int|int/.test(observed.operationValue) && /Literal\[['"]Second['"]\]/.test(observed.operationName) &&
      /ID|str/.test(observed.fragmentId);
  }, label);
  await until(() => lineDiagnostics(document, "final_source_ok").length === 0 &&
    lineDiagnostics(document, "precise_operation_ok").length === 0 &&
    lineDiagnostics(document, "precise_operation_bad_variables").length > 0 &&
    lineDiagnostics(document, "tuple_fragment_id_ok").length === 0,
  "Pylance confirms precise result and variable types");
  return observed;
}

async function assertRepairedControls(document, label) {
  let observed;
  await until(async () => {
    observed = {
      sourceOperation: await hover(document, "loose_source_operation"),
      operationName: await hover(document, "loose_operation_name"),
      operationValue: await hover(document, "loose_operation_value"),
      fragmentId: await hover(document, "loose_fragment_id"),
      fragmentName: await hover(document, "loose_fragment_name"),
    };
    return precisionDiagnostics(document).length === 0 &&
      /Literal\[['"]SourceLoose['"]\]/.test(observed.sourceOperation) &&
      /Literal\[['"]Second['"]\]/.test(observed.operationName) && /Int|int/.test(observed.operationValue) &&
      /ID|str/.test(observed.fragmentId) && /String|str/.test(observed.fragmentName) &&
      ["loose_source_result", "loose_operation_result", "loose_fragment_result"]
        .every(name => lineDiagnostics(document, name).length === 0);
  }, label);
  return observed;
}

async function updateWorkspaceFolders(start, deleteCount, ...folders) {
  let changed = false;
  const listener = vscode.workspace.onDidChangeWorkspaceFolders(() => { changed = true; });
  try {
    assert.equal(vscode.workspace.updateWorkspaceFolders(start, deleteCount, ...folders), true);
    await until(() => changed, "workspace-folder change event arrives");
  } finally { listener.dispose(); }
}

exports.run = async () => {
  const temporaryRoot = process.env.GRAPHQL_DESK_TEST_TEMP_ROOT;
  assert.ok(temporaryRoot, "The host runner must provide its owned temporary root");
  const expectedOsTemp = path.join(temporaryRoot, "os-temp");
  for (const name of ["TEMP", "TMP"]) {
    assert.equal(path.resolve(process.env[name] || ""), expectedOsTemp, `${name} must stay inside this host run's temporary root`);
  }
  await fs.access(expectedOsTemp);
  if (process.env.GRAPHQL_DESK_TEST_NODE_PATH) {
    process.env.NODE_PATH = process.env.GRAPHQL_DESK_TEST_NODE_PATH;
    require("node:module").Module._initPaths();
  }
  const workspace = process.env.GRAPHQL_DESK_TEST_WORKSPACE;
  const python = process.env.GRAPHQL_DESK_PYTHON;
  const extensionsRoot = path.resolve(process.env.GRAPHQL_DESK_TEST_EXTENSIONS || "");
  assert.ok(process.env.GRAPHQL_DESK_TEST_EXTENSIONS, "The host runner must provide its isolated extensions directory");
  const sourceUri = vscode.Uri.file(path.join(workspace, "consumer.py"));
  const results = { host: vscode.version, extensions: {}, warnings: [], trace: [] };
  const originalCreateOutputChannel = vscode.window.createOutputChannel;
  vscode.window.createOutputChannel = (name, ...args) => {
    const channel = originalCreateOutputChannel.call(vscode.window, name, ...args);
    if (name !== "GraphQL Desk") return channel;
    return new Proxy(channel, { get(target, property) {
      if (property === "appendLine") return value => {
        results.trace.push(String(value));
        return target.appendLine(value);
      };
      const value = Reflect.get(target, property, target);
      return typeof value === "function" ? value.bind(target) : value;
    } });
  };
  try {
    const pinnedCoreExtensions = new Map([["ms-python.python", "2026.10.0"], ["ms-python.vscode-pylance", "2026.4.1"]]);
    const extensionIds = [...pinnedCoreExtensions.keys(), "ms-python.debugpy", "ms-python.vscode-python-envs"];
    for (const id of extensionIds) {
      const extension = vscode.extensions.getExtension(id);
      assert.ok(extension, `${id} must be installed`);
      if (pinnedCoreExtensions.has(id)) {
        assert.equal(extension.packageJSON.version, pinnedCoreExtensions.get(id));
        await extension.activate();
      }
      const extensionPath = path.resolve(extension.extensionPath);
      const relativePath = path.relative(extensionsRoot, extensionPath);
      assert.ok(relativePath && !path.isAbsolute(relativePath) && relativePath !== ".." && !relativePath.startsWith(`..${path.sep}`),
        `${id} must load from this host run's isolated extensions directory: ${extensionPath}`);
      results.extensions[id] = {
        version: extension.packageJSON.version,
        path: relativePath.replaceAll("\\", "/"),
        active: extension.isActive,
      };
    }
    const graphQlDesk = vscode.extensions.getExtension("graphql-desk.graphql-desk-editor");
    assert.ok(graphQlDesk, "GraphQL Desk extension must be loaded");
    await graphQlDesk.activate();
    const document = await vscode.workspace.openTextDocument(sourceUri);
    await vscode.window.showTextDocument(document, { preview: false });
    const originalDisk = await fs.readFile(sourceUri.fsPath, "utf8");

    const initialWarnings = [
      { operand: "SOURCE_LOOSE", context: "loose_source_doc = graphql(SOURCE_LOOSE)", message: "Exact overload inference requires an inline string literal or a stable module-level Final/Literal alias chain; widened or dynamic sources may become str in a checker" },
      { operand: "SELECTOR_LOOSE", context: "operation=SELECTOR_LOOSE", message: "Exact operation selection requires a literal keyword value" },
      { operand: "[card]", context: '"query FragmentList { user { id ...Card } }", [card]', message: "Exact fragment overload inference requires a fixed tuple; lists and widened fragment collections use the untyped fallback" },
    ];
    await until(async () => {
      results.initialDiagnostics = diagnostics(document).map(item => ({ source: item.source, code: String(item.code), severity: item.severity,
        message: item.message, range: { start: item.range.start, end: item.range.end } }));
      return precisionDiagnostics(document).length === initialWarnings.length;
    }, "Pylance host receives all three GraphQL Desk precision warnings");
    const found = precisionDiagnostics(document);
    for (const expected of initialWarnings) {
      const item = found.find(candidate => {
        const range = document.getText().slice(document.offsetAt(candidate.range.start), document.offsetAt(candidate.range.end));
        return range === expected.operand;
      });
      assert.ok(item, `Missing precision warning on ${expected.operand}: ${JSON.stringify(found.map(value => value.message))}`);
      warningContents(document, { ...expected, item });
      results.warnings.push({ operand: expected.operand, code: String(item.code), severity: item.severity, message: item.message,
        range: document.getText().slice(document.offsetAt(item.range.start), document.offsetAt(item.range.end)) });
    }
    assert.deepEqual(found.map(item => document.getText().slice(document.offsetAt(item.range.start), document.offsetAt(item.range.end))).sort(),
      initialWarnings.map(item => item.operand).sort(), "Exact Final/Literal aliases and tuple fragments do not receive precision warnings");

    const nonPython = await vscode.workspace.openTextDocument({ language: "markdown", content: "Reviewing another file." });
    await vscode.window.showTextDocument(nonPython, { preview: false });
    await new Promise(resolve => setTimeout(resolve, 250));
    assert.equal(precisionDiagnostics(document).length, 3, "Switching away from Python preserves current factory warnings");
    await vscode.window.showTextDocument(document, { preview: false });

    const initialControlsReady = await until(async () => {
      results.initialFinalSource = await hover(document, "final_source_value");
      results.initialLiteralSource = await hover(document, "literal_source_value");
      results.initialFinalOperation = await hover(document, "final_source_operation");
      results.initialLiteralOperation = await hover(document, "literal_source_operation");
      results.initialPreciseOperation = await hover(document, "precise_operation_value");
      results.initialPreciseOperationName = await hover(document, "precise_operation_name");
      results.initialTupleFragment = await hover(document, "tuple_fragment_id");
      results.initialPylanceDiagnostics = diagnostics(document).filter(item => item.source !== "GraphQL Desk")
        .map(item => ({ source: item.source, code: String(item.code), message: item.message, range: item.range }));
      return /String|str/.test(results.initialFinalSource) && /String|str/.test(results.initialLiteralSource) &&
        /Literal\[['"]SourceFinal['"]\]/.test(results.initialFinalOperation) &&
        /Literal\[['"]SourceLiteral['"]\]/.test(results.initialLiteralOperation) &&
        /Int|int/.test(results.initialPreciseOperation) &&
        /Literal\[['"]Second['"]\]/.test(results.initialPreciseOperationName) &&
        /ID|str/.test(results.initialTupleFragment);
    }, "Pylance infers documented Final, Literal, and tuple controls");
    assert.ok(initialControlsReady, JSON.stringify(results));

    await replace(document, 'SOURCE_LOOSE = "query SourceLoose { selected }"', 'SOURCE_LOOSE: Final = "query SourceLoose { selected }"');
    await replace(document, 'operation=SELECTOR_LOOSE)', 'operation="Second")');
    await replace(document, '"query FragmentList { user { id ...Card } }", [card]', '"query FragmentList { user { id ...Card } }", (card,)');

    const repaired = await until(async () => {
      results.repairedSource = await hover(document, "loose_source_operation");
      results.repairedOperationName = await hover(document, "loose_operation_name");
      results.repairedOperationValue = await hover(document, "loose_operation_value");
      results.repairedFragmentId = await hover(document, "loose_fragment_id");
      results.repairedFragment = await hover(document, "loose_fragment_name");
      results.repairedChecks = {
        warningsCleared: precisionDiagnostics(document).length === 0,
        sourceLiteral: /Literal\[['"]SourceLoose['"]\]/.test(results.repairedSource),
        operationLiteral: /Literal\[['"]Second['"]\]/.test(results.repairedOperationName),
        operationValue: /Int|int/.test(results.repairedOperationValue),
        fragmentId: /ID|str/.test(results.repairedFragmentId),
        fragmentName: /String|str/.test(results.repairedFragment),
        sourceFallbackErrorCleared: lineDiagnostics(document, "loose_source_result").length === 0,
        operationFallbackErrorCleared: lineDiagnostics(document, "loose_operation_result").length === 0,
        fragmentFallbackErrorCleared: lineDiagnostics(document, "loose_fragment_result").length === 0,
      };
      results.repairedPylanceDiagnostics = diagnostics(document).filter(item => item.source !== "GraphQL Desk")
        .map(item => ({ message: item.message, range: item.range }));
      return Object.values(results.repairedChecks).every(Boolean);
    }, "Repairs clear warnings, restore typed overloads, and remove fallback errors");
    assert.ok(repaired, JSON.stringify(results));
    assertNoQuarantineFailures(results.trace, "The repaired source refresh must complete its worker quarantine request");

    for (const name of ["final_source_ok", "literal_source_ok", "precise_operation_ok", "tuple_fragment_id_ok", "tuple_fragment_name_ok"]) {
      assert.equal(lineDiagnostics(document, name).length, 0, `${name} should type-check`);
    }
    for (const name of ["final_source_bad", "literal_source_bad", "precise_operation_bad", "precise_operation_bad_variables", "tuple_fragment_id_bad", "tuple_fragment_name_bad"]) {
      assert.ok(lineDiagnostics(document, name).length > 0, `${name} should retain its expected type error`);
    }
    results.repaired = {
      sourceType: variableType("final_source_value", results.initialFinalSource),
      operationName: variableType("precise_operation_name", await hover(document, "precise_operation_name")),
      operationType: variableType("precise_operation_value", results.initialPreciseOperation),
      fragmentIdType: variableType("tuple_fragment_id", results.initialTupleFragment),
      precisionWarningsCleared: precisionDiagnostics(document).length === 0,
      staleFallbackErrorsCleared: ["loose_source_result", "loose_operation_result", "loose_fragment_result"]
        .every(name => lineDiagnostics(document, name).length === 0),
    };

    await replace(document, 'SOURCE_LOOSE: Final = "query SourceLoose { selected }"', 'SOURCE_LOOSE = "query SourceLoose { selected }"');
    await replace(document, 'operation="Second")', 'operation=SELECTOR_LOOSE)');
    await replace(document, '"query FragmentList { user { id ...Card } }", (card,)', '"query FragmentList { user { id ...Card } }", [card]');
    await until(() => precisionDiagnostics(document).length === 3, "Restored widened expressions republish current warnings");

    const removedWorkspace = process.env.GRAPHQL_DESK_TEST_REMOVED_WORKSPACE;
    if (removedWorkspace) {
      assert.ok(await fs.realpath(removedWorkspace), "A second isolated workspace root is available");
      await fs.mkdir(path.join(removedWorkspace, ".vscode"), { recursive: true });
      await fs.mkdir(path.join(removedWorkspace, ".graphql-desk"), { recursive: true });
      for (const name of ["pyproject.toml", "pyrightconfig.json", "schema.graphql", "consumer.py", "fragments.py"]) {
        await fs.copyFile(path.join(workspace, name), path.join(removedWorkspace, name));
      }
      await fs.copyFile(path.join(workspace, ".graphql-desk", "keep.txt"), path.join(removedWorkspace, ".graphql-desk", "keep.txt"));
      await fs.writeFile(path.join(removedWorkspace, ".vscode", "settings.json"), JSON.stringify({
        "python.defaultInterpreterPath": python,
        "graphqlDesk.pythonPath": python,
        "python.analysis.typeCheckingMode": "strict",
        "python.analysis.stubPath": "python-custom-stubs",
      }, null, 2));
      await fs.cp(path.join(workspace, "graphql_desk_generated"), path.join(removedWorkspace, "graphql_desk_generated"), { recursive: true });
      const removedUri = vscode.Uri.file(removedWorkspace);
      const removedSourceUri = vscode.Uri.file(path.join(removedWorkspace, "consumer.py"));
      const removedDocument = await vscode.workspace.openTextDocument(removedSourceUri);
      await vscode.window.showTextDocument(removedDocument, { preview: false });
      await assertPrecisionWarnings(removedDocument, initialWarnings, "Second project publishes its exact independent precision diagnostics");
      await assertPreciseControls(removedDocument, "Second project receives precise Final/Literal and tuple inference");
      await replace(removedDocument, 'SOURCE_LOOSE = "query SourceLoose { selected }"', 'SOURCE_LOOSE: Final = "query SourceLoose { selected }"');
      await replace(removedDocument, 'operation=SELECTOR_LOOSE)', 'operation="Second")');
      await replace(removedDocument, '"query FragmentList { user { id ...Card } }", [card]', '"query FragmentList { user { id ...Card } }", (card,)');
      await assertRepairedControls(removedDocument, "Second project repairs restore typed overloads and clear fallback errors");
      assertNoQuarantineFailures(results.trace, "The second root's repaired source refresh must complete its worker quarantine request");
      await replace(removedDocument, 'SOURCE_LOOSE: Final = "query SourceLoose { selected }"', 'SOURCE_LOOSE = "query SourceLoose { selected }"');
      await replace(removedDocument, 'operation="Second")', 'operation=SELECTOR_LOOSE)');
      await replace(removedDocument, '"query FragmentList { user { id ...Card } }", (card,)', '"query FragmentList { user { id ...Card } }", [card]');
      await assertPrecisionWarnings(removedDocument, initialWarnings, "Second project restores exact warnings after reverting the three widened expressions");
      const dirtyEdit = new vscode.WorkspaceEdit();
      dirtyEdit.insert(removedSourceUri, new vscode.Position(0, 0), "# user edit\n");
      assert.equal(await vscode.workspace.applyEdit(dirtyEdit), true);
      await assertPrecisionWarnings(removedDocument, initialWarnings, "Second project's dirty buffer keeps exact warning ranges and messages");
      const removedBuffer = removedDocument.getText();
      const removedVersion = removedDocument.version;
      const removedDisk = await fs.readFile(removedSourceUri.fsPath, "utf8");
      const removedIndex = vscode.workspace.getWorkspaceFolder(removedUri).index;
      await vscode.window.showTextDocument(document, { preview: false });
      await updateWorkspaceFolders(removedIndex, 1);
      await until(() => !vscode.workspace.getWorkspaceFolder(removedUri) && precisionDiagnostics(removedDocument).length === 0,
        "Removing a workspace root clears only that project's factory diagnostics");
      await until(() => precisionDiagnostics(document).length === 3,
        "Removing one root preserves another root's current diagnostics");
      assert.equal(removedDocument.getText(), removedBuffer);
      assert.equal(removedDocument.version, removedVersion);
      assert.equal(removedDocument.isDirty, true);
      assert.equal(await fs.readFile(removedSourceUri.fsPath, "utf8"), removedDisk);
      assert.equal(await fs.readFile(path.join(removedWorkspace, ".graphql-desk", "keep.txt"), "utf8"), "preserve this user file");
      results.secondRoot = {
        exactWarnings: true,
        preciseControls: true,
        repairedControls: true,
        restoredWarnings: true,
        dirtyBufferWarnings: true,
      };
    }
    assert.equal(await fs.readFile(sourceUri.fsPath, "utf8"), originalDisk, "GraphQL Desk preserves the user's source file on cleanup");
    assert.equal(document.isDirty, true, "Editor repairs remain in the user's dirty buffer");
    results.lifecycle = { removedRootDiagnosticsCleared: Boolean(removedWorkspace), removedBufferPreserved: Boolean(removedWorkspace),
      otherRootDiagnosticsPreserved: Boolean(removedWorkspace),
      userSourceUnsaved: document.isDirty };
    const selectedStubPath = vscode.workspace.getConfiguration("python", sourceUri).get("analysis.stubPath");
    results.lifecycle.activeStubRoot = path.resolve(workspace, selectedStubPath);
    results.lifecycle.activeDefaultStub = await fs.readFile(
      path.join(results.lifecycle.activeStubRoot, "graphql_desk_generated", "default.pyi"), "utf8");
    results.outcome = "factory-precision-diagnostics-observed";
  } catch (error) {
    results.error = String(error.stack || error);
    results.outcome = "factory-precision-diagnostics-failed";
    throw error;
  } finally {
    vscode.window.createOutputChannel = originalCreateOutputChannel;
    require("node:fs").writeFileSync(process.env.GRAPHQL_DESK_RESULT_PATH, JSON.stringify(results, null, 2));
    console.log(`GRAPHQL_DESK_FACTORY_PRECISION ${JSON.stringify(results)}`);
  }
};
