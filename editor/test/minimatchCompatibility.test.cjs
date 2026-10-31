"use strict";

const assert = require("node:assert/strict");
const Module = require("node:module");
const path = require("node:path");
const test = require("node:test");

test("patched minimatch validation preserves vscode-languageclient glob paths", async () => {
  const originalLoad = Module._load;
  const vscode = {
    workspace: {
      onDidCreateFiles() { return { dispose() {} }; },
      fs: { async stat() { return { type: 1 }; } },
    },
    Uri: { parse(value) { return { path: value, with({ path }) { return { fsPath: path }; } }; } },
    FileType: { File: 1, Directory: 2 },
  };
  Module._load = function(request, parent, isMain) {
    return request === "vscode" ? vscode : originalLoad.call(this, request, parent, isMain);
  };
  try {
    require("../src/minimatchCompatibility");
    const clientDir = path.dirname(require.resolve("vscode-languageclient"));
    const { DidCreateFilesFeature } = require(path.join(clientDir, "fileOperations.js"));
    const { matchGlobPattern } = require(path.join(clientDir, "utils/globPattern.js"));
    const feature = new DidCreateFilesFeature({
      code2ProtocolConverter: { asDidCreateFilesParams: value => value },
      middleware: { workspace: {} },
    });
    feature.register({ id: "valid", registerOptions: { filters: [{ pattern: { glob: "**/*.py" } }] } });
    assert.equal(feature.filterSize(), 1);
    const result = await feature.filter({ files: [{ uri: { fsPath: "/workspace/card.py", scheme: "file" } }] }, item => item.uri);
    assert.equal(result.files.length, 1);
    assert.throws(() => feature.register({ id: "comment", registerOptions: { filters: [{ pattern: { glob: "#ignored" } }] } }), /Invalid pattern/);
    assert.throws(() => feature.register({ id: "empty", registerOptions: { filters: [{ pattern: { glob: "" } }] } }), /Invalid pattern/);
    assert.equal(matchGlobPattern("**/*.py", { fsPath: "/workspace/card.py" }), true);
    assert.equal(matchGlobPattern("#ignored", { fsPath: "/workspace/card.py" }), false);
    assert.equal(matchGlobPattern("", { fsPath: "/workspace/card.py" }), false);
  } finally {
    Module._load = originalLoad;
  }
});
