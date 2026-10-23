"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const { createLanguageFeatures, registerLanguageFeatures, positionAt, toDecoded } = require("../src/languageFeatures");
const { bundle, document, options } = require("./featureFixtures.cjs");
const gql = require("graphql");
const gls = require("graphql-language-service");

test("actual Python source maps give exact schema/deprecation diagnostics and clear on repair", async () => {
  const value = bundle("# 😀\r\nfrom graphql_desk import gql\r\nq=gql('{ user { missing old } }')");
  const snapshot = document(value);
  const service = createLanguageFeatures(options(value));
  const diagnostics = await service.diagnostics(snapshot);
  assert.equal(diagnostics.length, 2);
  const missing = diagnostics.find((item) => item.code === "graphql.validation");
  assert.equal(snapshot.text.slice(missing.range.start, missing.range.end), "missing");
  assert.equal(missing.range.end - missing.range.start, 7);
  const deprecated = diagnostics.find((item) => item.code === "graphql.deprecation");
  assert.equal(deprecated.severity, 2);
  assert.match(deprecated.message, /Use name/);
  const fixed = bundle("from graphql_desk import gql\nq=gql('{ user { name } }')");
  assert.deepEqual(await createLanguageFeatures(options(fixed)).diagnostics(document(fixed)), []);
});

test("incomplete GraphQL inside valid Python still completes with a unique mapped name edit", async () => {
  const value = bundle("from graphql_desk import gql\nq=gql('{ user { na')");
  const snapshot = document(value);
  assert.equal(value.response.calls[0].index, null);
  const items = await createLanguageFeatures(options(value)).complete(snapshot, snapshot.text.indexOf("na'") + 2);
  const name = items.find((item) => item.label === "name");
  assert.ok(name);
  assert.equal(snapshot.text.slice(name.range.start, name.range.end), "na");
  assert.equal(name.insertText, "name");
  assert.equal(items.some((item) => "command" in item), false);
});

test("blank GraphQL completion preserves exact zero-width escape and CRLF endpoints", async () => {
  const cases = [
    ["'|'", "query"], ["r'|'", "query"], ["' |'", "query"],
    ["'\\n|'", "query"], ["'\\r\\n|'", "query"], ["'\\t|'", "query"],
    ["'\\u0020|'", "query"], ["'''\n|'''", "query"],
    ["'''\r\n|'''", "query"], ["'''\r\n|\r\n'''", "query"],
    ["'''{user{\r\n|\r\n}}'''", "name"],
    ["'''{user(\r\n|\r\n){id}}'''", "id"],
    ["'''{user(filter:{\r\n|\r\n}){id}}'''", "color"],
    ["'''{user(color:\r\n|\r\n){id}}'''", "RED"],
  ];
  for (const [expression, expected] of cases) {
    const marked = "# 😀\r\nfrom graphql_desk import gql\r\nq=gql(" + expression + ")";
    const offset = marked.indexOf("|");
    const source = marked.replace("|", "");
    const value = bundle(source);
    const snapshot = document(value);
    const call = value.response.calls[0];
    const points = toDecoded(call, snapshot, offset);
    assert.equal(points.length, 1, expression);
    const decoded = points[0].start;
    const native = gls.getAutocompleteSuggestions(gql.buildSchema(value.request.schemas[0].text),
      call.source, gls.offsetToPosition(call.source, decoded))
      .filter(item => /^[$@]?[_A-Za-z][_0-9A-Za-z]*$/.test(item.label));
    const items = await createLanguageFeatures(options(value)).complete(snapshot, offset);
    assert.deepEqual(items.map(item => item.label), native.map(item => item.label), expression);
    const item = items.find(item => item.label === expected);
    assert.ok(item, expression);
    assert.deepEqual(item.range, { start: offset, end: offset }, expression);
    assert.equal(item.insertText, expected, expression);
    const edited = source.slice(0, offset) + item.insertText + source.slice(offset);
    assert.equal(bundle(edited).response.calls[0].source,
      call.source.slice(0, decoded) + expected + call.source.slice(decoded), expression);
  }
});

test("zero-width completion retains origin multiplicity and rejects literal gaps or interiors", async () => {
  for (const marked of [
    "from graphql_desk import gql\nTEXT='|'\nq=gql(TEXT+TEXT)",
    "from graphql_desk import gql\nTEXT='|'\nq=gql(TEXT)\nr=gql(TEXT)",
    "from graphql_desk import gql\nq=gql('\\u0|020')",
    "from graphql_desk import gql\nq=gql('\\|n')",
    "from graphql_desk import gql\r\nq=gql('''\r|\n''')",
    "from graphql_desk import gql\nq=gql('\\n|' '')",
    "from graphql_desk import gql\nq=gql('\\\n|')",
    "from graphql_desk import gql\nq=gql('\\n'|)",
  ]) {
    const offset = marked.indexOf("|");
    const source = marked.replace("|", "");
    const value = bundle(source);
    assert.deepEqual(await createLanguageFeatures(options(value)).complete(document(value), offset), [], marked);
  }
});

test("hover keeps signatures/descriptions and definitions point to actual SDL fields", async () => {
  const value = bundle("from graphql_desk import gql\nq=gql('{ user { name } }')");
  const snapshot = document(value);
  const service = createLanguageFeatures(options(value));
  const offset = snapshot.text.indexOf("name") + 1;
  const hover = await service.hover(snapshot, offset);
  assert.match(JSON.stringify(hover.contents), /String!/);
  assert.match(JSON.stringify(hover.contents), /Display name/);
  const definitions = await service.definition(snapshot, offset);
  assert.equal(definitions.length, 1);
  assert.equal(definitions[0].module.uri, "file:///workspace/schema.graphql");
  assert.equal(value.request.schemas[0].text.slice(definitions[0].range.start, definitions[0].range.end), "name");
  assert.deepEqual(definitions[0].positionRange.start, positionAt(value.request.schemas[0].text, definitions[0].range.start));
});

test("definition covers schema arguments, directives, enum/input members, types and local variables", async () => {
  const source = "from graphql_desk import gql\nq=gql('query Read($id:ID,$color:Color) @tag(value: \"x\") { user(id:$id,color:RED,filter:{color:BLUE}) { name } }')";
  const value = bundle(source);
  const snapshot = document(value);
  const service = createLanguageFeatures(options(value));
  for (const [needle, skip, target] of [["id:$id", 0, "id"], ["tag(value", 0, "tag"],
    ["RED", 0, "RED"], ["filter:{color", 8, "color"], ["$color:Color", 7, "Color"]]) {
    const definitions = await service.definition(snapshot, source.indexOf(needle) + skip + 1);
    assert.equal(definitions.length, 1, needle);
    assert.equal(value.request.schemas[0].text.slice(definitions[0].range.start, definitions[0].range.end), target, needle);
  }
  const local = await service.definition(snapshot, source.indexOf("id:$id") + 5);
  assert.equal(local.length, 1);
  assert.equal(local[0].module.uri, snapshot.uri);
  assert.equal(snapshot.text.slice(local[0].range.start, local[0].range.end), "id");
  for (const [needle, skip, expected] of [["tag(value", 1, /Tag a query/],
    ["value:", 1, /Tag argument/], ["RED", 1, /Color.RED/],
    ["filter:{color", 9, /Filter.color: Color/], ["$color:Color", 8, /Available colors/]]) {
    const hover = await service.hover(snapshot, source.indexOf(needle) + skip);
    assert.match(JSON.stringify(hover.contents), expected, needle);
  }
  const inputHover = await service.hover(snapshot, source.indexOf("filter:{color") + 9);
  assert.match(JSON.stringify(inputHover.contents), /Default:/);
  assert.match(JSON.stringify(inputHover.contents), /RED/);
  const argumentHover = await service.hover(snapshot, source.indexOf("id:$id") + 1);
  assert.match(JSON.stringify(argumentHover.contents), /Default:/);
});

test("field hover includes schema arguments and GraphQL defaults in complete and incomplete sources", async () => {
  const schema = `
    enum Color { RED BLUE }
    input Filter { colors: [Color!]! }
    type User { name: String! }
    type Query {
      "Find a user" user(id: ID!, text: String = "quoted\\\"", enabled: Boolean = false,
        count: Int = 0, empty: String = null, ids: [ID!] = ["1", "2"], filter: Filter = {colors: [RED, BLUE]}): User
        @deprecated(reason: "Use viewer")
      viewer: User
    }
  `;
  const signature = 'Query.user(id: ID!, text: String = "quoted\\\"", enabled: Boolean = false, count: Int = 0, empty: String = null, ids: [ID!] = [1, 2], filter: Filter = {colors: [RED, BLUE]}): User';
  for (const format of ["sdl", "introspection"]) {
    const schemaText = format === "sdl" ? schema : JSON.stringify(gql.introspectionFromSchema(gql.buildSchema(schema)));
    for (const query of ["{ user(id: 1) { name } }", "{ selected: user(id: 1) { name } }", "{ user"]) {
      const source = `# 😀\r\nfrom graphql_desk import gql\r\nq=gql('${query}')`;
      const value = bundle(source, { schema: schemaText, schemaFormat: format });
      const snapshot = document(value);
      const service = createLanguageFeatures(options(value));
      const start = source.indexOf("user");
      const hover = await service.hover(snapshot, start + 1);
      assert.equal(hover.contents, `\`\`\`graphql\n${signature}\n\`\`\`\n\nFind a user\n\nDeprecated: Use viewer`, `${format}: ${query}`);
      assert.deepEqual(hover.range, { start, end: start + 4 });
      if (query.includes("selected:")) assert.equal(await service.hover(snapshot, source.indexOf("selected") + 1), null);
    }
    const value = bundle("from graphql_desk import gql\nq=gql('{ viewer { name } }')", { schema: schemaText, schemaFormat: format });
    const hover = await createLanguageFeatures(options(value)).hover(document(value), document(value).text.indexOf("viewer") + 1);
    assert.equal(hover.contents, "```graphql\nQuery.viewer: User\n```");
    const introspection = bundle('from graphql_desk import gql\nq=gql(\'{ __type(name: "User") { name } }\')', { schema: schemaText, schemaFormat: format });
    const typeHover = await createLanguageFeatures(options(introspection)).hover(document(introspection), document(introspection).text.indexOf("__type") + 1);
    assert.ok(typeHover.contents.startsWith("```graphql\n__type(name: String!): __Type\n```"));
  }
  const value = bundle("from graphql_desk import gql\nq=gql('{ field }')", {
    schema: 'scalar JSON type Query { "Custom field" field(arg: JSON = {key: 1}): String }',
  });
  const hover = await createLanguageFeatures(options(value)).hover(document(value), document(value).text.indexOf("field") + 1);
  assert.equal(hover.contents, "```graphql\nQuery.field: String\n```\n\nCustom field");
});

test("external fragment completion and definitions use real imported source, never printed offsets", async () => {
  const fragment = { project: "p", module: "fragments", uri: "file:///workspace/fragments.py", version: 4,
    source: "# 😀\r\nfrom graphql_desk import gql\r\ncard=gql('''fragment Card on User {\r\n id name\r\n}''')", is_package: false };
  const source = "from graphql_desk import gql\nfrom fragments import card\nq=gql('{ user { ...Card } }',[card])";
  const value = bundle(source, { modules: [fragment] });
  const service = createLanguageFeatures(options(value));
  const snapshot = document(value);
  const definitions = await service.definition(snapshot, source.indexOf("...Card") + 4);
  assert.equal(definitions.length, 1);
  assert.equal(definitions[0].module.uri, fragment.uri);
  assert.equal(definitions[0].range.start, fragment.source.indexOf("Card"));
  assert.equal(definitions[0].module.version, 4);
  const partial = bundle(source.replace("...Card", "...Ca"), { modules: [fragment] });
  const partialDoc = document(partial);
  const completion = await createLanguageFeatures(options(partial)).complete(partialDoc, partialDoc.text.indexOf("...Ca") + 5);
  assert.ok(completion.some((item) => item.label === "Card"));
});

test("fragment definitions reject ambiguous authored targets and recover after repair", async () => {
  const modules = ["left", "right"].map((module) => ({ project: "p", module,
    uri: `file:///workspace/${module}.py`, version: 4, is_package: false,
    source: `# 😀\r\nfrom graphql_desk import gql\r\ncard=gql('fragment Card on User { ${module === "left" ? "id" : "name"} }')`,
  }));
  const prefix = "# 😀\r\nfrom graphql_desk import gql\r\n";
  const imported = prefix + "from left import card as a\r\nfrom right import card as b\r\nq=gql('{user{name ...Card}}',[a,b])";
  const local = prefix + "q=gql('{user{name ...Card}} fragment Card on User{id} fragment Card on User{name}')";
  const mixed = prefix + "from left import card\r\nq=gql('{user{name ...Card}} fragment Card on User{name}',[card])";
  for (const [source, code] of [[local, "duplicate-definition"], [imported, "conflicting-fragment"], [mixed, "conflicting-fragment"]]) {
    const value = bundle(source, { modules });
    const snapshot = document(value);
    const service = createLanguageFeatures(options(value));
    assert.ok(value.response.diagnostics.some(item => item.code === code));
    assert.deepEqual(await service.definition(snapshot, source.indexOf("...Card") + 4), [], code);
    const fields = await service.definition(snapshot, source.indexOf("name ...") + 1);
    assert.equal(fields.length, 1);
    assert.equal(fields[0].module.uri, value.request.schemas[0].uri);
    assert.ok((await service.diagnostics(snapshot)).some(item => item.code === `graphql-desk.${code}`));
  }
  for (const source of [
    imported.replace("[a,b]", "[b]"),
    local.replace(" fragment Card on User{id}", ""),
    imported.replace("[a,b]", "[b,b]"),
  ]) {
    const value = bundle(source, { modules });
    const definitions = await createLanguageFeatures(options(value)).definition(document(value), source.indexOf("...Card") + 4);
    assert.equal(definitions.length, 1);
    const target = value.request.modules.find(item => item.uri === definitions[0].module.uri);
    assert.equal(target.source.slice(definitions[0].range.start, definitions[0].range.end), "Card");
    assert.equal(target.uri, source.includes("from right") ? modules[1].uri : document(value).uri);
  }
});

test("escapes, adjacent literals and repeated/shared origins never become speculative completion edits", async () => {
  for (const source of [
    "from graphql_desk import gql\nq=gql('{user{na\\u006de}}')",
    "from graphql_desk import gql\nq=gql('{user{na' 'me}}')",
    "from graphql_desk import gql\nTEXT='{user{na}}'\nq=gql(TEXT+TEXT)",
    "from graphql_desk import gql\nTEXT='{user{na}}'\nq=gql(TEXT)\nr=gql(TEXT)",
  ]) {
    const value = bundle(source);
    const snapshot = document(value);
    const offset = source.includes("\\u") ? source.indexOf("\\u") + 3 : source.indexOf("na") + 2;
    assert.deepEqual(await createLanguageFeatures(options(value)).complete(snapshot, offset), [], source);
  }
});

test("schema argument/value errors and client directive filtering retain schema validation", async () => {
  const value = bundle("from graphql_desk import gql\nq=gql('{ user(id:true,color:BAD) @client { name } }')");
  const diagnostics = await createLanguageFeatures(options(value)).diagnostics(document(value));
  assert.equal(diagnostics.length, 2);
  assert.ok(diagnostics.some((item) => /ID cannot represent/.test(item.message)));
  assert.ok(diagnostics.some((item) => /BAD/.test(item.message)));
  assert.equal(diagnostics.some((item) => /Unknown directive/.test(item.message)), false);
});

test("configured client directives complete only in directive-name context", async () => {
  const source = "from graphql_desk import gql\nq=gql('{ user @loc { name } }')";
  const value = bundle(source);
  const snapshot = document(value);
  const service = createLanguageFeatures(options(value, { clientDirectives: () => ["local"] }));
  const directive = await service.complete(snapshot, source.indexOf("@loc") + 4);
  const local = directive.find((item) => item.label === "local");
  assert.ok(local);
  assert.equal(local.detail, "Configured client directive");
  assert.equal(snapshot.text.slice(local.range.start, local.range.end), "loc");
  assert.equal(local.insertText, "local");
  const field = await service.complete(snapshot, source.indexOf("name") + 2);
  assert.equal(field.some((item) => item.label === "local"), false);
});

test("configured client directives suppress only unknown-directive errors and deduplicate shared origins", async () => {
  const source = "from graphql_desk import gql\nTEXT='{ user @local { missing old } }'\nfirst=gql(TEXT)\nsecond=gql(TEXT)";
  const value = bundle(source);
  const diagnostics = await createLanguageFeatures(options(value, { clientDirectives: () => ["local"] }))
    .diagnostics(document(value));
  assert.deepEqual(diagnostics.map((item) => item.code).sort(), ["graphql.deprecation", "graphql.validation"]);
  assert.deepEqual(diagnostics.map((item) => document(value).text.slice(item.range.start, item.range.end)).sort(), ["missing", "old"]);
  assert.equal(diagnostics.some((item) => /Unknown directive/.test(item.message)), false);
});

test("untrusted, cancelled, stale, wrong-schema and failed analysis yield no precise results", async () => {
  const value = bundle("from graphql_desk import gql\nq=gql('{ user { na } }')");
  const snapshot = document(value);
  let calls = 0;
  const noTrust = createLanguageFeatures(options(value, { isTrusted: () => false, analyze: async () => { calls += 1; return value; } }));
  assert.deepEqual(await noTrust.complete(snapshot, snapshot.text.indexOf("na") + 2), []);
  assert.deepEqual(await noTrust.diagnostics(snapshot), []);
  assert.equal(await noTrust.hover(snapshot, 0), null);
  assert.deepEqual(await noTrust.definition(snapshot, 0), []);
  assert.equal(calls, 0);
  const cancelled = { isCancellationRequested: true };
  assert.deepEqual(await createLanguageFeatures(options(value)).complete(snapshot, 0, cancelled), []);
  assert.deepEqual(await createLanguageFeatures(options(value, { isCurrent: async () => false })).diagnostics(snapshot), []);
  const mismatched = structuredClone(value);
  mismatched.response.schemas[0].token = "stale";
  assert.deepEqual(await createLanguageFeatures(options(mismatched)).complete(snapshot, snapshot.text.indexOf("na") + 2), []);
  const failure = createLanguageFeatures(options(value, { analyze: async () => { throw Error("PRIVATE_MARKER"); } }));
  assert.deepEqual(await failure.diagnostics(snapshot), []);
  assert.deepEqual(await failure.complete(snapshot, 0), []);
});

test("cancellation during the final freshness await cannot publish", async () => {
  const value = bundle("from graphql_desk import gql\nq=gql('{ user { name } }')");
  const token = { isCancellationRequested: false };
  let checks = 0;
  const service = createLanguageFeatures(options(value, { isCurrent: async () => {
    checks += 1;
    if (checks === 2) token.isCancellationRequested = true;
    return true;
  } }));
  assert.equal(await service.hover(document(value), document(value).text.indexOf("name") + 1, token), null);
  let validations = 0;
  const failingGuard = createLanguageFeatures(options(value, { isCurrent: async () => {
    if (++validations === 2) throw new Error("guard failed");
    return true;
  } }));
  assert.equal(await failingGuard.hover(document(value), document(value).text.indexOf("name") + 1), null);
});

test("VS Code registration supplements Python services and ignores old diagnostic refreshes", async () => {
  const value = bundle("from graphql_desk import gql\nq=gql('{user{missing}}')");
  const publications = [];
  const providers = {};
  let disposed = 0;
  const handle = () => ({ dispose() { disposed += 1; } });
  const vscode = {
    Range: class { constructor(start, end) { this.start = start; this.end = end; } },
    CompletionItem: class { constructor(label) { this.label = label; } },
    Diagnostic: class { constructor(range, message, severity) { Object.assign(this, { range, message, severity }); } },
    languages: {
      createDiagnosticCollection() { return { set(_uri, diagnostics) { publications.push(diagnostics); }, delete() {}, dispose() { disposed += 1; } }; },
      registerCompletionItemProvider(selector, provider) { providers.complete = provider; assert.equal(selector[0].language, "python"); return handle(); },
      registerHoverProvider(_selector, provider) { providers.hover = provider; return handle(); },
      registerDefinitionProvider(_selector, provider) { providers.definition = provider; return handle(); },
    },
  };
  let release;
  let invocation = 0;
  const registered = registerLanguageFeatures(vscode, options(value, { analyze: async () => {
    invocation += 1;
    if (invocation === 1) await new Promise((resolve) => { release = resolve; });
    return value;
  } }));
  const item = value.request.modules[0];
  const doc = { uri: { toString: () => item.uri }, version: item.version,
    getText: () => item.source, positionAt: (offset) => positionAt(item.source, offset) };
  const old = registered.refresh(doc);
  await registered.refresh(doc);
  release();
  await old;
  assert.equal(publications.length, 1);
  assert.equal(publications[0][0].code, "graphql.validation");
  assert.deepEqual(Object.keys(providers).sort(), ["complete", "definition", "hover"]);
  registered.dispose();
  assert.equal(disposed, 4);
  let releaseFinal;
  let enteredFinal;
  const reachedFinal = new Promise((resolve) => { enteredFinal = resolve; });
  let checks = 0;
  const pendingRegistration = registerLanguageFeatures(vscode, options(value, {
    isCurrent: async () => {
      if (++checks === 2) {
        enteredFinal();
        await new Promise((resolve) => { releaseFinal = resolve; });
      }
      return true;
    },
  }));
  doc.offsetAt = (position) => position.offset;
  const completion = providers.complete.provideCompletionItems(doc, { offset: item.source.indexOf("missing") + 2 });
  await reachedFinal;
  pendingRegistration.dispose();
  releaseFinal();
  assert.deepEqual(await completion, []);
});
