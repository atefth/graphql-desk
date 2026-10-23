"use strict";
const assert = require("node:assert/strict");
const path = require("node:path");
const fs = require("node:fs/promises");
const vscode = require("vscode");

async function until(check, label) {
  const deadline = Date.now() + 30000;
  while (Date.now() < deadline) {
    if (await check()) return;
    await new Promise(resolve => setTimeout(resolve, 100));
  }
  assert.fail(label);
}

async function updateFolders(start, deleteCount, ...folders) {
  let changed = false;
  const listener = vscode.workspace.onDidChangeWorkspaceFolders(() => { changed = true; });
  try {
    assert.equal(vscode.workspace.updateWorkspaceFolders(start, deleteCount, ...folders), true);
    await until(() => changed, "workspace folder change event arrives before the next update");
  } finally { listener.dispose(); }
}

exports.run = async () => {
  const workspace = process.env.GRAPHQL_DESK_TEST_WORKSPACE;
  const stubRoot = process.env.GRAPHQL_DESK_STUB_ROOT;
  const results = { host: vscode.version, extensions: {}, lifecycle: {
    activeStubRoot: stubRoot, configuredStubPath: ".graphql-desk/typings",
  } };
  const exists = file => fs.access(file).then(() => true, () => false);
  try {
    for (const [id, version] of [["ms-python.python", "2026.10.0"], ["ms-python.vscode-pylance", "2026.4.1"]]) {
      const extension = vscode.extensions.getExtension(id);
      assert.ok(extension);
      assert.equal(extension.packageJSON.version, version);
      await extension.activate();
      results.extensions[id] = version;
    }
    const consumerUri = vscode.Uri.file(path.join(workspace, "consumer.py"));
    const consumer = await vscode.workspace.openTextDocument(consumerUri);
    await vscode.window.showTextDocument(consumer, { preview: false });
    await vscode.extensions.getExtension("graphql-desk.graphql-desk-editor").activate();
    const hover = async () => {
      const items = await vscode.commands.executeCommand("vscode.executeHoverProvider", consumerUri, new vscode.Position(8, 2)) || [];
      return items.flatMap(item => item.contents.map(content => typeof content === "string" ? content : content.value)).join("\n");
    };
    await until(async () => await exists(path.join(stubRoot, "graphql_desk_generated", "default.pyi")) && /String/.test(await hover()), "first workspace publishes and Pylance observes String");

    const removedWorkspace = path.join(path.dirname(workspace), "removed-workspace");
    const removedUri = vscode.Uri.file(removedWorkspace);
    const removedSourceUri = vscode.Uri.file(path.join(removedWorkspace, "consumer.py"));
    const removedStubRoot = path.join(removedWorkspace, ".graphql-desk", "typings");
    await fs.mkdir(removedStubRoot, { recursive: true });
    await fs.writeFile(path.join(removedStubRoot, "keep.txt"), "preserve neighboring user file");
    for (const name of ["pyproject.toml", "schema.graphql", "consumer.py"]) {
      await fs.copyFile(path.join(workspace, name), path.join(removedWorkspace, name));
    }
    await fs.cp(path.join(workspace, "graphql_desk_generated"), path.join(removedWorkspace, "graphql_desk_generated"), { recursive: true });
    assert.ok(vscode.workspace.getWorkspaceFolder(removedUri));
    const removedDocument = await vscode.workspace.openTextDocument(removedSourceUri);
    const removedEditor = await vscode.window.showTextDocument(removedDocument, { preview: false });
    await until(() => exists(path.join(removedStubRoot, "graphql_desk_generated", "default.pyi")), "second workspace publishes managed stubs");
    assert.equal(await removedEditor.edit(edit => edit.insert(new vscode.Position(0, 0), "# unsaved user edit\n")), true);
    const removedBuffer = { text: removedDocument.getText(), version: removedDocument.version };
    const removedDisk = await fs.readFile(removedSourceUri.fsPath, "utf8");
    const removedIndex = vscode.workspace.getWorkspaceFolder(removedSourceUri).index;
    await updateFolders(removedIndex, 1);
    await until(async () => !vscode.workspace.getWorkspaceFolder(removedSourceUri) &&
      !await exists(path.join(removedStubRoot, "graphql_desk_generated")), "removed workspace retires verified owned stubs");
    assert.equal(removedDocument.getText(), removedBuffer.text);
    assert.equal(removedDocument.version, removedBuffer.version);
    assert.equal(removedDocument.isDirty, true);
    assert.equal(await fs.readFile(removedSourceUri.fsPath, "utf8"), removedDisk);
    assert.equal(await fs.readFile(path.join(removedStubRoot, "keep.txt"), "utf8"), "preserve neighboring user file");
    await vscode.window.showTextDocument(consumer, { preview: false });
    await until(async () => {
      results.lifecycle.remainingWorkspaceHover = await hover();
      return /String/.test(results.lifecycle.remainingWorkspaceHover) &&
        await exists(path.join(stubRoot, "graphql_desk_generated", "default.pyi"));
    }, "remaining workspace retains live typing");
    results.lifecycle.removedWorkspaceCleanupPreservesDirtySource = true;
    results.outcome = "managed-workspace-removal-observed";
  } catch (error) {
    results.error = String(error.stack || error);
    results.outcome = "managed-workspace-removal-failed";
    throw error;
  } finally {
    require("node:fs").writeFileSync(process.env.GRAPHQL_DESK_RESULT_PATH, JSON.stringify(results, null, 2));
    console.log(`GRAPHQL_DESK_WORKSPACE_REMOVAL ${JSON.stringify(results)}`);
  }
};
