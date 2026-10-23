"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs/promises");
const path = require("node:path");
const vscode = require("vscode");

async function until(check, label, timeout = 45000) {
  const deadline = Date.now() + timeout;
  while (Date.now() < deadline) {
    if (await check()) return true;
    await new Promise(resolve => setTimeout(resolve, 100));
  }
  assert.fail(label);
}

exports.run = async () => {
  const workspace = process.env.GRAPHQL_DESK_TEST_WORKSPACE;
  const phase = process.env.GRAPHQL_DESK_RESTART_PHASE;
  const consumerUri = vscode.Uri.file(path.join(workspace, "consumer.py"));
  const results = { host: vscode.version, phase, lifecycle: {} };
  try {
    assert.ok(["before-restart", "after-restart"].includes(phase));
    assert.equal(vscode.version, process.env.GRAPHQL_DESK_HOST_VERSION);
    for (const [id, version] of [["ms-python.python", "2026.10.0"], ["ms-python.vscode-pylance", "2026.4.1"]]) {
      const extension = vscode.extensions.getExtension(id);
      assert.ok(extension);
      assert.equal(extension.packageJSON.version, version);
      await extension.activate();
    }
    const document = await vscode.workspace.openTextDocument(consumerUri);
    await vscode.window.showTextDocument(document, { preview: false });
    const extension = vscode.extensions.getExtension("graphql-desk.graphql-desk-editor");
    assert.ok(extension);
    await extension.activate();
    const hover = async () => {
      const items = await vscode.commands.executeCommand("vscode.executeHoverProvider", consumerUri, new vscode.Position(8, 2)) || [];
      return items.flatMap(item => item.contents.map(content => typeof content === "string" ? content : content.value)).join("\n");
    };
    const hasSelectedType = text => /\bselected:\s*(?:String|str)\b/.test(text);
    const generatedPath = () => path.join(workspace,
      vscode.workspace.getConfiguration("python", consumerUri).get("analysis.stubPath", ".graphql-desk/typings"),
      "graphql_desk_generated", "_documents", "default", "d0.pyi");
    await until(async () => {
      const stub = await fs.readFile(generatedPath(), "utf8").catch(() => "");
      return stub.includes("'selected': 'String'") && hasSelectedType(await hover());
    }, "a fresh VS Code process resolves the retained generated typing with Pylance");
    results.lifecycle.generatedStubAvailableAfterRestart = true;
    results.lifecycle.pylanceHover = await hover();
    results.lifecycle.invalidVariableDiagnostic = await until(async () => vscode.languages.getDiagnostics(consumerUri)
      .some(item => item.severity === vscode.DiagnosticSeverity.Error && item.range.start.line === 11),
    "a fresh VS Code process restores operation-variable diagnostics");
    results.lifecycle.stubPath = vscode.workspace.getConfiguration("python", consumerUri).get("analysis.stubPath");
    assert.ok(typeof results.lifecycle.stubPath === "string" && results.lifecycle.stubPath.includes("generations"));
    if (phase === "after-restart") {
      await vscode.commands.executeCommand("workbench.action.closeActiveEditor");
      const reopened = await vscode.workspace.openTextDocument(consumerUri);
      await vscode.window.showTextDocument(reopened, { preview: false });
      await until(async () => hasSelectedType(await hover()), "reopened Python document retains generated typing");
      await until(async () => vscode.languages.getDiagnostics(consumerUri)
        .some(item => item.severity === vscode.DiagnosticSeverity.Error && item.range.start.line === 11),
      "the reopened Python document restores operation-variable diagnostics");
      results.lifecycle.savedConsumerReopenRetainsTyping = true;
    }
    results.outcome = "owned-output-restart-observed";
  } catch (error) {
    results.error = String(error.stack || error);
    results.outcome = "owned-output-restart-failed";
    throw error;
  } finally {
    await fs.writeFile(process.env.GRAPHQL_DESK_RESULT_PATH, JSON.stringify(results, null, 2));
  }
};
