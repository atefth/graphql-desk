"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs/promises");
const path = require("node:path");
const vscode = require("vscode");

async function until(action, accept, label, timeout = 45000) {
  const deadline = Date.now() + timeout;
  let value;
  do {
    value = await action();
    if (accept(value)) return value;
    await new Promise(resolve => setTimeout(resolve, 100));
  } while (Date.now() < deadline);
  assert.fail(`${label}: last observed ${JSON.stringify(value)}`);
}

function hoverText(values) {
  return (values ?? []).flatMap(value => value.contents ?? [])
    .map(value => typeof value === "string" ? value : value.value ?? "").join("\n");
}

exports.run = async () => {
  const workspace = process.env.GRAPHQL_DESK_TEST_WORKSPACE;
  const resultPath = process.env.GRAPHQL_DESK_RESULT_PATH;
  const consumerPath = path.join(workspace, "consumer.py");
  const consumer = await vscode.workspace.openTextDocument(vscode.Uri.file(consumerPath));
  const uri = consumer.uri;
  const position = consumer.positionAt(consumer.getText().indexOf("value") + 1);
  const getHover = async () => hoverText(await vscode.commands.executeCommand("vscode.executeHoverProvider", uri, position));
  for (const [id, version] of [["ms-python.python", "2026.10.0"], ["ms-python.vscode-pylance", "2026.4.1"]]) {
    const extension = vscode.extensions.getExtension(id);
    assert.ok(extension, `${id} is installed in the isolated host`);
    await extension.activate();
    assert.equal(extension.packageJSON.version, version);
  }
  await vscode.window.showTextDocument(consumer);
  const before = await until(getHover, value => /value:\s*str\b/.test(value), "Pylance resolves a generated stub through project extraPaths");
  const generationFile = vscode.Uri.file(path.join(workspace, ".graphql-desk", "generations", "g-1", "graphql_desk_generated", "default.pyi"));
  const generationDocument = await vscode.workspace.openTextDocument(generationFile);
  assert.equal(generationDocument.isDirty, false, "managed generation starts clean");
  const source = generationDocument.getText();
  const start = source.indexOf("value: str");
  assert.ok(start >= 0, "managed generation has its expected original annotation");
  const edit = new vscode.WorkspaceEdit();
  edit.replace(generationFile, new vscode.Range(generationDocument.positionAt(start), generationDocument.positionAt(start + "value: str".length)), "value: str | None");
  assert.equal(await vscode.workspace.applyEdit(edit), true, "VS Code applies the managed in-memory stub update");
  const writeFinishedAt = Date.now();
  assert.equal(await generationDocument.save(), true, "the managed stub is saved to disk and the document becomes clean");
  const after = await until(getHover, value => /value:\s*(?:str\s*\|\s*None|str\s*\|\s*NoneType|Optional\[str\])/i.test(value),
    "Pylance refreshes generated typing after a saved VS Code document update without changing extraPaths");
  const refreshMs = Date.now() - writeFinishedAt;
  assert.doesNotMatch(after, /value:\s*str\s*\|\s*None\s*\|\s*None/);
  const diagnostic = await until(
    () => vscode.languages.getDiagnostics(uri).find(item => item.range.start.line === 4 && /str/.test(item.message)),
    value => Boolean(value), "Pylance reports the nullable assignment after the project config refresh",
  );
  assert.match(diagnostic.message, /None|nullable/i);
  const settings = vscode.workspace.getConfiguration("python", uri);
  const workspaceStubPath = settings.get("analysis.stubPath", "");
  assert.equal(workspaceStubPath, "workspace-shadow", "Pyright project configuration remains authoritative over the workspace stubPath setting");
  await fs.writeFile(resultPath, JSON.stringify({ vscode: vscode.version, before, after,
    refreshMs, workspaceStubPath, configuredProjectStubPath: "custom_stubs", stableExtraPath: true,
    generatedDocumentClean: !generationDocument.isDirty,
    persistedGeneration: await fs.readFile(generationFile.fsPath, "utf8") }, null, 2));
};
