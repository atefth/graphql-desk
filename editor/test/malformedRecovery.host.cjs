"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs/promises");
const path = require("node:path");
const vscode = require("vscode");

async function until(check, label) {
  const deadline = Date.now() + 60000;
  while (Date.now() < deadline) {
    if (await check()) return true;
    await new Promise(resolve => setTimeout(resolve, 100));
  }
  assert.fail(`${label} was not observed`);
}

async function replace(document, before, after) {
  const source = document.getText();
  const start = source.indexOf(before);
  assert.ok(start >= 0, `Expected source text ${before}`);
  const edit = new vscode.WorkspaceEdit();
  edit.replace(document.uri, new vscode.Range(document.positionAt(start), document.positionAt(start + before.length)), after);
  assert.equal(await vscode.workspace.applyEdit(edit), true);
}

function hoverText(items) {
  return (items || []).flatMap(item => item.contents.map(content => typeof content === "string" ? content : content.value)).join("\n");
}

exports.run = async () => {
  const results = { host: vscode.version, pylance: {}, lifecycle: {} };
  try {
    for (const [id, version] of [["ms-python.python", "2026.10.0"], ["ms-python.vscode-pylance", "2026.4.1"]]) {
      const extension = vscode.extensions.getExtension(id);
      assert.ok(extension, `${id} must be installed in the isolated host`);
      await extension.activate();
      assert.equal(extension.packageJSON.version, version);
      results.pylance[id] = version;
    }

    const workspace = process.env.GRAPHQL_DESK_TEST_WORKSPACE;
    const sourcePath = path.join(workspace, "consumer.py");
    const uri = vscode.Uri.file(sourcePath);
    const graph = vscode.extensions.getExtension("graphql-desk.graphql-desk-editor");
    assert.ok(graph, "GraphQL Desk must be loaded in the host");
    await graph.activate();
    const document = await vscode.workspace.openTextDocument(uri);
    await vscode.window.showTextDocument(document, { preview: false });
    const original = document.getText();
    const diskOriginal = await fs.readFile(sourcePath, "utf8");
    assert.equal(original, diskOriginal);
    const hover = async marker => {
      const offset = document.getText().indexOf(marker) + 2;
      assert.ok(offset > 1, `Expected source marker ${marker}`);
      return hoverText(await vscode.commands.executeCommand("vscode.executeHoverProvider", uri, document.positionAt(offset)));
    };
    const selectedHover = () => hover("selected = ");
    const siblingHover = () => hover("other_id = ");
    await until(async () => /String/.test(await selectedHover()) && /\bID\b/.test(await siblingHover()),
      "initial generated operation and sibling types");

    const selectedBefore = "{ selected count(limit: $limit) }";
    const malformed = "{ selected count(limit: $limit }";
    assert.ok(original.includes(selectedBefore));
    const invalid = original.replace(selectedBefore, malformed);
    await replace(document, selectedBefore, malformed);
    results.lifecycle.malformedEdit = await until(async () => {
      const selected = await selectedHover();
      const sibling = await siblingHover();
      const current = vscode.languages.getDiagnostics(uri);
      let statuses;
      try {
        const settings = JSON.parse(await fs.readFile(path.join(workspace, ".vscode", "settings.json"), "utf8"));
        const generation = path.resolve(workspace, settings["python.analysis.stubPath"]);
        const manifest = JSON.parse(await fs.readFile(path.join(generation, "complete.json"), "utf8"));
        statuses = Object.fromEntries(manifest.operationManifest.flatMap(group => group.operations.map(item => [item.binding, item.status])));
      } catch { /* Wait for the selected complete generation. */ }
      const parseDiagnostic = current.find(item => item.source === "GraphQL Desk" && /syntax|parse|expected|end of file/i.test(item.message));
      results.lifecycle.malformedEditProbe = { selected, sibling, statuses, parseDiagnostic: parseDiagnostic?.message,
        dirty: document.isDirty };
      return /Never|NoReturn|Unknown/.test(selected) && /\bID\b/.test(sibling) &&
        statuses?.doc === "unavailable" && statuses?.other_doc === "precise" && Boolean(parseDiagnostic);
    }, "malformed GraphQL suspends only its operation and reports a parse diagnostic");
    results.lifecycle.dirtySourcePreserved = document.isDirty;
    assert.ok(document.isDirty, "The malformed edit must remain unsaved in the user's buffer");
    assert.equal(document.getText(), invalid, "GraphQL Desk must preserve the dirty source buffer");
    assert.equal(await fs.readFile(sourcePath, "utf8"), diskOriginal, "GraphQL Desk must not write the source file");

    await replace(document, malformed, selectedBefore);
    results.lifecycle.repair = await until(async () => {
      const selected = await selectedHover();
      const sibling = await siblingHover();
      const parseDiagnostics = vscode.languages.getDiagnostics(uri).filter(item => item.source === "GraphQL Desk");
      results.lifecycle.repairProbe = { selected, sibling, parseDiagnostics: parseDiagnostics.map(item => item.message) };
      return /String/.test(selected) && /\bID\b/.test(sibling) && parseDiagnostics.length === 0;
    }, "repair restores precise operation types and clears diagnostics");
    assert.equal(document.getText(), original, "Repair must restore the original in-memory source");
    assert.equal(await fs.readFile(sourcePath, "utf8"), diskOriginal, "The on-disk source must remain unchanged");
    results.outcome = "malformed-graphql-recovery-observed";
    results.lifecycle.configuredStubPath = vscode.workspace.getConfiguration("python", uri).get("analysis.stubPath");
    results.lifecycle.activeStubRoot = path.resolve(workspace, results.lifecycle.configuredStubPath);
    assert.ok(results.lifecycle.configuredStubPath, "Live typing must leave a selected managed generation");
  } catch (error) {
    results.error = String(error.stack || error);
    results.outcome = "malformed-graphql-recovery-failed";
    throw error;
  } finally {
    await fs.writeFile(process.env.GRAPHQL_DESK_RESULT_PATH, JSON.stringify(results, null, 2));
  }
};
