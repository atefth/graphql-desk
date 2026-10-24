"use strict";

const { CharacterStream, onlineParser } = require("graphql-language-service");
const { toHost } = require("./languageFeatures");

const COLORS = {
  keyword: "symbolIcon.keywordForeground", meta: "symbolIcon.keywordForeground",
  def: "symbolIcon.fieldForeground", property: "symbolIcon.fieldForeground",
  type: "symbolIcon.classForeground", variable: "symbolIcon.variableForeground",
  string: "symbolIcon.stringForeground", number: "symbolIcon.numberForeground",
  comment: "descriptionForeground", attribute: "symbolIcon.eventForeground",
  atom: "symbolIcon.enumMemberForeground", builtin: "symbolIcon.classForeground",
  qualifier: "symbolIcon.fieldForeground",
  "string-2": "symbolIcon.enumMemberForeground",
};

function tokens(text) {
  const parser = onlineParser();
  const state = parser.startState();
  const result = [];
  let base = 0;
  for (const line of text.split("\n")) {
    const stream = new CharacterStream(line);
    while (!stream.eol()) {
      const start = stream.getCurrentPosition();
      const scope = parser.token(stream, state);
      const end = stream.getCurrentPosition();
      if (end <= start) break;
      if (COLORS[scope]) result.push({ start: base + start, end: base + end, scope });
    }
    base += line.length + 1;
  }
  return result;
}

function decodePythonString(text, raw) {
  const output = [];
  const source = [];
  const append = (value, start, end) => {
    output.push(value);
    for (let index = 0; index < value.length; index += 1) source.push({ start, end });
  };
  const codePoint = digits => {
    const value = Number.parseInt(digits, 16);
    return Number.isSafeInteger(value) && value <= 0x10ffff && !(value >= 0xd800 && value <= 0xdfff)
      ? String.fromCodePoint(value) : null;
  };
  const simple = { "\\": "\\", "'": "'", '"': '"', a: "\x07", b: "\b", f: "\f",
    n: "\n", r: "\r", t: "\t", v: "\x0b" };
  for (let index = 0; index < text.length;) {
    if (raw || text[index] !== "\\") {
      const value = String.fromCodePoint(text.codePointAt(index));
      append(value, index, index + value.length);
      index += value.length;
      continue;
    }
    const escape = text[index + 1];
    if (escape === undefined) return null;
    if (Object.prototype.hasOwnProperty.call(simple, escape)) {
      append(simple[escape], index, index + 2);
      index += 2;
      continue;
    }
    if (escape === "\n" || escape === "\r") {
      index += escape === "\r" && text[index + 2] === "\n" ? 3 : 2;
      continue;
    }
    if (escape === "x" || escape === "u" || escape === "U") {
      const size = escape === "x" ? 2 : escape === "u" ? 4 : 8;
      const digits = text.slice(index + 2, index + 2 + size);
      if (digits.length !== size || !/^[0-9a-f]+$/i.test(digits)) return null;
      const value = codePoint(digits);
      if (value === null) return null;
      append(value, index, index + size + 2);
      index += size + 2;
      continue;
    }
    if (/[0-7]/.test(escape)) {
      const digits = text.slice(index + 1).match(/^[0-7]{1,3}/)[0];
      append(String.fromCodePoint(Number.parseInt(digits, 8)), index, index + digits.length + 1);
      index += digits.length + 1;
      continue;
    }
    // shortcut: named and unknown escapes stay unhighlighted until an exact non-executing decoder is available.
    return null;
  }
  return { text: output.join(""), source };
}

/** Lexical candidates never become semantic source, diagnostics or typed facts. */
function lexicalCandidates(text, factories = []) {
  const strings = [];
  const mask = text.split("");
  for (let i = 0; i < text.length;) {
    if (text[i] === "#") {
      while (i < text.length && !/[\r\n]/.test(text[i])) mask[i++] = " ";
      continue;
    }
    const literal = /^(?:[rRuUbBfF]{0,2})(?:"""|'''|"|')/.exec(text.slice(i));
    if (!literal || (i && /[_0-9A-Za-z]/.test(text[i - 1]))) { i += 1; continue; }
    const quote = /("""|'''|"|')$/.exec(literal[0])[0];
    const prefix = literal[0].slice(0, -quote.length).toLowerCase();
    const start = i;
    const bodyStart = i + literal[0].length;
    i = bodyStart;
    while (i < text.length && !text.startsWith(quote, i)) {
      if (quote.length === 1 && /[\r\n]/.test(text[i])) break;
      if (text[i] === "\\") i += Math.min(2, text.length - i);
      else i += 1;
    }
    const bodyEnd = i;
    if (text.startsWith(quote, i)) i += quote.length;
    strings.push({ start, bodyStart, bodyEnd, prefix });
    for (let position = start; position < i; position += 1) {
      if (!/[\r\n]/.test(text[position])) mask[position] = " ";
    }
  }
  const code = mask.join("");
  const constructors = [
    { module: "graphql_desk", export: "graphql" },
    { module: "graphql_desk", export: "gql" }, ...factories,
  ];
  const callees = new Set();
  const imports = new Map();
  const importedName = name => imports.set(name, (imports.get(name) ?? 0) + 1);
  const importsFrom = /^from[ \t]+([_A-Za-z][_0-9A-Za-z]*(?:\.[_A-Za-z][_0-9A-Za-z]*)*)[ \t]+import(?=[ \t(])[ \t]*(?:\([ \t\r\n]*([\s\S]*?)\)|([^\r\n]+))[ \t]*$/gm;
  for (const from of code.matchAll(importsFrom)) {
    const entries = (from[2] ?? from[3]).split(",").map(entry => entry.trim());
    if (entries.at(-1) === "") entries.pop();
    const names = entries.map(entry => /^([_A-Za-z][_0-9A-Za-z]*)(?:\s+as\s+([_A-Za-z][_0-9A-Za-z]*))?$/.exec(entry));
    if (!names.length || names.some(match => !match)) continue;
    for (const match of names) {
      importedName(match[2] ?? match[1]);
      if (constructors.some(item => item.module === from[1] && item.export === match[1])) callees.add(match[2] ?? match[1]);
    }
  }
  for (const line of code.split(/\r\n|\r|\n/)) {
    const imported = /^import\s+([_A-Za-z][_0-9A-Za-z]*(?:\.[_A-Za-z][_0-9A-Za-z]*)*)(?:\s+as\s+([_A-Za-z][_0-9A-Za-z]*))?\s*$/.exec(line);
    if (imported) {
      importedName(imported[2] ?? imported[1].split(".")[0]);
      for (const item of constructors) {
        if (item.module === imported[1]) callees.add(`${imported[2] ?? imported[1]}.${item.export}`);
      }
    }
  }
  return strings.flatMap((literal) => {
    if (!["", "r", "u"].includes(literal.prefix)) return [];
    const decoded = decodePythonString(text.slice(literal.bodyStart, literal.bodyEnd), literal.prefix === "r");
    if (!decoded) return [];
    const match = /([_A-Za-z][_0-9A-Za-z]*(?:\.[_A-Za-z][_0-9A-Za-z]*)*)\s*\(\s*(?:source\s*=\s*)?$/.exec(code.slice(0, literal.start));
    if (!match || !callees.has(match[1])) return [];
    if (/(?:[\p{ID_Continue}]|\.\s*)$/u.test(code.slice(0, match.index))) return [];
    const name = match[1].split(".")[0];
    if (imports.get(name) !== 1) return [];
    if (new RegExp(`(?:\\b(?:def|class)\\s+${name}\\b|\\b${name}\\s*=(?!=))`).test(code)) return [];
    const parts = match[1].split(".");
    if (parts.some((_, index) => new RegExp(`\\b${parts.slice(0, index + 1).join("\\.")}\\s*=(?!=)`).test(code))) return [];
    return [{ ...decoded, start: literal.bodyStart, mode: "lexical" }];
  });
}

function createHighlighting({ analyze, isCurrent, isTrusted, mapSnapshot = value => value }) {
  if (![analyze, isCurrent, isTrusted].every((item) => typeof item === "function")) throw new Error("Analysis, trust and freshness callbacks are required");
  const lexical = (snapshot, factories = []) => lexicalCandidates(snapshot.text, factories).flatMap((candidate) =>
    tokens(candidate.text).flatMap((token) => {
      const first = candidate.source[token.start];
      const last = candidate.source[token.end - 1];
      return first && last ? [{ ...token, start: first.start + candidate.start,
        end: last.end + candidate.start, mode: "lexical" }] : [];
    }));
  return {
    async highlight(snapshot, token) {
      if (token?.isCancellationRequested) return [];
      if (!isTrusted()) return lexical(snapshot);
      try {
        const bundle = await analyze(snapshot, token);
        if (bundle) snapshot = mapSnapshot(snapshot, bundle);
        if (!bundle || token?.isCancellationRequested || !isTrusted() ||
            bundle.request.request_id !== bundle.response.request_id ||
            !bundle.request.modules.some((item) => item.uri === snapshot.uri && item.version === snapshot.version && item.source === snapshot.text) ||
            !await isCurrent(bundle, snapshot, token) || token?.isCancellationRequested || !isTrusted()) return [];
        const result = bundle.response.calls.flatMap((call) => tokens(call.source).flatMap((token) =>
          toHost(bundle, call, token).filter((origin) => origin.module.uri === snapshot.uri && origin.module.version === snapshot.version)
            .map((origin) => ({ ...origin.range, scope: token.scope, mode: "resolved" }))));
        const owners = bundle.request.modules.filter(item => item.uri === snapshot.uri &&
          item.version === snapshot.version && item.source === snapshot.text);
        if (bundle.response.diagnostics.some((item) => item.code === "invalid-python" &&
          owners.some(owner => owner.project === item.module.project && owner.module === item.module.module &&
            owner.uri === item.module.uri && owner.version === item.module.version))) {
          const bindings = owners.length === 1 ? bundle.request.bindings.filter(item => item.project === owners[0].project) : [];
          result.push(...lexical(snapshot, bindings.length === 1 ? bindings[0].factories : []));
        }
        if (!await isCurrent(bundle, snapshot, token) || token?.isCancellationRequested || !isTrusted()) return [];
        return [...new Map(result.map((item) => [JSON.stringify(item), item])).values()];
      } catch { return []; }
    },
  };
}

function registerHighlighting(vscode, options) {
  let disposed = false;
  const generations = new Map();
  const service = createHighlighting({ ...options,
    isCurrent: async (...args) => !disposed && await options.isCurrent(...args) && !disposed,
  });
  const decorations = new Map(Object.entries(COLORS).map(([scope, color]) =>
    [scope, vscode.window.createTextEditorDecorationType({ color: new vscode.ThemeColor(color) })]));
  return {
    async refresh(editor, token) {
      if (disposed || editor.document.languageId !== "python") return;
      const document = editor.document;
      const uri = document.uri.toString();
      const version = document.version;
      const generation = (generations.get(uri) ?? 0) + 1;
      generations.set(uri, generation);
      const snapshot = options.snapshot ? options.snapshot(document) : { uri, version, text: document.getText() };
      const items = await service.highlight(snapshot, token);
      if (disposed || document.isClosed || generations.get(uri) !== generation || document.version !== version || token?.isCancellationRequested) return;
      for (const [scope, decoration] of decorations) editor.setDecorations(decoration,
        items.filter((item) => item.scope === scope).map((item) =>
          new vscode.Range(document.positionAt(item.start), document.positionAt(item.end))));
    },
    close(uri) { generations.set(uri.toString(), (generations.get(uri.toString()) ?? 0) + 1); },
    dispose() { disposed = true; generations.clear(); decorations.forEach((item) => item.dispose()); },
  };
}

module.exports = { createHighlighting, registerHighlighting, lexicalCandidates, tokens };
