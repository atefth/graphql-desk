"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs/promises");
const path = require("node:path");
const vscode = require("vscode");
const { createOwnedAnalysisStub } = require("../src/ownedAnalysisStub");

async function until(check, label, milliseconds = 20000) {
  const deadline = Date.now() + milliseconds;
  while (Date.now() < deadline) {
    if (await check()) return true;
    await new Promise(resolve => setTimeout(resolve, 100));
  }
  console.log(`NOT_OBSERVED: ${label}`);
  return false;
}

exports.run = async () => {
  const results = { host: vscode.version, disk: {}, pylance: {}, lifecycle: {} };
  const workspace = process.env.GRAPHQL_DESK_TEST_WORKSPACE;
  const root = process.env.GRAPHQL_DESK_STUB_ROOT;
  const savedUri = vscode.Uri.file(path.join(workspace, "generated.pyi"));
  const consumerUri = vscode.Uri.file(path.join(workspace, "consumer.py"));
  const names = ["generated.pyi", "generated.py", "consumer.py", "user.py", ".vscode/settings.json"];
  const snapshot = new Map(await Promise.all(names.map(async name => [name, await fs.readFile(path.join(workspace, name))])));
  async function unchangedProject() {
    for (const [name, bytes] of snapshot) assert.deepEqual(await fs.readFile(path.join(workspace, name)), bytes, name);
  }
  const original = await fs.readFile(savedUri.fsPath, "utf8");
  const nullable = original.replace("selected: str", "selected: str | None");
  let handle;
  try {
    assert.equal(vscode.workspace.isTrusted, true);
    handle = createOwnedAnalysisStub(vscode, workspace, root, "generated", original);
    const initialBytes = await fs.readFile(handle.uri.fsPath);
    results.disk.seededBeforeActivation = true;
    assert.throws(() => createOwnedAnalysisStub(vscode, workspace, root, "generated", original), /EEXIST/);
    results.lifecycle.restartLeftoverRefused = true;
    for (const id of ["ms-python.python", "ms-python.vscode-pylance"]) {
      const extension = vscode.extensions.getExtension(id);
      assert.ok(extension);
      await extension.activate();
      results.pylance[id] = extension.packageJSON.version;
      assert.equal(extension.packageJSON.version, id === "ms-python.python" ? "2026.10.0" : "2026.4.1");
    }
    const consumer = await vscode.workspace.openTextDocument(consumerUri);
    await vscode.window.showTextDocument(consumer, { preview: false });
    const hover = async () => {
      const items = await vscode.commands.executeCommand("vscode.executeHoverProvider", consumerUri, new vscode.Position(2, 2)) || [];
      return items.flatMap(item => item.contents.map(c => typeof c === "string" ? c : c.value)).join("\n");
    };
    const errors = () => vscode.languages.getDiagnostics(consumerUri).filter(d => d.range.start.line === 3 && d.severity === vscode.DiagnosticSeverity.Error);
    assert.ok(await until(async () => (await hover()).includes("selected: str") && !errors().length, "saved baseline", 30000));
    results.pylance.savedBaseline = await hover();
    const definitions = await vscode.commands.executeCommand("vscode.executeDefinitionProvider", consumerUri, new vscode.Position(0, 9)) || [];
    results.pylance.baselineDefinitions = definitions.map(d => (d.targetUri || d.uri).toString());
    await unchangedProject();
    let current = 1;
    assert.equal(await handle.publish(nullable, () => current === 0), false);
    assert.deepEqual(await fs.readFile(handle.uri.fsPath), initialBytes);
    results.lifecycle.staleRejectedBeforeWrite = true;
    assert.equal(await handle.publish(nullable, () => current === 1), true);
    assert.ok((await fs.readFile(handle.uri.fsPath, "utf8")).endsWith(nullable));
    results.disk.duringPublicationWritesOwnedAnalysisFile = true;
    await unchangedProject();
    results.pylance.diskOnlyUpdate = await until(async () => (await hover()).includes("None") && errors().length > 0, "disk-only shadow import nullable update", 12000);
    if (!results.pylance.diskOnlyUpdate) {
      await vscode.workspace.openTextDocument(handle.uri);
      results.pylance.cleanOpenUpdate = await until(async () => (await hover()).includes("None") && errors().length > 0, "clean opened shadow nullable update");
    }
    results.pylance.nullableHover = await hover();
    results.pylance.nullableDiagnostics = errors().map(d => d.message);
    results.pylance.consumerUpdated = !!(results.pylance.diskOnlyUpdate || results.pylance.cleanOpenUpdate);
    await vscode.workspace.saveAll(false);
    await unchangedProject();
    assert.ok((await fs.readFile(handle.uri.fsPath, "utf8")).endsWith(nullable));
    results.lifecycle.saveAllDoesNotAlterProject = true;
    const userUri = vscode.Uri.file(path.join(workspace, "user.py"));
    const user = await vscode.workspace.openTextDocument(userUri);
    const userEditor = await vscode.window.showTextDocument(user, { preview: false });
    assert.equal(await userEditor.edit(edit => edit.insert(new vscode.Position(0, 0), "# unsaved user edit\n")), true);
    const userState = { text: user.getText(), version: user.version };
    function unchangedUser() {
      assert.equal(user.getText(), userState.text);
      assert.equal(user.version, userState.version);
      assert.equal(user.isDirty, true);
    }
    const shadow = vscode.workspace.textDocuments.find(d => d.uri.toString() === handle.uri.toString());
    if (shadow) assert.equal(shadow.isDirty, false);
    results.lifecycle.shadowNotDirty = !shadow || !shadow.isDirty;
    current = 2;
    assert.equal(await handle.publish(original, () => current === 1), false);
    assert.ok((await fs.readFile(handle.uri.fsPath, "utf8")).endsWith(nullable));
    assert.equal(await handle.publish(original, () => current === 2), true);
    results.pylance.repaired = await until(async () => !(await hover()).includes("None") && (await hover()).includes("selected: str") && !errors().length, "repair update");
    await unchangedProject();
    unchangedUser();
    current = 3;
    assert.equal(await handle.publish(nullable, () => current === 3), true);
    results.pylance.beforeCleanupNullable = await until(async () => (await hover()).includes("None") && errors().length > 0, "nullable before cleanup");
    results.pylance.beforeCleanupHover = await hover();
    results.pylance.consumerStillOpen = !consumer.isClosed;
    unchangedUser();
    await handle.cleanup();
    assert.deepEqual(await fs.readdir(root), ["preexisting.txt"]);
    assert.equal(await fs.readFile(path.join(root, "preexisting.txt"), "utf8"), "preserve");
    results.disk.ownedArtifactsRemoved = true;
    results.pylance.cleanupRestored = await until(async () => (await hover()).includes("selected: str") && !(await hover()).includes("None") && !errors().length, "saved import after cleanup");
    await unchangedProject();
    unchangedUser();
    results.disk.originalProjectUnchangedThroughout = true;
    results.lifecycle.userDirtyBufferPreserved = true;
    const edited = createOwnedAnalysisStub(vscode, workspace, root, "foreign_edits", original);
    const editedDocument = await vscode.workspace.openTextDocument(edited.uri);
    const editedEditor = await vscode.window.showTextDocument(editedDocument, { preview: false });
    assert.equal(await editedEditor.edit(edit => edit.insert(new vscode.Position(0, 0), "# user edit\n")), true);
    const editedState = { text: editedDocument.getText(), version: editedDocument.version };
    const editedDisk = await fs.readFile(edited.uri.fsPath);
    await assert.rejects(edited.publish(nullable, () => true), /user edits/);
    await assert.rejects(edited.cleanup(), /user edits/);
    assert.equal(editedDocument.getText(), editedState.text);
    assert.equal(editedDocument.version, editedState.version);
    assert.equal(editedDocument.isDirty, true);
    assert.deepEqual(await fs.readFile(edited.uri.fsPath), editedDisk);
    results.lifecycle.dirtyOwnedBufferPreserved = true;
    const associatedUri = vscode.Uri.file(path.join(root, "associated_edits", "__init__.pyi"));
    const associatedDocument = await vscode.workspace.openTextDocument(associatedUri.with({ scheme: "untitled" }));
    const associatedEditor = await vscode.window.showTextDocument(associatedDocument, { preview: false });
    assert.equal(await associatedEditor.edit(edit => edit.insert(new vscode.Position(0, 0), "# associated user edit\n")), true);
    const associatedState = { text: associatedDocument.getText(), version: associatedDocument.version };
    assert.throws(() => createOwnedAnalysisStub(vscode, workspace, root, "associated_edits", original), /user edits/);
    assert.equal(associatedDocument.getText(), associatedState.text);
    assert.equal(associatedDocument.version, associatedState.version);
    assert.equal(associatedDocument.isDirty, true);
    await assert.rejects(fs.stat(path.dirname(associatedUri.fsPath)), { code: "ENOENT" });
    results.lifecycle.associatedUntitledPreserved = true;
    results.outcome = results.pylance.consumerUpdated && results.pylance.repaired && results.pylance.beforeCleanupNullable && results.pylance.cleanupRestored ? "bounded-owned-analysis-observed" : "blocked-no-complete-consumer-refresh";
  } catch (error) {
    results.error = String(error.stack || error);
    results.outcome = "harness-or-ownership-failure";
    throw error;
  } finally {
    await fs.writeFile(process.env.GRAPHQL_DESK_RESULT_PATH, JSON.stringify(results, null, 2));
    console.log(`GRAPHQL_DESK_OWNED_ANALYSIS ${JSON.stringify(results)}`);
  }
};
