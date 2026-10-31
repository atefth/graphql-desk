"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs/promises");
const path = require("node:path");
const vscode = require("vscode");
const { CancellationTokenSource, Position, Range, Uri } = vscode;
const { PrepareRenameRequest, RenameRequest } = require("vscode-languageclient/node");
const { createLanguageFeatures } = require("../src/languageFeatures");
const { bundle, options } = require("./featureFixtures.cjs");
const { createRenameLanguageClient } = require("../src/renameLanguageClient");
const { EXTRACT_REQUEST } = require("../src/renameBridge");

function featureService() {
  function occurrences(text) {
    const result = [];
    for (const match of text.matchAll(/fragment ([_A-Za-z][_0-9A-Za-z]*)|\.\.\.([_A-Za-z][_0-9A-Za-z]*)/g)) {
      const name = match[1] ?? match[2];
      if (name !== "Card") continue;
      const start = match.index + match[0].lastIndexOf(name);
      result.push({ name, start, end: start + name.length });
    }
    return result;
  }
  function at(document, position) {
    const offset = document.offsetAt(position);
    return occurrences(document.getText()).find(item => offset >= item.start && offset <= item.end);
  }
  function positionAt(text, offset) {
    const lines = text.slice(0, offset).split(/\r\n|\r|\n/);
    return { line: lines.length - 1, character: lines.at(-1).length };
  }
  return {
    async prepareRename(document, position) {
      const item = at(document, position);
      return item ? { range: new Range(document.positionAt(item.start), document.positionAt(item.end)), placeholder: item.name } : null;
    },
    async rename(document, position, newName, token) {
      if (token?.isCancellationRequested || !/^[_A-Za-z][_0-9A-Za-z]*$/.test(newName)) return null;
      if (!at(document, position)) return null;
      const result = [];
      const folder = path.dirname(document.uri.fsPath);
      for (const file of [path.join(folder, "main.txt"), path.join(folder, "fragments.txt")]) {
        let sourceText;
        try { sourceText = await fs.readFile(file, "utf8"); } catch { return null; }
        const target = vscode.workspace.textDocuments.find(item => item.uri.fsPath === file);
        for (const item of occurrences(sourceText)) result.push({
          module: { uri: Uri.file(file).toString(), version: target?.version ?? 1 },
          sourceText, range: { start: item.start, end: item.end },
          positionRange: { start: positionAt(sourceText, item.start), end: positionAt(sourceText, item.end) },
          replacement: newName,
        });
      }
      return result.length ? result : null;
    },
  };
}

let root;
async function writePair(directory, definition = "fragment Card on User { id }\n", main = "query Read { user { ...Card } }\n",
  { openFragment = true } = {}) {
  const folder = path.join(root, directory);
  await fs.mkdir(folder, { recursive: true });
  await fs.writeFile(path.join(folder, "fragments.txt"), definition, { flag: "wx" });
  await fs.writeFile(path.join(folder, "main.txt"), main, { flag: "wx" });
  const files = {};
  const uris = {};
  for (const name of ["main.txt", "fragments.txt"]) uris[name] = Uri.file(path.join(folder, name));
  for (const name of openFragment ? ["main.txt", "fragments.txt"] : ["main.txt"]) {
    files[name] = await vscode.workspace.openTextDocument(uris[name]);
  }
  return { folder, files, uris };
}

async function pythonDocuments(directory) {
  const folder = path.join(root, directory);
  await fs.mkdir(folder, { recursive: true });
  const sources = {
    "fragments.py": "from graphql_desk import gql\ncard = gql('fragment Card on User { id }')\n",
    "main.py": "from graphql_desk import gql\nfrom fragments import card\nquery = gql('query Read { user { ...Card } }', [card])\n",
  };
  const documents = {};
  for (const [name, source] of Object.entries(sources)) {
    const file = path.join(folder, name);
    await fs.writeFile(file, source, { flag: "wx" });
    let document = await vscode.workspace.openTextDocument(Uri.file(file));
    if (document.languageId !== "python") document = await vscode.languages.setTextDocumentLanguage(document, "python");
    documents[name] = document;
  }
  const value = bundle(sources["main.py"], {
    uri: documents["main.py"].uri.toString(), version: documents["main.py"].version,
    modules: [{ project: "p", module: "fragments", uri: documents["fragments.py"].uri.toString(),
      version: documents["fragments.py"].version, source: sources["fragments.py"], is_package: false }],
  });
  return { documents, service: createLanguageFeatures(options(value)) };
}

async function pythonExtractionDocument(directory,
  source = "from graphql_desk import gql\nquery = gql('query Read { user { id name } }')\n") {
  const folder = path.join(root, directory);
  await fs.mkdir(folder, { recursive: true });
  const file = path.join(folder, "main.py");
  await fs.writeFile(file, source, { flag: "wx" });
  let document = await vscode.workspace.openTextDocument(Uri.file(file));
  if (document.languageId !== "python") document = await vscode.languages.setTextDocumentLanguage(document, "python");
  const value = bundle(source, { uri: document.uri.toString(), version: document.version });
  const service = createLanguageFeatures(options(value));
  return { document, source, language: {
    extractFragment: (item, selection, token) => service.extractFragment(
      { uri: item.uri.toString(), version: item.version, text: item.getText() }, selection, token),
  } };
}

async function requestExtract(client, document, token) {
  const start = document.getText().indexOf("id name");
  assert.notEqual(start, -1);
  return client.sendRequest(EXTRACT_REQUEST, {
    textDocument: { uri: document.uri.toString() }, version: document.version,
    selection: { start, end: start + "id name".length },
  }, token);
}

function positionOf(document, text) {
  const offset = document.getText().indexOf(text);
  assert.notEqual(offset, -1);
  return document.positionAt(offset);
}

async function requestRename(client, document, name, token) {
  return client.sendRequest(RenameRequest.type, {
    textDocument: { uri: document.uri.toString() },
    position: { line: positionOf(document, "Card").line, character: positionOf(document, "Card").character },
    newName: name,
  }, token);
}

async function run() {
  root = process.env.GRAPHQL_DESK_TEST_WORKSPACE;
  const resultPath = process.env.GRAPHQL_DESK_RESULT_PATH;
  assert.ok(root && path.isAbsolute(root));
  assert.ok(resultPath && path.isAbsolute(resultPath));
  const observed = { outcome: "fragment-rename-lsp-unverified", step: "setup", twoFileSuccess: false,
    closedTargetOpened: false, staleTargetRejectedWithoutPartialEdit: false, cancellation: false,
    extractRoundtripUndo: false, extractFunctionRoundtripUndo: false,
    extractQualifiedRoundtripUndo: false, extractStaleRejected: false,
    extractCancellation: false, shutdown: false };
  const clients = [];
  try {
    const basic = await writePair("success");
    observed.step = "two-file-success";
    const service = featureService();
    const client = createRenameLanguageClient(vscode, service, {
      documentSelector: [{ language: "plaintext", scheme: "file" }],
      isRenameDocument: document => document.uri.scheme === "file" && document.languageId === "plaintext",
      isTrusted: () => true,
    });
    clients.push(client);
    await client.start();

    await requestRename(client.client, basic.files["main.txt"], "UserCard");
    assert.equal(basic.files["main.txt"].getText(), "query Read { user { ...UserCard } }\n");
    assert.equal(basic.files["fragments.txt"].getText(), "fragment UserCard on User { id }\n");
    observed.twoFileSuccess = true;
    await client.stop();

    observed.step = "real-python-analysis";
    const actual = await pythonDocuments("actual-python");
    const snapshot = document => ({ uri: document.uri.toString(), version: document.version, text: document.getText() });
    const traced = {
      prepareRename: (document, position, token) => actual.service.prepareRename(snapshot(document), document.offsetAt(position), token),
      rename: (document, position, name, token) => actual.service.rename(snapshot(document), document.offsetAt(position), name, token),
    };
    const actualClient = createRenameLanguageClient(vscode, traced, { isTrusted: () => true });
    clients.push(actualClient);
    await actualClient.start();
    await requestRename(actualClient.client, actual.documents["main.py"], "UserCard");
    assert.match(actual.documents["main.py"].getText(), /\.\.\.UserCard/);
    assert.match(actual.documents["fragments.py"].getText(), /fragment UserCard/);
    await actualClient.stop();

    const definitionPair = await writePair("definition");
    observed.step = "definition-origin";
    const definitionClient = createRenameLanguageClient(vscode, service, {
      documentSelector: [{ language: "plaintext", scheme: "file" }],
      isRenameDocument: document => document.uri.scheme === "file" && document.languageId === "plaintext",
      isTrusted: () => true,
    });
    clients.push(definitionClient);
    await definitionClient.start();
    const definitionPosition = positionOf(definitionPair.files["fragments.txt"], "Card");
    const prepared = await definitionClient.client.sendRequest(PrepareRenameRequest.type, {
      textDocument: { uri: definitionPair.files["fragments.txt"].uri.toString() },
      position: { line: definitionPosition.line, character: definitionPosition.character },
    });
    assert.equal(prepared.placeholder, "Card");
    await requestRename(definitionClient.client, definitionPair.files["fragments.txt"], "AccountCard");
    assert.equal(definitionPair.files["main.txt"].getText(), "query Read { user { ...AccountCard } }\n");
    assert.equal(definitionPair.files["fragments.txt"].getText(), "fragment AccountCard on User { id }\n");
    await definitionClient.stop();

    // Closing the target forces the extension-side bridge to open it and wait
    // for the LSP document synchronization before planning the versioned edit.
    const closed = await writePair("closed", undefined, undefined, { openFragment: false });
    observed.step = "closed-target";
    const closedClient = createRenameLanguageClient(vscode, service, {
      documentSelector: [{ language: "plaintext", scheme: "file" }],
      isRenameDocument: document => document.uri.scheme === "file" && document.languageId === "plaintext",
      isTrusted: () => true,
    });
    clients.push(closedClient);
    await closedClient.start();
    assert.ok(!vscode.workspace.textDocuments.some(item => item.uri.toString() === closed.uris["fragments.txt"].toString()));
    await requestRename(closedClient.client, closed.files["main.txt"], "UserCard");
    assert.equal(closed.files["main.txt"].getText(), "query Read { user { ...UserCard } }\n");
    const reopened = await vscode.workspace.openTextDocument(closed.uris["fragments.txt"]);
    assert.equal(reopened.getText(), "fragment UserCard on User { id }\n");
    observed.closedTargetOpened = true;
    await closedClient.stop();

    let rejectedStaleEdit = false;
    const stale = await writePair("stale");
    observed.step = "stale-version";
    const staleClient = createRenameLanguageClient(vscode, service, {
      documentSelector: [{ language: "plaintext", scheme: "file" }],
      isRenameDocument: document => document.uri.scheme === "file" && document.languageId === "plaintext",
      isTrusted: () => true,
      middleware: { workspace: { async handleApplyEdit(params, next) {
        const targetUri = stale.files["fragments.txt"].uri.toString();
        if (!params.edit.documentChanges?.some(change => change.textDocument?.uri === targetUri)) return next(params);
        const external = new vscode.WorkspaceEdit();
        external.replace(stale.files["fragments.txt"].uri, new Range(new Position(0, 9), new Position(0, 13)), "Other");
        assert.ok(await vscode.workspace.applyEdit(external));
        const result = await next(params);
        rejectedStaleEdit = result?.applied === false;
        return result;
      } } },
    });
    clients.push(staleClient);
    await staleClient.start();
    await requestRename(staleClient.client, stale.files["main.txt"], "UserCard");
    assert.equal(rejectedStaleEdit, true, "VS Code's language client must reject the versioned apply after a post-plan edit");
    assert.equal(stale.files["main.txt"].getText(), "query Read { user { ...Card } }\n");
    assert.equal(stale.files["fragments.txt"].getText(), "fragment Other on User { id }\n");
    observed.staleTargetRejectedWithoutPartialEdit = true;
    await staleClient.stop();

    let cancelledCapturedResolve;
    let cancelledGateResolve;
    const cancelledCaptured = new Promise(resolve => { cancelledCapturedResolve = resolve; });
    const cancelledGate = new Promise(resolve => { cancelledGateResolve = resolve; });
    const cancelClient = createRenameLanguageClient(vscode, service, {
      documentSelector: [{ language: "plaintext", scheme: "file" }],
      isRenameDocument: document => document.uri.scheme === "file" && document.languageId === "plaintext",
      isTrusted: () => true,
      beforeReturn: async () => { cancelledCapturedResolve(); await cancelledGate; },
    });
    clients.push(cancelClient);
    await cancelClient.start();
    const cancellation = await writePair("cancel");
    observed.step = "cancellation";
    const sourceBeforeCancel = cancellation.files["main.txt"].getText();
    const targetBeforeCancel = cancellation.files["fragments.txt"].getText();
    const cancellationSource = new CancellationTokenSource();
    const cancelledRequest = requestRename(cancelClient.client, cancellation.files["main.txt"], "UserCard", cancellationSource.token);
    await cancelledCaptured;
    cancellationSource.cancel();
    cancelledGateResolve();
    await cancelledRequest;
    assert.equal(cancellation.files["main.txt"].getText(), sourceBeforeCancel);
    assert.equal(cancellation.files["fragments.txt"].getText(), targetBeforeCancel);
    observed.cancellation = true;
    await cancelClient.stop();

    observed.step = "extract-roundtrip-undo";
    const extraction = await pythonExtractionDocument("extract");
    const extractClient = createRenameLanguageClient(vscode, extraction.language, { isTrusted: () => true });
    clients.push(extractClient);
    await extractClient.start();
    await vscode.window.showTextDocument(extraction.document);
    assert.equal(await requestExtract(extractClient.client, extraction.document), true);
    assert.match(extraction.document.getText(), /user_fields = gql\(source="fragment UserFields on User/);
    assert.match(extraction.document.getText(), /\.\.\.UserFields/);
    await vscode.commands.executeCommand("undo");
    assert.equal(extraction.document.getText(), extraction.source);
    observed.extractRoundtripUndo = true;
    await extractClient.stop();

    observed.step = "extract-function-roundtrip-undo";
    const localSource = "# 😀\r\nfrom graphql_desk import gql\r\ndef read():\r\n    # local query\r\n    query: object = gql('query Read { user { id name } }')\r\n";
    const localExtraction = await pythonExtractionDocument("extract-function", localSource);
    const localClient = createRenameLanguageClient(vscode, localExtraction.language, { isTrusted: () => true });
    clients.push(localClient);
    await localClient.start();
    await vscode.window.showTextDocument(localExtraction.document);
    assert.equal(await requestExtract(localClient.client, localExtraction.document), true);
    assert.match(localExtraction.document.getText(), /fragments=\[gql\(source="fragment UserFields on User/);
    assert.match(localExtraction.document.getText(), /\r\n    # local query\r\n    query: object = gql\(.*fragments=\[gql\(source=/);
    await vscode.commands.executeCommand("undo");
    assert.equal(localExtraction.document.getText(), localSource);
    observed.extractFunctionRoundtripUndo = true;
    await localClient.stop();

    observed.step = "extract-module-qualified-roundtrip-undo";
    const qualifiedSource = "# 😀\r\nimport graphql_desk as desk\r\nquery = desk.gql('query Read { user { id name } }')\r\n";
    const qualifiedExtraction = await pythonExtractionDocument("extract-module-qualified", qualifiedSource);
    const qualifiedClient = createRenameLanguageClient(vscode, qualifiedExtraction.language, { isTrusted: () => true });
    clients.push(qualifiedClient);
    await qualifiedClient.start();
    await vscode.window.showTextDocument(qualifiedExtraction.document);
    assert.equal(await requestExtract(qualifiedClient.client, qualifiedExtraction.document), true);
    assert.match(qualifiedExtraction.document.getText(), /user_fields = desk\.gql\(source="fragment UserFields on User/);
    assert.match(qualifiedExtraction.document.getText(), /\.\.\.UserFields/);
    const transformedSource = qualifiedExtraction.document.getText();
    const reparsed = bundle(transformedSource, {
      uri: qualifiedExtraction.document.uri.toString(),
      version: qualifiedExtraction.document.version,
    });
    assert.equal(reparsed.response.calls.length, 2);
    assert.ok(reparsed.response.calls.every(item => item.index));
    assert.deepEqual(reparsed.response.diagnostics, []);
    await vscode.commands.executeCommand("undo");
    assert.equal(qualifiedExtraction.document.getText(), qualifiedSource);
    observed.extractQualifiedRoundtripUndo = true;
    await qualifiedClient.stop();

    observed.step = "extract-stale";
    const staleExtract = await pythonExtractionDocument("extract-stale");
    let rejectedExtraction = false;
    const staleExtractClient = createRenameLanguageClient(vscode, staleExtract.language, {
      isTrusted: () => true,
      middleware: { workspace: { async handleApplyEdit(params, next) {
        const external = new vscode.WorkspaceEdit();
        const start = staleExtract.document.getText().indexOf("name");
        external.replace(staleExtract.document.uri,
          new Range(staleExtract.document.positionAt(start), staleExtract.document.positionAt(start + 4)), "id");
        assert.ok(await vscode.workspace.applyEdit(external));
        const result = await next(params);
        rejectedExtraction = result?.applied === false;
        return result;
      } } },
    });
    clients.push(staleExtractClient);
    await staleExtractClient.start();
    assert.equal(await requestExtract(staleExtractClient.client, staleExtract.document), false);
    assert.equal(rejectedExtraction, true);
    assert.equal(staleExtract.document.getText(), staleExtract.source.replace("id name", "id id"));
    observed.extractStaleRejected = true;
    await staleExtractClient.stop();

    observed.step = "extract-cancellation";
    const cancelledExtract = await pythonExtractionDocument("extract-cancel");
    let capturedExtract, releaseExtract;
    const capturedExtractPromise = new Promise(resolve => { capturedExtract = resolve; });
    const extractGate = new Promise(resolve => { releaseExtract = resolve; });
    const cancelledExtractClient = createRenameLanguageClient(vscode, cancelledExtract.language, {
      isTrusted: () => true, beforeReturn: async () => { capturedExtract(); await extractGate; },
    });
    clients.push(cancelledExtractClient);
    await cancelledExtractClient.start();
    const extractToken = new CancellationTokenSource();
    const pendingExtract = requestExtract(cancelledExtractClient.client, cancelledExtract.document, extractToken.token);
    await capturedExtractPromise;
    extractToken.cancel();
    releaseExtract();
    await pendingExtract.catch(() => false);
    assert.equal(cancelledExtract.document.getText(), cancelledExtract.source);
    observed.extractCancellation = true;
    await cancelledExtractClient.stop();

    assert.ok(clients.every(item => !item.client.needsStop()));
    observed.step = "shutdown";
    observed.shutdown = true;
    observed.outcome = "fragment-rename-lsp-observed";
    await fs.writeFile(resultPath, JSON.stringify(observed), "utf8");
    console.log(`Fragment rename LSP evidence: ${resultPath}`);
  } catch (error) {
    observed.error = error?.stack ?? String(error);
    console.error(`Fragment rename test failed at ${observed.step}: ${observed.error}`);
    throw error;
  } finally {
    for (const item of clients) if (item.client.needsStop()) await item.stop();
    await fs.writeFile(resultPath, JSON.stringify(observed), "utf8").catch(() => {});
  }
}

module.exports = { run };
