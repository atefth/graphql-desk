"use strict";

const { createConnection, ProposedFeatures, TextDocuments } = require("vscode-languageserver/node");
const { TextDocument } = require("vscode-languageserver-textdocument");
const { PREPARE_REQUEST, PLAN_REQUEST, SYNC_REQUEST, EXTRACT_REQUEST, EXTRACT_PLAN_REQUEST } = require("./renameBridge");

const connection = createConnection(ProposedFeatures.all);
const documents = new TextDocuments(TextDocument);
documents.listen(connection);

function currentTarget(target) {
  const document = documents.get(target.uri);
  return document && document.version === target.version && document.getText() === target.text
    ? document : null;
}

function validPlan(plan) {
  return plan && Array.isArray(plan.targets) && plan.targets.length > 0 &&
    plan.targets.every(target => typeof target?.uri === "string" && Number.isInteger(target.version) &&
      typeof target.text === "string" && Array.isArray(target.edits) && target.edits.length > 0 &&
      new Set(plan.targets.map(item => item.uri)).size === plan.targets.length);
}

connection.onInitialize(() => {
  return {
    capabilities: {
      textDocumentSync: 2,
      renameProvider: { prepareProvider: true },
    },
  };
});

connection.onRequest(SYNC_REQUEST, targets => {
  return Array.isArray(targets) && targets.length > 0 &&
    targets.every(target => typeof target?.uri === "string" && Number.isInteger(target.version) &&
      typeof target.text === "string" && currentTarget(target));
});

connection.onPrepareRename(async (params, token) => {
  if (token.isCancellationRequested) return null;
  try {
    const result = await connection.sendRequest(PREPARE_REQUEST, params, token);
    return token.isCancellationRequested ? null : result ?? null;
  } catch { return null; }
});

async function applyPlan(plan, label, token) {
  if (token.isCancellationRequested || !validPlan(plan) || plan.targets.some(target => !currentTarget(target))) return false;
  // VS Code verifies each version atomically while applying the workspace edit.
  const result = await connection.workspace.applyEdit({
    label,
    documentChanges: plan.targets.map(target => ({
      textDocument: { uri: target.uri, version: target.version },
      edits: target.edits,
    })),
  });
  return result.applied;
}

connection.onRenameRequest(async (params, token) => {
  if (token.isCancellationRequested) return null;
  try {
    const plan = await connection.sendRequest(PLAN_REQUEST, params, token);
    await applyPlan(plan, "Rename GraphQL fragment", token);
    return null;
  } catch { return null; }
});

connection.onRequest(EXTRACT_REQUEST, async (params, token) => {
  if (token.isCancellationRequested) return false;
  try {
    const plan = await connection.sendRequest(EXTRACT_PLAN_REQUEST, params, token);
    return await applyPlan(plan, "Extract GraphQL fragment", token);
  } catch { return false; }
});

connection.listen();
