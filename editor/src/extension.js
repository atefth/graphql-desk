"use strict";

const { createAnalysisController } = require("./analysisController");
const { registerLanguageFeatures } = require("./languageFeatures");
const { registerHighlighting } = require("./highlighting");

function activate(context) {
  const vscode = require("vscode");
  let language;
  let highlighting;
  let timer;
  let disposed = false;
  const refreshVisible = () => {
    clearTimeout(timer);
    // Drop published semantic diagnostics immediately; rebuild after the edit settles.
    for (const document of vscode.workspace.textDocuments) language?.close(document.uri);
    timer = setTimeout(() => {
      if (disposed) return;
      for (const editor of vscode.window.visibleTextEditors) {
        if (editor.document.languageId !== "python") continue;
        void language.refresh(editor.document);
        void highlighting.refresh(editor);
      }
    }, 100);
  };
  const status = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Right, 0);
  status.name = "GraphQL Desk";
  status.command = "graphqlDesk.refreshSchema";
  status.tooltip = "GraphQL Desk: refresh a remote schema; positive schema_refresh_interval enables trusted background polling. Check configuration and interpreter settings if unavailable.";
  const controller = createAnalysisController(vscode, { changed: refreshVisible,
    status: message => {
      if (message) { status.text = `GraphQL Desk: ${message}`; status.show(); }
      else status.hide();
    }, report: message => vscode.window.showInformationMessage(message) });
  language = registerLanguageFeatures(vscode, controller);
  highlighting = registerHighlighting(vscode, controller);
  const visible = vscode.window.onDidChangeVisibleTextEditors(refreshVisible);
  const command = vscode.commands.registerCommand("graphqlDesk.refreshSchema", () => controller.refresh());
  context.subscriptions.push({ dispose() { disposed = true; clearTimeout(timer); command.dispose(); status.dispose(); visible.dispose(); language.dispose(); highlighting.dispose(); controller.dispose(); } });
  refreshVisible();
}
module.exports = { activate };
