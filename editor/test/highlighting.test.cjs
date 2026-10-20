"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const { createHighlighting, registerHighlighting, lexicalCandidates, tokens } = require("../src/highlighting");
const { positionAt } = require("../src/languageFeatures");
const { bundle, document, options } = require("./featureFixtures.cjs");

test("resolved highlighting uses real maps with escapes, Unicode, CRLF and adjacent literals", async () => {
  const source = "# 😀\r\nfrom graphql_desk import gql\r\nq=gql('''query Read {\r\n user { na\\u006de ''' 'id } }')";
  const value = bundle(source);
  const highlights = await createHighlighting(options(value)).highlight(document(value));
  assert.ok(highlights.some((item) => source.slice(item.start, item.end) === "query"));
  assert.ok(highlights.some((item) => source.slice(item.start, item.end) === "\\u006d"));
  assert.ok(highlights.some((item) => source.slice(item.start, item.end) === "id"));
  assert.ok(highlights.every((item) => item.mode === "resolved"));
  for (const item of highlights) {
    assert.ok(item.start >= source.indexOf("query"));
    assert.ok(!source.slice(item.start, item.end).includes("'''"));
    assert.ok(!source.slice(item.start, item.end).includes("gql"));
  }
});

test("token categories tolerate incomplete GraphQL and cover strings, comments, numbers and variables", () => {
  const text = 'query Read($id:ID){ user(id:$id) { name # comment\n old(arg:12, text:"😀")';
  const styles = new Set(tokens(text).map((item) => item.scope));
  for (const scope of ["keyword", "variable", "property", "comment", "number", "string"]) assert.ok(styles.has(scope), scope);
  assert.ok(tokens(text).every((item) => item.end > item.start && item.end <= text.length));
});

test("untrusted workspace gets lexical highlighting without any Python analysis", async () => {
  const text = "from graphql_desk import gql as document\nq=document('''query Read { user { name";
  let analyses = 0;
  const highlighter = createHighlighting({
    analyze: async () => { analyses += 1; throw new Error("must not run"); },
    isCurrent: async () => { throw new Error("no semantic facts"); },
    isTrusted: () => false,
  });
  const highlights = await highlighter.highlight({ uri: "untitled:new.py", version: 1, text });
  assert.ok(highlights.some((item) => text.slice(item.start, item.end) === "query"));
  assert.ok(highlights.every((item) => item.mode === "lexical"));
  assert.equal(analyses, 0);
});

test("incomplete Python uses a lexical candidate, then recovers to resolved maps", async () => {
  const incomplete = bundle("from graphql_desk import gql\nq=gql('''query Read { user { name");
  assert.equal(incomplete.response.calls.length, 0);
  const lexical = await createHighlighting(options(incomplete)).highlight(document(incomplete));
  assert.ok(lexical.length);
  assert.ok(lexical.every((item) => item.mode === "lexical"));
  const repaired = bundle("from graphql_desk import gql\nq=gql('''query Read { user { name } }''')");
  const resolved = await createHighlighting(options(repaired)).highlight(document(repaired));
  assert.ok(resolved.length);
  assert.ok(resolved.every((item) => item.mode === "resolved"));
});

test("lexical candidates exclude lookalikes, strings/comments as imports, interpolation and shadowed aliases", () => {
  const unrelated = [
    "gql('query { field }')",
    "from other import gql\nq=gql('query { field }')",
    "# from graphql_desk import gql\nq=gql('query { field }')",
    "'''from graphql_desk import gql'''\nq=gql('query { field }')",
    "from graphql_desk import gql\nq=gql(f'query { field }')",
    "from graphql_desk import gql\nq=gql(b'query { field }')",
    "from graphql_desk import gql\ngql=lambda source:source\nq=gql('query { field }')",
    "from graphql_desk import gql\nq=gql('query\\n{ field }')",
  ];
  for (const text of unrelated) assert.deepEqual(lexicalCandidates(text), [], text);
  for (const text of [
    "import graphql_desk as desk\nq=desk.gql('query { field }')",
    "from graphql_desk import graphql as document\nq=document(source=r'query { field }')",
  ]) assert.equal(lexicalCandidates(text).length, 1, text);
});

test("valid Python semantic shadowing does not fall back to lexical precision", async () => {
  const value = bundle("from graphql_desk import gql\ndef gql(source):return source\nq=gql('query { field }')");
  assert.deepEqual(await createHighlighting(options(value)).highlight(document(value)), []);
});

test("cancelled, stale, changed or newly untrusted semantic results cannot highlight", async () => {
  const value = bundle("from graphql_desk import gql\nq=gql('{user{name}}')");
  assert.deepEqual(await createHighlighting(options(value, { isCurrent: async () => false })).highlight(document(value)), []);
  assert.deepEqual(await createHighlighting(options(value)).highlight({ ...document(value), version: 2 }), []);
  const token = { isCancellationRequested: false };
  let checks = 0;
  const highlighter = createHighlighting(options(value, { isCurrent: async () => {
    checks += 1;
    if (checks === 2) token.isCancellationRequested = true;
    return true;
  } }));
  assert.deepEqual(await highlighter.highlight(document(value), token), []);
});

test("VS Code decorations stay read-only and disposal prevents late publication", async () => {
  const value = bundle("from graphql_desk import gql\nq=gql('{user{name}}')");
  const source = document(value);
  let release;
  let disposed = 0;
  const publications = [];
  const vscode = {
    ThemeColor: class { constructor(id) { this.id = id; } },
    Range: class { constructor(start, end) { this.start = start; this.end = end; } },
    window: { createTextEditorDecorationType(style) { return { style, dispose() { disposed += 1; } }; } },
  };
  const document_ = { languageId: "python", uri: { toString: () => source.uri }, version: source.version,
    getText: () => source.text, positionAt: (offset) => positionAt(source.text, offset) };
  const editor = { document: document_, setDecorations(...args) { publications.push(args); } };
  const highlighting = registerHighlighting(vscode, options(value, { analyze: async () => {
    await new Promise((resolve) => { release = resolve; }); return value;
  } }));
  const pending = highlighting.refresh(editor);
  highlighting.dispose();
  release();
  await pending;
  assert.equal(publications.length, 0);
  assert.ok(disposed > 0);
});
