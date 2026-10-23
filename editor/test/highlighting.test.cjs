"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const { createHighlighting, registerHighlighting, lexicalCandidates, tokens } = require("../src/highlighting");
const { positionAt } = require("../src/languageFeatures");
const { bundle, document, options } = require("./featureFixtures.cjs");

test("resolved highlighting uses real maps with escapes, Unicode, CRLF and adjacent literals", async () => {
  const source = "# 😀\r\nfrom graphql_desk import gql\r\nq=gql('''query Read {\r\n user(id: \"😀\", color: RED, filter: { color: BLUE }) { na\\u006de ''' 'id } }')";
  const value = bundle(source);
  const highlights = await createHighlighting(options(value)).highlight(document(value));
  assert.ok(highlights.some((item) => source.slice(item.start, item.end) === "query"));
  assert.ok(highlights.some((item) => source.slice(item.start, item.end) === '\"😀\"' && item.scope === "string"));
  assert.ok(highlights.some((item) => source.slice(item.start, item.end) === "BLUE" && item.scope === "string-2"));
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
  const text = 'query Read($id:ID){ user(id:$id) { name # comment\n old(arg:12, text:"😀", enum:GREEN)';
  const styles = new Set(tokens(text).map((item) => item.scope));
  for (const scope of ["keyword", "variable", "property", "comment", "number", "string", "string-2"]) assert.ok(styles.has(scope), scope);
  assert.ok(tokens(text).every((item) => item.end > item.start && item.end <= text.length));
});

test("incomplete GraphQL grammar scopes operation, type, alias, directive, fragment and value tokens", () => {
  const text = '# 😀\r\nquery Search($id: ID! = "x") @skip(if: true) { alias: user(id: $id) { ...UserFields } } fragment UserFields on User { name: field(arg: 1.2e-4, choices: [RED, null], meta: { enabled: false, text: "😀" })';
  const scopes = tokens(text).map((item) => ({ text: text.slice(item.start, item.end), scope: item.scope }));
  for (const [value, scope] of [
    ["query", "keyword"], ["Search", "def"], ["$", "variable"], ["id", "variable"], ["ID", "atom"],
    ['"x"', "string"], ["@", "meta"], ["skip", "meta"], ["if", "attribute"], ["true", "builtin"],
    ["alias", "property"], ["user", "qualifier"], ["UserFields", "def"],
    ["on", "keyword"], ["User", "atom"], ["field", "qualifier"], ["arg", "attribute"],
    ["1.2e-4", "number"], ["choices", "attribute"], ["RED", "string-2"], ["null", "keyword"],
    ["meta", "attribute"], ["enabled", "attribute"], ["false", "builtin"],
    ["text", "attribute"], ['"😀"', "string"],
  ]) assert.ok(scopes.some((item) => item.text === value && item.scope === scope), `${value}: ${scope}`);
  assert.ok(scopes.every((item) => item.text.length > 0));
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

test("incomplete Python highlights only valid non-interpolated string prefixes", async () => {
  for (const prefix of ["ur", "ru", "rr", "br", "rb", "fr", "rf", "b", "f"]) {
    const value = bundle(`from graphql_desk import gql\nq=gql(${prefix}'''query { field`);
    assert.ok(value.response.diagnostics.some(item => item.code === "invalid-python"), prefix);
    assert.deepEqual(await createHighlighting(options(value)).highlight(document(value)), [], prefix);
  }
  for (const prefix of ["", "r", "u"]) {
    const value = bundle(`from graphql_desk import gql\nq=gql(${prefix}'''query { field`);
    const highlights = await createHighlighting(options(value)).highlight(document(value));
    assert.ok(highlights.length, prefix);
    assert.ok(highlights.every(item => item.mode === "lexical"), prefix);
  }
});

test("lexical candidates exclude lookalikes, strings/comments as imports, interpolation and shadowed aliases", () => {
  const unrelated = [
    "gql('query { field }')",
    "from other import gql\nq=gql('query { field }')",
    "def helper():\n    from graphql_desk import gql\nq=gql('''query { field",
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

test("configured factory candidates accept exact direct/qualified aliases and preserve lexical exclusions", () => {
  const factories = [{ module: "generated.default", export: "graphql" }, { module: "custom.templates", export: "document" }, { module: "custom2.templates", export: "document2" }];
  for (const [imported, callee] of [
    ["from generated.default import graphql", "graphql"],
    ["from generated.default import graphql as query", "query"],
    ["from generated.default import graphql as query2", "query2"],
    ["import generated.default as documents", "documents.graphql"],
    ["import generated.default", "generated.default.graphql"],
    ["from custom.templates import document as query", "query"],
    ["import custom.templates as templates", "templates.document"],
    ["from custom2.templates import document2", "document2"],
    ["import custom2.templates as templates2", "templates2.document2"],
  ]) {
    const text = `# 😀\r\n${imported}\r\nq=${callee}(source=r'query Read { title`;
    const candidates = lexicalCandidates(text, factories);
    assert.equal(candidates.length, 1, text);
    assert.equal(text.slice(candidates[0].start, candidates[0].end), "query Read { title");
    assert.equal(candidates[0].mode, "lexical");
    assert.deepEqual(lexicalCandidates(text), [], "Configured identities are never assumed by the untrusted default");
  }
  for (const text of [
    "from generated.other import graphql\nq=graphql('query { title",
    "from generated.default import other\nq=other('query { title",
    "from generated.default import graphql\ngraphql=unknown\nq=graphql('query { title",
    "import generated.default as documents\ndocuments=unknown\nq=documents.graphql('query { title",
    "import generated.default as documents\ndocuments.graphql=unknown\nq=documents.graphql('query { title",
    "import generated.default\ngenerated.default=unknown\nq=generated.default.graphql('query { title",
    "from generated.default import graphql\nfrom other import graphql\nq=graphql('query { title",
    "import generated.default as documents\nimport other as documents\nq=documents.graphql('query { title",
    "from generated.default import graphql\nq=graphql(f'query { title",
    "from generated.default import graphql\nq=graphql(b'query { title",
    "from generated.default import graphql\nq=graphql('query\\n{ title",
    "# from generated.default import graphql\nq=graphql('query { title",
    "'from generated.default import graphql'\nq=graphql('query { title",
  ]) assert.deepEqual(lexicalCandidates(text, factories), [], text);
});

test("incomplete Python recognizes parenthesized imports without promoting lexical candidates", async () => {
  for (const [imported, callee, factories] of [
    ["from graphql_desk import (\n    gql as query, # comment\n)", "query", []],
    ["from graphql_desk import(\n    gql as\n        query,\n)", "query", []],
    ["from generated.default import (\n    graphql as query,\n)", "query", [{ module: "generated.default", export: "graphql" }]],
  ]) {
    const text = `${imported}\nresult=${callee}(source='''query Read { user`;
    const candidates = lexicalCandidates(text, factories);
    assert.equal(candidates.length, 1, text);
    assert.equal(candidates[0].mode, "lexical");
    assert.equal(text.slice(candidates[0].start, candidates[0].end), "query Read { user");
    assert.deepEqual(lexicalCandidates(text), factories.length ? [] : candidates);
  }
  const text = "from graphql_desk import (\n    gql as query,\n)\nresult=query(source='''query Read { user";
  const value = bundle(text);
  const highlights = await createHighlighting(options(value)).highlight(document(value));
  assert.ok(highlights.some(item => text.slice(item.start, item.end) === "query"));
  assert.ok(highlights.every(item => item.mode === "lexical"));
  assert.deepEqual(lexicalCandidates("from graphql_desk import (\n    other,\n)\nq=gql('''query { field"), []);
  assert.deepEqual(lexicalCandidates("from\ngraphql_desk import gql\nq=gql('''query { field"), []);
});

test("incomplete lexical calls cannot borrow an imported callee suffix", async () => {
  for (const [imported, callee, factories] of [
    ["from graphql_desk import gql", "gql", []],
    ["import graphql_desk as desk", "desk.gql", []],
    ["from generated.default import graphql as query", "query", [{ module: "generated.default", export: "graphql" }]],
    ["import generated.default as documents", "documents.graphql", [{ module: "generated.default", export: "graphql" }]],
  ]) {
    for (const prefix of ["other . ", "other.\t", "λ", "a\u0301", "𐐀"]) {
      const text = `# 😀\r\n${imported}\r\nq=${prefix}${callee}('''query Read { user`;
      assert.deepEqual(lexicalCandidates(text, factories), [], text);
    }
    const text = `${imported}\nq=${callee}('''query Read { user`;
    assert.equal(lexicalCandidates(text, factories).length, 1, "The imported callee itself remains a candidate");
  }
  for (const callee of ["other . gql", "λgql", "a\u0301gql"]) {
    const text = `from graphql_desk import gql\nq=${callee}('''query Read { user`;
    const incomplete = bundle(text);
    assert.ok(incomplete.response.diagnostics.some(item => item.code === "invalid-python"));
    assert.deepEqual(await createHighlighting(options(incomplete)).highlight(document(incomplete)), []);
    const repaired = bundle(`${text} } }''')`);
    assert.equal(repaired.response.calls.length, 0);
    assert.deepEqual(await createHighlighting(options(repaired)).highlight(document(repaired)), []);
  }
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
