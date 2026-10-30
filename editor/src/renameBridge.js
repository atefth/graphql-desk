"use strict";

const { RequestType } = require("vscode-languageserver/node");

const PREPARE_METHOD = "graphqlDesk/prepareFragmentRename";
const PLAN_METHOD = "graphqlDesk/fragmentRenamePlan";
const SYNC_METHOD = "graphqlDesk/confirmRenameDocumentSync";
const EXTRACT_METHOD = "graphqlDesk/extractFragment";
const EXTRACT_PLAN_METHOD = "graphqlDesk/fragmentExtractPlan";
const PREPARE_REQUEST = new RequestType(PREPARE_METHOD);
const PLAN_REQUEST = new RequestType(PLAN_METHOD);
const SYNC_REQUEST = new RequestType(SYNC_METHOD);
const EXTRACT_REQUEST = new RequestType(EXTRACT_METHOD);
const EXTRACT_PLAN_REQUEST = new RequestType(EXTRACT_PLAN_METHOD);

function cancelled(token) {
  return token?.isCancellationRequested === true;
}

function samePlan(left, right) {
  const key = item => JSON.stringify([item.module.uri, item.module.version, item.range, item.replacement]);
  return left.length === right.length && left.map(key).sort().join("\n") === right.map(key).sort().join("\n");
}

/**
 * Builds a rename plan only after every affected source is open in VS Code and
 * the analysis service has reproduced the same plan against current snapshots.
 * The LSP server separately confirms its synchronized copy before applyEdit.
 */
function createRenameBridge(vscode, language, { beforeReturn = async () => {},
  isRenameDocument = document => document.languageId === "python" && document.uri.scheme === "file",
  isTrusted = () => vscode.workspace.isTrusted } = {}) {
  const snapshot = document => ({ uri: document.uri.toString(), version: document.version, text: document.getText() });
  async function open(uri) {
    const document = await vscode.workspace.openTextDocument(vscode.Uri.parse(uri));
    if (document.uri.toString() !== uri || !isRenameDocument(document) || document.isClosed) return null;
    return document;
  }

  return {
    async prepare(params, token) {
      if (cancelled(token) || !isTrusted()) return null;
      const document = await open(params.textDocument.uri);
      if (!document || cancelled(token)) return null;
      const position = new vscode.Position(params.position.line, params.position.character);
      const item = await language.prepareRename(document, position, token);
      if (!item || cancelled(token) || document.isClosed) return null;
      return {
        range: {
          start: { line: item.range.start.line, character: item.range.start.character },
          end: { line: item.range.end.line, character: item.range.end.character },
        },
        placeholder: item.placeholder,
      };
    },

    async plan(params, token, extraction = false) {
      if (cancelled(token) || !isTrusted() || typeof params?.textDocument?.uri !== "string" ||
          (!extraction && typeof params.newName !== "string")) return null;
      const origin = await open(params.textDocument.uri);
      if (!origin || cancelled(token)) return null;
      if (extraction && (!Number.isInteger(params.version) || origin.version !== params.version ||
          !Number.isInteger(params.selection?.start) || !Number.isInteger(params.selection?.end))) return null;
      const position = extraction ? null : new vscode.Position(params.position.line, params.position.character);
      const calculate = () => extraction ? language.extractFragment(origin, params.selection, token)
        : language.rename(origin, position, params.newName, token);
      const originSnapshot = snapshot(origin);
      const initial = await calculate();
      if (!initial?.length || cancelled(token) || origin.version !== originSnapshot.version ||
          origin.getText() !== originSnapshot.text) return null;

      const documents = new Map();
      for (const item of initial) {
        if (cancelled(token)) return null;
        const document = await open(item.module.uri);
        if (!document || document.version !== item.module.version || document.getText() !== item.sourceText) return null;
        const captured = documents.get(item.module.uri);
        if (captured && (captured.document !== document || captured.text !== item.sourceText ||
            captured.version !== item.module.version)) return null;
        documents.set(item.module.uri, { document, version: document.version, text: document.getText() });
      }

      // Re-run analysis after opening closed targets: the plan must be based on
      // the exact versions/text that will be synchronized to the LSP server.
      const fresh = await calculate();
      if (!fresh?.length || cancelled(token) || origin.version !== originSnapshot.version ||
          origin.getText() !== originSnapshot.text || !samePlan(initial, fresh)) return null;

      const targets = [];
      for (const item of fresh) {
        const captured = documents.get(item.module.uri);
        if (!captured || captured.document.isClosed || captured.document.version !== captured.version ||
            captured.document.version !== item.module.version || captured.document.getText() !== captured.text ||
            captured.text !== item.sourceText) return null;
        const target = targets.find(candidate => candidate.uri === item.module.uri);
        const edit = {
          range: {
            start: item.positionRange.start,
            end: item.positionRange.end,
          },
          newText: item.replacement,
        };
        if (target) target.edits.push(edit);
        else targets.push({ uri: item.module.uri, version: captured.version, text: captured.text, edits: [edit] });
      }
      if (targets.some(target => !target.edits.length) || cancelled(token)) return null;
      await beforeReturn();
      if (cancelled(token) || origin.isClosed || origin.version !== originSnapshot.version ||
          origin.getText() !== originSnapshot.text || targets.some(target => {
            const document = documents.get(target.uri)?.document;
            return !document || document.isClosed || document.version !== target.version || document.getText() !== target.text;
          })) return null;
      return { targets };
    },
    async extract(params, token) { return this.plan(params, token, true); },
  };
}

module.exports = { PREPARE_METHOD, PLAN_METHOD, SYNC_METHOD, PREPARE_REQUEST, PLAN_REQUEST,
  SYNC_REQUEST, EXTRACT_REQUEST, EXTRACT_PLAN_REQUEST, createRenameBridge, samePlan };
