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

/** Lexical candidates never become semantic source, diagnostics or typed facts. */
function lexicalCandidates(text) {
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
  const callees = new Set();
  for (const line of code.split(/\r\n|\r|\n/)) {
    const from = /^\s*from\s+graphql_desk\s+import\s+([_A-Za-z, \t]+)\s*$/.exec(line);
    if (from) for (const entry of from[1].split(",")) {
      const match = /^\s*(graphql|gql)(?:\s+as\s+([_A-Za-z][_0-9A-Za-z]*))?\s*$/.exec(entry);
      if (match) callees.add(match[2] ?? match[1]);
    }
    const imported = /^\s*import\s+graphql_desk(?:\s+as\s+([_A-Za-z][_0-9A-Za-z]*))?\s*$/.exec(line);
    if (imported) {
      const name = imported[1] ?? "graphql_desk";
      callees.add(`${name}.graphql`);
      callees.add(`${name}.gql`);
    }
  }
  return strings.flatMap((literal) => {
    if (/[bf]/.test(literal.prefix)) return [];
    const body = text.slice(literal.bodyStart, literal.bodyEnd);
    // Escaped ordinary literals require the semantic decoder, never guessed offsets.
    if (!literal.prefix.includes("r") && body.includes("\\")) return [];
    const match = /([_A-Za-z][_0-9A-Za-z]*(?:\.(?:graphql|gql))?)\s*\(\s*(?:source\s*=\s*)?$/.exec(code.slice(0, literal.start));
    if (!match || !callees.has(match[1])) return [];
    const name = match[1].split(".")[0];
    if (new RegExp(`(?:\\b(?:def|class)\\s+${name}\\b|\\b${name}\\s*=(?!=))`).test(code)) return [];
    return [{ text: body, start: literal.bodyStart, end: literal.bodyEnd, mode: "lexical" }];
  });
}

function createHighlighting({ analyze, isCurrent, isTrusted, mapSnapshot = value => value }) {
  if (![analyze, isCurrent, isTrusted].every((item) => typeof item === "function")) throw new Error("Analysis, trust and freshness callbacks are required");
  const lexical = (snapshot) => lexicalCandidates(snapshot.text).flatMap((candidate) =>
    tokens(candidate.text).map((token) => ({ ...token, start: token.start + candidate.start,
      end: token.end + candidate.start, mode: "lexical" })));
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
        if (bundle.response.diagnostics.some((item) => item.module.uri === snapshot.uri &&
          item.module.version === snapshot.version && item.code === "invalid-python")) result.push(...lexical(snapshot));
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
