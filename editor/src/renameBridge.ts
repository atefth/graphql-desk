import {
  RequestType,
  type CancellationToken,
  type PrepareRenameParams,
  type Range,
  type RenameParams,
  type TextDocumentIdentifier,
  type TextEdit,
} from "vscode-languageserver/node";
import type { Position, TextDocument } from "vscode";

type Span = { start: number; end: number };
type PreparedRename = { range: Range; placeholder: string };
type RenameEdit = {
  module: { uri: string; version: number };
  range: Span;
  positionRange: Range;
  sourceText: string;
  replacement: string;
};
type PrepareSource = PreparedRename | { range: Span; positionRange: Range; placeholder: string };
type RenameEdits = readonly RenameEdit[] | null;
export type RenameLanguage = {
  prepareRename(document: TextDocument, position: Position, token?: CancellationToken):
    PrepareSource | null | Promise<PrepareSource | null>;
  rename(document: TextDocument, position: Position, newName: string, token?: CancellationToken):
    RenameEdits | Promise<RenameEdits>;
  extractFragment(document: TextDocument, selection: Span, token?: CancellationToken):
    RenameEdits | Promise<RenameEdits>;
};
export type RenameBridgeOptions = {
  beforeReturn?: () => void | Promise<void>;
  isRenameDocument?: (document: TextDocument) => boolean;
  isTrusted?: () => boolean;
};
export type ExtractParams = { textDocument: TextDocumentIdentifier; version: number; selection: Span };
export type RenameTarget = { uri: string; version: number; text: string; edits: TextEdit[] };
export type RenamePlan = { targets: RenameTarget[] };

export const PREPARE_METHOD = "graphqlDesk/prepareFragmentRename";
export const PLAN_METHOD = "graphqlDesk/fragmentRenamePlan";
export const SYNC_METHOD = "graphqlDesk/confirmRenameDocumentSync";
const EXTRACT_METHOD = "graphqlDesk/extractFragment";
const EXTRACT_PLAN_METHOD = "graphqlDesk/fragmentExtractPlan";
export const PREPARE_REQUEST = new RequestType<PrepareRenameParams, PreparedRename | null, void>(PREPARE_METHOD);
export const PLAN_REQUEST = new RequestType<RenameParams, RenamePlan | null, void>(PLAN_METHOD);
export const SYNC_REQUEST = new RequestType<RenameTarget[], boolean, void>(SYNC_METHOD);
export const EXTRACT_REQUEST = new RequestType<ExtractParams, boolean, void>(EXTRACT_METHOD);
export const EXTRACT_PLAN_REQUEST = new RequestType<ExtractParams, RenamePlan | null, void>(EXTRACT_PLAN_METHOD);

function cancelled(token?: CancellationToken) {
  return token?.isCancellationRequested === true;
}

export function samePlan(left: readonly RenameEdit[], right: readonly RenameEdit[]) {
  const key = (item: RenameEdit) => JSON.stringify([item.module.uri, item.module.version, item.range, item.replacement]);
  return left.length === right.length && left.map(key).sort().join("\n") === right.map(key).sort().join("\n");
}

/**
 * Builds a rename plan only after every affected source is open in VS Code and
 * the analysis service has reproduced the same plan against current snapshots.
 * The LSP server separately confirms its synchronized copy before applyEdit.
 */
export function createRenameBridge(
  vscode: Pick<typeof import("vscode"), "workspace" | "Uri" | "Position">,
  language: RenameLanguage,
  {
    beforeReturn = async () => {},
    isRenameDocument = document => document.languageId === "python" && document.uri.scheme === "file",
    isTrusted = () => vscode.workspace.isTrusted,
  }: RenameBridgeOptions = {},
) {
  const snapshot = (document: TextDocument) => ({ version: document.version, text: document.getText() });
  async function open(uri: string) {
    const document = await vscode.workspace.openTextDocument(vscode.Uri.parse(uri));
    if (document.uri.toString() !== uri || !isRenameDocument(document) || document.isClosed) return null;
    return document;
  }

  return {
    async prepare(params: PrepareRenameParams, token?: CancellationToken): Promise<PreparedRename | null> {
      if (cancelled(token) || !isTrusted()) return null;
      const document = await open(params.textDocument.uri);
      if (!document || cancelled(token)) return null;
      const position = new vscode.Position(params.position.line, params.position.character);
      const item = await language.prepareRename(document, position, token);
      if (!item || cancelled(token) || document.isClosed) return null;
      const range = "positionRange" in item ? item.positionRange : item.range;
      if (![range?.start, range?.end].every(point =>
        point && Number.isInteger(point.line) && point.line >= 0 &&
        Number.isInteger(point.character) && point.character >= 0)) return null;
      return {
        range: {
          start: { line: range.start.line, character: range.start.character },
          end: { line: range.end.line, character: range.end.character },
        },
        placeholder: item.placeholder,
      };
    },

    async plan(params: RenameParams | ExtractParams, token?: CancellationToken, extraction = false): Promise<RenamePlan | null> {
      if (cancelled(token) || !isTrusted() || typeof params?.textDocument?.uri !== "string" ||
          (!extraction && typeof (params as RenameParams).newName !== "string")) return null;
      const origin = await open(params.textDocument.uri);
      if (!origin || cancelled(token)) return null;
      let calculate: () => RenameEdits | Promise<RenameEdits>;
      if (extraction) {
        const { version, selection } = params as ExtractParams;
        if (!Number.isInteger(version) || origin.version !== version ||
            !Number.isInteger(selection?.start) || !Number.isInteger(selection?.end)) return null;
        calculate = () => language.extractFragment(origin, selection, token);
      } else {
        const rename = params as RenameParams;
        const position = new vscode.Position(rename.position.line, rename.position.character);
        calculate = () => language.rename(origin, position, rename.newName, token);
      }
      const originSnapshot = snapshot(origin);
      const initial = await calculate();
      if (!initial?.length || cancelled(token) || origin.version !== originSnapshot.version ||
          origin.getText() !== originSnapshot.text) return null;

      const documents = new Map<string, { document: TextDocument; version: number; text: string }>();
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

      const targets: RenameTarget[] = [];
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
    async extract(params: ExtractParams, token?: CancellationToken) { return this.plan(params, token, true); },
  };
}
