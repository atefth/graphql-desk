"use strict";

const clientEntry = require.resolve("vscode-languageclient");
const minimatchPath = require.resolve("minimatch", { paths: [clientEntry] });
const { Minimatch }: typeof import("minimatch") = require(minimatchPath);
const matcherPrototype: typeof Minimatch.prototype & { makeRe?: () => boolean } = Minimatch.prototype;

// shortcut: Restore makeRe()'s truthy validation contract only; remove when vscode-languageclient stops calling it.
if (typeof matcherPrototype.makeRe !== "function") {
  Object.defineProperty(matcherPrototype, "makeRe", {
    value(this: typeof matcherPrototype) { return !this.comment && this.set.length > 0; },
  });
}

export = {};
