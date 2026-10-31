"use strict";

const fs = require("node:fs");
const path = require("node:path");
const { pathToFileURL } = require("node:url");
const { createAnalysisGenerations } = require("../src/analysisGenerations");

const [workspace, root, marker] = process.argv.slice(2);
if (![workspace, root, marker].every(value => typeof value === "string" && path.isAbsolute(value))) {
  throw new Error("Expected absolute workspace, managed root, and marker paths");
}

const originalLink = fs.linkSync;
fs.linkSync = (temporary, target) => {
  const result = originalLink(temporary, target);
  if (target.startsWith(`${path.join(root, "generations")}${path.sep}`) && target.endsWith(`${path.sep}default.pyi`)) {
    fs.writeFileSync(marker, JSON.stringify({ directory: path.dirname(path.dirname(target)), file: target }));
    Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0);
  }
  return result;
};

const vscode = {
  Uri: { file: file => ({ scheme: "file", fsPath: file, toString: () => pathToFileURL(file).href }) },
  workspace: {
    isTrusted: true,
    textDocuments: [],
    getWorkspaceFolder: () => ({ uri: { fsPath: workspace } }),
  },
};
const managed = { ownsSetting: true, selectedSetting: ".graphql-desk/typings", ownsGeneration: () => false,
  async recordStaging() { return true; } };
const publisher = createAnalysisGenerations(vscode, workspace, root);
publisher.publish([[
  "graphql_desk_generated",
  [{ relative_path: "graphql_desk_generated/default.pyi", text: "selected: int\n" }],
]], "interrupted-publication", () => true, managed, "a".repeat(64), "b".repeat(64), []).catch(error => {
  console.error(error);
  process.exitCode = 1;
});
