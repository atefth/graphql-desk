"use strict";

const clientEntry = require.resolve("vscode-languageclient");
const minimatchPath = require.resolve("minimatch", { paths: [clientEntry] });
const { Minimatch } = require(minimatchPath);

// shortcut: Restore makeRe()'s truthy validation contract only; remove when vscode-languageclient stops calling it.
if (typeof Minimatch.prototype.makeRe !== "function") {
  Object.defineProperty(Minimatch.prototype, "makeRe", {
    value() { return !this.comment && this.set.length > 0; },
  });
}
