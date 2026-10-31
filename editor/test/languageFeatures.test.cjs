"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const { createLanguageFeatures, registerLanguageFeatures, positionAt, toDecoded } = require("../src/languageFeatures");
const { bundle, document, options, schema } = require("./featureFixtures.cjs");
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

test("unused imported fragments are reported as warnings", async () => {
  const source = "from graphql_desk import gql\nfrom fragments import card as Card\nq=gql('{ user { id } }')";
  const fragment = { project: "p", module: "fragments", uri: "file:///workspace/fragments.py", version: 1,
    source: "from graphql_desk import gql\ncard=gql('fragment Card on User { name }')", is_package: false };
  const value = bundle(source, { modules: [fragment] });
  const diagnostic = (await createLanguageFeatures(options(value)).diagnostics(document(value)))
    .find(item => item.code === "graphql-desk.unused-colocated-fragment");
  assert.ok(diagnostic);
  assert.equal(diagnostic.severity, 2);
  assert.equal(source.slice(diagnostic.range.start, diagnostic.range.end), "from fragments import card as Card");
});

test("bounded field usage is a warning at the selected parent, without a deletion action", async () => {
  const source = [
    "from graphql_desk import gql",
    "from graphql_desk.testing import read_result",
    "query = gql('{ user { id name } }')",
    "result = read_result(query, {})",
    "print(result['user']['id'])",
  ].join("\n");
  const value = bundle(source);
  const snapshot = document(value);
  const service = createLanguageFeatures(options(value));
  const diagnostics = await service.diagnostics(snapshot);
  const warning = diagnostics.find(item => item.code === "graphql-desk.52005");
  assert.ok(warning);
  assert.equal(warning.severity, 2);
  assert.equal(source.slice(warning.range.start, warning.range.end), "user");
  assert.match(warning.message, /user\.name/);
  assert.deepEqual(await service.codeActions(snapshot, [warning]), []);
});

test("stale persisted hashes produce an exact fix only for direct ID literals", async () => {
  const source = [
    "from graphql_desk import gql",
    "from fragments import card",
    "query = gql('query Q { user { ...Card } }', [card])",
    `registered = gql.persisted(${JSON.stringify(`sha256:${"0".repeat(64)}`)}, query)`,
  ].join("\n");
  const fragment = { project: "p", module: "fragments", uri: "file:///workspace/fragments.py", version: 2,
    source: "from graphql_desk import gql\ncard = gql('fragment Card on User { name }')", is_package: false };
  const value = bundle(source, { modules: [fragment] });
  const snapshot = document(value);
  const service = createLanguageFeatures(options(value));
  const diagnostics = await service.diagnostics(snapshot);
  const stale = diagnostics.find(item => item.code === "graphql-desk.persisted-hash-mismatch");
  assert.ok(stale);
  assert.match(snapshot.text.slice(stale.range.start, stale.range.end), /^["']sha256:/);
  const actions = await service.codeActions(snapshot, [{ code: stale.code, range: stale.range }]);
  assert.equal(actions.length, 1);
  assert.equal(snapshot.text.slice(actions[0].range.start, actions[0].range.end), snapshot.text.slice(stale.range.start, stale.range.end));
  assert.match(actions[0].replacement, /^"sha256:[0-9a-f]{64}"$/);
  assert.notEqual(actions[0].replacement.slice(1, -1), `sha256:${"0".repeat(64)}`);

  const aliased = bundle(source.replace(
    `registered = gql.persisted(${JSON.stringify(`sha256:${"0".repeat(64)}`)}, query)`,
    `id = ${JSON.stringify(`sha256:${"0".repeat(64)}`)}\nregistered = gql.persisted(id, query)`,
  ), { modules: [fragment] });
  const aliasService = createLanguageFeatures(options(aliased));
  const aliasDiagnostic = (await aliasService.diagnostics(document(aliased)))
    .find(item => item.code === "graphql-desk.persisted-hash-mismatch");
  assert.ok(aliasDiagnostic);
  assert.deepEqual(await aliasService.codeActions(document(aliased), [
    { code: aliasDiagnostic.code, range: aliasDiagnostic.range },
  ]), []);
});

test("missing persisted ID action uses only unambiguous document arguments", async () => {
  const source = "from graphql_desk import gql\nquery = gql('query Q { user { id } }')\nsaved = gql.persisted(document=query)\n";
  const value = bundle(source);
  const snapshot = document(value);
  const service = createLanguageFeatures(options(value));
  const diagnostic = (await service.diagnostics(snapshot)).find(item =>
    item.code === "graphql-desk.persisted-id-missing");
  assert.ok(diagnostic);
  assert.equal(snapshot.text.slice(diagnostic.range.start, diagnostic.range.end), "gql.persisted(document=query)");
  const actions = await service.codeActions(snapshot, [{ code: diagnostic.code, range: diagnostic.range }]);
  assert.equal(actions.length, 1);
  assert.deepEqual(actions[0].range, { start: source.indexOf("document"), end: source.indexOf("document") });
  assert.match(actions[0].replacement, /^id="sha256:[0-9a-f]{64}", $/);
  assert.deepEqual(await service.codeActions(snapshot, [{ code: diagnostic.code,
    range: { start: diagnostic.range.start + 1, end: diagnostic.range.end } }]), []);

  const positionalSource = source.replace("gql.persisted(document=query)", "gql.persisted(query)");
  const positional = bundle(positionalSource);
  const positionalSnapshot = document(positional);
  const positionalService = createLanguageFeatures(options(positional));
  const positionalDiagnostic = (await positionalService.diagnostics(positionalSnapshot)).find(item =>
    item.code === "graphql-desk.persisted-id-missing");
  assert.ok(positionalDiagnostic);
  const positionalActions = await positionalService.codeActions(positionalSnapshot, [
    { code: positionalDiagnostic.code, range: positionalDiagnostic.range },
  ]);
  assert.equal(positionalActions.length, 1);
  const argumentStart = positionalSource.indexOf("gql.persisted(query)") + "gql.persisted(".length;
  assert.deepEqual(positionalActions[0].range, { start: argumentStart, end: argumentStart });
  assert.match(positionalActions[0].replacement, /^"sha256:[0-9a-f]{64}", $/);
});

test("dynamic factory source is diagnosed while valid sibling operations remain indexed", async () => {
  const source = `from graphql_desk import gql
def query_source() -> str: return "{ user { name } }"
good = gql("{ user { id } }")
bad = gql(query_source())`;
  const value = bundle(source);
  assert.equal(value.response.calls.length, 1);
  assert.match(value.response.calls[0].source, /user \{ id \}/);
  const call = "gql(query_source())";
  const start = source.lastIndexOf(call);
  const diagnostics = await createLanguageFeatures(options(value)).diagnostics(document(value));
  const invalid = diagnostics.find(item => item.code === "graphql-desk.source-not-static");
  assert.ok(invalid);
  assert.deepEqual(invalid.range, { start, end: start + call.length });
});

test("imported GraphQL constants map editor navigation, completion and diagnostics to each origin", async () => {
  const main = "from graphql_desk import gql\nfrom first import FIRST\nfrom second import SECOND\nquery = gql(FIRST + SECOND)";
  const first = 'FIRST = "{ user { "';
  const second = '# 😀\r\nSECOND = "name missing } }"';
  const value = bundle(main, { modules: [
    { project: "p", module: "first", uri: "file:///workspace/first.py", version: 1, source: first, is_package: false },
    { project: "p", module: "second", uri: "file:///workspace/second.py", version: 1, source: second, is_package: false },
  ] });
  const features = createLanguageFeatures(options(value));
  const firstDocument = { uri: "file:///workspace/first.py", version: 1, text: first };
  const secondDocument = { uri: "file:///workspace/second.py", version: 1, text: second };

  const userDefinition = await features.definition(firstDocument, first.indexOf("user") + 1);
  assert.equal(userDefinition.length, 1);
  assert.equal(userDefinition[0].module.uri, "file:///workspace/schema.graphql");
  assert.equal(value.request.schemas[0].text.slice(userDefinition[0].range.start, userDefinition[0].range.end), "user");

  const nameStart = second.indexOf("name");
  const completion = await features.complete(secondDocument, nameStart + 2);
  const name = completion.find(item => item.label === "name");
  assert.ok(name);
  assert.deepEqual(name.range, { start: nameStart, end: nameStart + 4 });
  assert.equal(second.slice(name.range.start, name.range.end), "name");

  const missingStart = second.indexOf("missing");
  const diagnostics = await features.diagnostics(secondDocument);
  const missing = diagnostics.find(item => item.code === "graphql.validation");
  assert.ok(missing);
  assert.deepEqual(missing.range, { start: missingStart, end: missingStart + "missing".length });
  assert.equal(second.slice(missing.range.start, missing.range.end), "missing");
  assert.deepEqual(await features.diagnostics(firstDocument), []);
});

test("syntax diagnostics highlight escaped and literal errors while retaining EOF points", async () => {
  for (const [source, expected] of [
    ["from graphql_desk import gql\nq=gql('{ user{ \\u0060 } }')", "\\u0060"],
    ["from graphql_desk import gql\nq=gql('{ user{ ` } }')", "`"],
    ["from graphql_desk import gql\nq=gql('{ user{ 😀 } }')", "😀"],
    ["from graphql_desk import gql\nq=gql('{ user{ \\U0001F600 } }')", "\\U0001F600"],
    ["from graphql_desk import gql\nq=gql('{')", ""],
  ]) {
    const value = bundle(source);
    const diagnostics = await createLanguageFeatures(options(value)).diagnostics(document(value));
    const syntax = diagnostics.find((item) => item.code === "graphql.syntax");
    assert.ok(syntax, source);
    assert.equal(source.slice(syntax.range.start, syntax.range.end), expected, source);
    assert.equal(syntax.range.end - syntax.range.start, expected.length, source);
    if (!expected) assert.deepEqual(syntax.range, { start: source.indexOf("')"), end: source.indexOf("')") });
  }
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

test("fragment references and rename follow imported aliases and re-exports only", async () => {
  const fragment = { project: "p", module: "fragments", uri: "file:///workspace/fragments.py", version: 4,
    source: "from graphql_desk import gql\n# Card is incidental\nLABEL = 'Card'\ncard = gql('fragment Card on User { id }')", is_package: false };
  const exports = { project: "p", module: "exports", uri: "file:///workspace/exports.py", version: 7,
    source: "from fragments import card as aliased\n", is_package: false };
  const source = "from graphql_desk import gql\nfrom exports import aliased as public_card\nLABEL = 'Card'\nq = gql('query Q { user { ...Card } }', [public_card])";
  const value = bundle(source, { modules: [fragment, exports] });
  const service = createLanguageFeatures(options(value));
  const main = document(value);
  const spread = source.indexOf("...Card") + 4;
  const symbol = await service.prepareRename(main, spread);
  assert.equal(symbol.placeholder, "Card");
  assert.equal(main.text.slice(symbol.range.start, symbol.range.end), "Card");
  const references = await service.references(main, spread, true);
  assert.deepEqual(references.map((item) => item.module.uri).sort(), [fragment.uri, main.uri].sort());
  assert.equal(references.some((item) => item.range.start === source.indexOf("Card")), false);
  const fragmentDocument = { uri: fragment.uri, version: fragment.version, text: fragment.source };
  const fragmentDefinition = fragment.source.indexOf("fragment Card") + "fragment ".length;
  assert.equal((await service.prepareRename(fragmentDocument, fragmentDefinition)).placeholder, "Card");
  assert.equal((await service.references(fragmentDocument, fragmentDefinition, true)).length, 2);
  assert.equal((await service.rename(fragmentDocument, fragmentDefinition, "HeaderCard")).length, 2);
  const edits = await service.rename(main, spread, "UserCard");
  assert.equal(edits.length, 2);
  for (const item of edits) {
    const owner = value.request.modules.find((module) => module.uri === item.module.uri);
    assert.equal(owner.version, item.module.version);
    assert.equal(owner.source.slice(item.range.start, item.range.end), "Card");
    assert.equal(item.replacement, "UserCard");
    assert.ok(item.positionRange.start && item.positionRange.end);
  }
  assert.equal(await service.prepareRename(main, source.indexOf("LABEL") + 1), null);
  assert.equal(await service.rename(main, spread, "not-valid"), null);
  const readOnly = createLanguageFeatures(options(value, { isCurrent: async () => false }));
  assert.equal(await readOnly.rename(main, spread, "UserCard"), null);
});

test("fragment rename refuses ambiguous spreads and preserves escaped host text", async () => {
  const fragment = { project: "p", module: "fragments", uri: "file:///workspace/fragments.py", version: 4,
    source: "from graphql_desk import gql\ncard = gql('fragment C\\u0061rd on User { id }')", is_package: false };
  const source = "from graphql_desk import gql\nfrom fragments import card\nq=gql('query Q { user { ...Card } }',[card])\nLABEL='Card'";
  const value = bundle(source, { modules: [fragment] });
  const service = createLanguageFeatures(options(value));
  const snapshot = document(value);
  const edits = await service.rename(snapshot, source.indexOf("...Card") + 4, "UserCard");
  assert.equal(edits.length, 2);
  const fragmentEdit = edits.find((item) => item.module.uri === fragment.uri);
  assert.ok(fragmentEdit);
  assert.equal(fragment.source.slice(fragmentEdit.range.start, fragmentEdit.range.end), "C\\u0061rd");
  assert.equal(fragment.source.slice(0, fragmentEdit.range.start) + "UserCard" +
    fragment.source.slice(fragmentEdit.range.end), "from graphql_desk import gql\ncard = gql('fragment UserCard on User { id }')");
  assert.equal((await service.references(snapshot, source.indexOf("...Card") + 4, false)).some((item) => item.module.uri === fragment.uri), false);

  const ambiguous = bundle("from graphql_desk import gql\nfrom left import card as a\nfrom right import card as b\nq=gql('{ user { ...Card } }',[a,b])", {
    modules: ["left", "right"].map((module) => ({ project: "p", module, uri: `file:///workspace/${module}.py`, version: 1, is_package: false,
      source: `from graphql_desk import gql\ncard=gql('fragment Card on User { ${module === "left" ? "id" : "name"} }')` })),
  });
  const ambiguousDoc = document(ambiguous);
  assert.deepEqual(await createLanguageFeatures(options(ambiguous)).references(ambiguousDoc,
    ambiguousDoc.text.indexOf("...Card") + 4, true), []);
  assert.equal(await createLanguageFeatures(options(ambiguous)).rename(ambiguousDoc,
    ambiguousDoc.text.indexOf("...Card") + 4, "UserCard"), null);

  const collisionSource = "from graphql_desk import gql\nq=gql('fragment Card on User { id } fragment UserCard on User { name } query Q { user { ...Card } }')";
  const collision = bundle(collisionSource);
  assert.equal(await createLanguageFeatures(options(collision)).rename(document(collision),
    collisionSource.indexOf("...Card") + 4, "UserCard"), null);
});

test("same-named fragments from another schema are isolated from references and rename", async () => {
  const source = "from shop import graphql as shop\nfrom users import graphql as users\n" +
    "shop_card=shop('fragment Card on User { id }')\nusers_card=users('fragment Card on User { name }')\n" +
    "query=shop('{ user { ...Card } }',[shop_card])";
  const extra = {
    includeDefaultSchema: false,
    schemaNames: ["shop", "users"], defaultSchema: null,
    factories: [
      { module: "shop", export: "graphql", schema_name: "shop" },
      { module: "users", export: "graphql", schema_name: "users" },
    ],
    schemas: ["shop", "users"].map((schema_name, index) => ({ project: "p", schema_name,
      uri: `file:///workspace/${schema_name}.graphql`, version: index + 2, token: `${schema_name}-schema`,
      format: "sdl", text: "type User { id: ID name: String } type Query { user: User }" })),
  };
  const value = bundle(source, extra);
  const service = createLanguageFeatures(options(value));
  const snapshot = document(value);
  const spread = source.indexOf("...Card") + 4;
  const references = await service.references(snapshot, spread, true);
  assert.equal(references.length, 2);
  const usersName = source.indexOf("fragment Card", source.indexOf("fragment Card") + 1) + "fragment ".length;
  assert.equal((await service.prepareRename(snapshot, usersName)).placeholder, "Card");
  const usersReferences = await service.references(snapshot, usersName, true);
  assert.equal(usersReferences.length, 1);
  assert.equal(usersReferences[0].range.start, usersName);
  const edits = await service.rename(snapshot, spread, "ShopCard");
  assert.equal(edits.length, 2);
  assert.equal(edits.some((item) => item.range.start === usersName), false);
});

test("registered fragment rename exposes fresh plans for the LSP bridge", async () => {
  const fragment = { project: "p", module: "fragments", uri: "file:///workspace/fragments.py", version: 4,
    source: "from graphql_desk import gql\ncard=gql('fragment Card on User { id }')", is_package: false };
  const source = "from graphql_desk import gql\nfrom fragments import card\nq=gql('{ user { ...Card } }',[card])";
  const value = bundle(source, { modules: [fragment] });
  const providers = {};
  const openDocuments = new Map();
  const noop = () => ({ dispose() {} });
  const vscode = {
    Uri: { parse(value) { return { toString: () => value }; } },
    Range: class { constructor(start, end) { Object.assign(this, { start, end }); } },
    Location: class { constructor(uri, range) { Object.assign(this, { uri, range }); } },
    Diagnostic: class {},
    CodeAction: class {},
    CodeActionKind: { QuickFix: 1 },
    languages: {
      createDiagnosticCollection: noop,
      registerCompletionItemProvider(_selector, value) { providers.complete = value; return noop(); },
      registerHoverProvider(_selector, value) { providers.hover = value; return noop(); },
      registerDefinitionProvider(_selector, value) { providers.definition = value; return noop(); },
      registerReferenceProvider(_selector, value) { providers.references = value; return noop(); },
      registerDocumentSymbolProvider(_selector, value) { providers.symbols = value; return noop(); },
      registerCodeActionsProvider(_selector, value) { providers.actions = value; return noop(); },
    },
    workspace: { textDocuments: [], async openTextDocument(uri) { return openDocuments.get(uri.toString()); } },
  };
  const registration = registerLanguageFeatures(vscode, options(value));
  const mainModule = value.request.modules[0];
  const documentFor = (module) => ({ uri: { toString: () => module.uri }, version: module.version,
    languageId: "python", getText: () => module.source, positionAt: (offset) => ({ offset }), offsetAt: (position) => position.offset });
  const main = documentFor(mainModule);
  const fragmentDocument = documentFor(fragment);
  openDocuments.set(mainModule.uri, main);
  openDocuments.set(fragment.uri, fragmentDocument);
  vscode.workspace.textDocuments = [main, fragmentDocument];
  const spread = source.indexOf("...Card") + 4;
  const prepared = await registration.prepareRename(main, { offset: spread });
  assert.equal(prepared.placeholder, "Card");
  const references = await providers.references.provideReferences(main, { offset: spread }, { includeDeclaration: true });
  assert.equal(references.length, 2);
  const plan = await registration.rename(main, { offset: spread }, "UserCard");
  assert.equal(plan.length, 2);
  assert.deepEqual([...new Set(plan.map(item => item.module.uri))].sort(), [fragment.uri, mainModule.uri].sort());
  assert.ok(plan.every(item => item.replacement === "UserCard" && item.sourceText.length > 0));
  registration.dispose();
});

test("document symbols expose multi-operation and fragment ranges inside embedded Python", async () => {
  const source = "# 😀\r\nfrom graphql_desk import gql\r\nquery = gql('query ReadUser { user { id } } query UpdateUser { user { name } } fragment UserCard on User { id name }')";
  const value = bundle(source);
  const symbols = await createLanguageFeatures(options(value)).symbols(document(value));
  assert.deepEqual(symbols.map(item => [item.name, item.detail, item.kind]), [
    ["ReadUser", "query", "operation"], ["UpdateUser", "query", "operation"], ["UserCard", "fragment on User", "fragment"],
  ]);
  for (const item of symbols) {
    assert.equal(source.slice(item.selectionRange.start, item.selectionRange.end), item.name);
    assert.equal(source.slice(item.range.start, item.range.end), item.name === "ReadUser" ? "query ReadUser { user { id } }" :
      item.name === "UpdateUser" ? "query UpdateUser { user { name } }" : "fragment UserCard on User { id name }");
    assert.ok(item.range.start <= item.selectionRange.start && item.selectionRange.end <= item.range.end);
  }
});

test("document symbols recover complete definitions before an incomplete tail and reject stale snapshots", async () => {
  const source = "from graphql_desk import gql\nq=gql('query Read { user { id } } query Broken {')\nr=gql('query List { user { name } } ?')";
  const value = bundle(source);
  const symbols = await createLanguageFeatures(options(value)).symbols(document(value));
  assert.deepEqual(symbols.map(item => [item.name, source.slice(item.range.start, item.range.end)]), [
    ["Read", "query Read { user { id } }"],
    ["List", "query List { user { name } }"]
  ]);
  assert.deepEqual(await createLanguageFeatures(options(value, { isCurrent: async () => false })).symbols(document(value)), []);
});

test("document symbols use the call range when an adjacent-literal definition has split origins", async () => {
  const source = "from graphql_desk import gql\nq=gql('query Read { user { ' 'id } }')";
  const value = bundle(source);
  const symbols = await createLanguageFeatures(options(value)).symbols(document(value));
  assert.equal(symbols.length, 1);
  assert.equal(symbols[0].name, "Read");
  assert.equal(source.slice(symbols[0].selectionRange.start, symbols[0].selectionRange.end), "Read");
  assert.equal(source.slice(symbols[0].range.start, symbols[0].range.end), "gql('query Read { user { ' 'id } }')");
});

test("document symbols skip invalid GraphQL and source names outside their containing call", async () => {
  const source = "from graphql_desk import gql\nTEXT='query Read { user { id } }'\nfirst=gql(TEXT)\nsecond=gql(TEXT)\nbad=gql('query Broken {')";
  const value = bundle(source);
  const symbols = await createLanguageFeatures(options(value)).symbols(document(value));
  assert.deepEqual(symbols, []);
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

test("quick fixes replace only suggested names and explicit valid deprecated field replacements", async () => {
  const source = "from graphql_desk import gql\nq=gql('query { user { alias: nam old old2 } }')";
  const value = bundle(source, { schema: schema.replace(
    "old: String @deprecated(reason:\"Use name\")",
    "old: String @deprecated(reason:\"Use name\") old2: String @deprecated(reason:\"Use missing\")",
  ) });
  const snapshot = document(value);
  const service = createLanguageFeatures(options(value));
  const diagnostics = await service.diagnostics(snapshot);
  const actions = await service.codeActions(snapshot,
    diagnostics.filter((item) => ["graphql.validation", "graphql.deprecation"].includes(item.code)));
  assert.deepEqual(actions.map((item) => [item.title, snapshot.text.slice(item.range.start, item.range.end), item.replacement]), [
    ["Change 'nam' to 'name'", "nam", "name"],
    ["Replace deprecated field 'old' with 'name'", "old", "name"],
  ]);

  const escaped = "from graphql_desk import gql\nq=gql('query { user { na\\u006d } }')";
  const escapedValue = bundle(escaped);
  const escapedSnapshot = document(escapedValue);
  const escapedDiagnostics = await createLanguageFeatures(options(escapedValue)).diagnostics(escapedSnapshot);
  const escapedCodeActions = await createLanguageFeatures(options(escapedValue)).codeActions(escapedSnapshot,
    escapedDiagnostics.filter((item) => item.code === "graphql.validation"));
  assert.deepEqual(escapedCodeActions, [], "escaped Python source is never rewritten as decoded text");
});

test("deprecated replacement suggestions reject fields deprecated with an empty reason", async () => {
  const source = "from graphql_desk import gql\nq=gql('query { user { old } }')";
  const value = bundle(source, { schema: schema.replace(
    "name: String! old: String @deprecated(reason:\"Use name\")",
    "name: String! @deprecated(reason:\"\") old: String @deprecated(reason:\"Use name\")",
  ) });
  const snapshot = document(value);
  const service = createLanguageFeatures(options(value));
  const diagnostics = await service.diagnostics(snapshot);
  const deprecation = diagnostics.find((item) => item.code === "graphql.deprecation" && /old/.test(item.message));
  assert.ok(deprecation);
  assert.deepEqual(await service.codeActions(snapshot, [deprecation]), []);
});

test("extracting complete sibling fields creates a colocated fragment and composes it", async () => {
  const source = "from graphql_desk import gql\nq = gql('query Read { user { id name } }')\n";
  const value = bundle(source);
  const selection = { start: source.indexOf("id name"), end: source.indexOf("id name") + "id name".length };
  const plan = await createLanguageFeatures(options(value)).extractFragment(document(value), selection);
  assert.equal(plan.length, 3);
  const updated = [...plan].sort((a, b) => b.range.start - a.range.start)
    .reduce((text, item) => text.slice(0, item.range.start) + item.replacement + text.slice(item.range.end), source);
  assert.match(updated, /user_fields = gql\(source="fragment UserFields on User/);
  assert.match(updated, /q = gql\('query Read \{ user \{ \.\.\.UserFields \} \}', fragments=\[user_fields\]\)/);
  const refreshed = bundle(updated);
  assert.deepEqual(refreshed.response.diagnostics, []);
  assert.equal(refreshed.response.calls.length, 2);
  assert.equal(refreshed.response.calls[1].fragments.length, 1);
});

test("extraction preserves existing composition and avoids Python and GraphQL names", async () => {
  const source = "from graphql_desk import gql\nuser_fields = 1\ncard = gql('fragment UserFields on User { id }')\nq = gql('query Read { user { name ...UserFields } }', [card])\n";
  const value = bundle(source);
  const selection = { start: source.indexOf("name ..."), end: source.indexOf("name ...") + 4 };
  const plan = await createLanguageFeatures(options(value)).extractFragment(document(value), selection);
  assert.equal(plan.length, 3);
  const updated = [...plan].sort((a, b) => b.range.start - a.range.start)
    .reduce((text, item) => text.slice(0, item.range.start) + item.replacement + text.slice(item.range.end), source);
  assert.match(updated, /user_fields2 = gql\(source="fragment UserFields2 on User/);
  assert.match(updated, /\[user_fields2, card\]/);
  assert.deepEqual(bundle(updated).response.diagnostics, []);
});

test("extraction avoids Python identifiers that normalize to the generated binding", async () => {
  const source = "from graphql_desk import gql\nｕｓｅｒ＿ｆｉｅｌｄｓ = 1\nq=gql('query { user { name } }')\n";
  const value = bundle(source), start = source.indexOf("name");
  const plan = await createLanguageFeatures(options(value)).extractFragment(document(value), { start, end: start + 4 });
  assert.equal(plan.length, 3);
  assert.match(plan.find(item => item.replacement.includes(" = gql"))?.replacement, /user_fields2 = gql/);
});

test("extraction preserves referenced fragments inside selected fields", async () => {
  const source = "from graphql_desk import gql\ncard = gql('fragment Card on User { id }')\nq = gql('query Read { user { friend { ...Card } name } }', [card])\n";
  const value = bundle(source, { schema: schema.replace("id: ID!", "id: ID! friend: User") });
  const start = source.indexOf("friend { ...Card }");
  const selection = { start, end: start + "friend { ...Card }".length };
  const plan = await createLanguageFeatures(options(value)).extractFragment(document(value), selection);
  assert.equal(plan.length, 3);
  const updated = [...plan].sort((a, b) => b.range.start - a.range.start)
    .reduce((text, item) => text.slice(0, item.range.start) + item.replacement + text.slice(item.range.end), source);
  assert.match(updated, /user_fields = gql\(source="fragment UserFields on User.*", fragments=\[card\]\)/);
  assert.match(updated, /\[user_fields, card\]/);
  assert.deepEqual(bundle(updated, { schema: value.request.schemas[0].text }).response.diagnostics, []);
});

test("extraction preserves aliases, CRLF, keyword arguments and variables", async () => {
  const source = "# 😀\r\nfrom graphql_desk import gql as make\r\nq = make(source='query Read($id: ID) { user(id: $id) { name } }', fragments=())\r\n";
  const value = bundle(source);
  const selected = "user(id: $id) { name }", start = source.indexOf(selected);
  const plan = await createLanguageFeatures(options(value)).extractFragment(document(value), { start, end: start + selected.length });
  assert.equal(plan.length, 3);
  const updated = [...plan].sort((a, b) => b.range.start - a.range.start)
    .reduce((text, item) => text.slice(0, item.range.start) + item.replacement + text.slice(item.range.end), source);
  assert.match(updated, /query_fields = make\(source="fragment QueryFields on Query/);
  assert.match(updated, /fragments=\(query_fields,\)/);
  assert.equal(updated.split("\n").filter(line => line && !line.endsWith("\r")).length, 0);
  assert.deepEqual(bundle(updated).response.diagnostics, []);
});

test("extraction adds composition outside grouped sources and after trailing comments", async () => {
  for (const call of [
    "gql((('query Read { user { id name } }')))",
    "gql((('query Read { user { id name } }')), # trailing comment\n)",
  ]) {
    const source = `from graphql_desk import gql\nq=${call}\n`;
    const value = bundle(source), start = source.indexOf("id name");
    const plan = await createLanguageFeatures(options(value)).extractFragment(document(value), { start, end: start + 7 });
    assert.equal(plan.length, 3, source);
    const updated = [...plan].sort((a, b) => b.range.start - a.range.start)
      .reduce((text, item) => text.slice(0, item.range.start) + item.replacement + text.slice(item.range.end), source);
    assert.deepEqual(bundle(updated).response.diagnostics, [], updated);
  }
});

test("extraction leaves leading Python comments attached to the original query", async () => {
  const source = "from graphql_desk import gql\n# Query note\nq=gql('query { user { name } }')\n";
  const value = bundle(source), start = source.indexOf("name");
  const plan = await createLanguageFeatures(options(value)).extractFragment(document(value), { start, end: start + 4 });
  const updated = [...plan].sort((a, b) => b.range.start - a.range.start)
    .reduce((text, item) => text.slice(0, item.range.start) + item.replacement + text.slice(item.range.end), source);
  assert.match(updated, /user_fields = gql\(.*\)\n# Query note\nq=/);
  assert.deepEqual(bundle(updated).response.diagnostics, []);
});

test("extraction rejects partial, shared, escaped, invalid and stale selections", async () => {
  const cases = [
    ["from graphql_desk import gql\nq=gql('query { user { name id } }')", "ame id"],
    ["from graphql_desk import gql\nTEXT='query { user { name } }'\nq=gql(TEXT)\nr=gql(TEXT)", "name"],
    ["from graphql_desk import gql\nq=gql('query { user { na\\u006de } }')", "na\\u006de"],
    ["from graphql_desk import gql\nq=gql('query { user { missing } }')", "missing"],
    ["from graphql_desk import gql\nq=gql('query { user { ...Card } } fragment Card on User { id }')", "user { ...Card }"],
    ["from graphql_desk import gql\ndef read():\n    q=gql('query { user { name } }')", "name"],
  ];
  for (const [source, selected] of cases) {
    const value = bundle(source), start = source.indexOf(selected);
    const plan = await createLanguageFeatures(options(value)).extractFragment(document(value), { start, end: start + selected.length });
    assert.equal(plan, null, source);
  }
  const source = "from graphql_desk import gql\nq=gql('query { user { id name } }')";
  const value = bundle(source), start = source.indexOf("id name");
  const selection = { start, end: start + 7 };
  assert.equal(await createLanguageFeatures(options(value, { isCurrent: async () => false }))
    .extractFragment(document(value), selection), null);
  assert.equal(await createLanguageFeatures(options(value)).extractFragment(document(value), selection,
    { isCancellationRequested: true }), null);
});

test("extraction rejects custom factories without a proven composition contract", async () => {
  for (const [factory, call] of [
    ["def custom(source): return source", "custom('query { user { name } }')"],
    ["def custom(*, source, fragments=()): return source", "custom(source='query { user { name } }')"],
  ]) {
    const source = `from templates import custom\nq=${call}\n`;
    const module = { project: "p", module: "templates", uri: "file:///workspace/templates.py", version: 1,
      source: `${factory}\n`, is_package: false };
    const value = bundle(source, { modules: [module], factories: [{ module: "templates", export: "custom", schema_name: null }] });
    assert.equal(value.response.calls.length, 1);
    const start = source.indexOf("name");
    assert.equal(await createLanguageFeatures(options(value)).extractFragment(document(value), { start, end: start + 4 }), null);
  }
});

test("extraction supports a known schema-bound initializer", async () => {
  const source = "from graphql_desk import init_graphql\ngql = init_graphql('default')\nq=gql('query { user { name } }')\n";
  const value = bundle(source), start = source.indexOf("name");
  const plan = await createLanguageFeatures(options(value)).extractFragment(document(value), { start, end: start + 4 });
  assert.equal(plan.length, 3);
  const updated = [...plan].sort((a, b) => b.range.start - a.range.start)
    .reduce((text, item) => text.slice(0, item.range.start) + item.replacement + text.slice(item.range.end), source);
  assert.deepEqual(bundle(updated).response.diagnostics, []);
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
  assert.deepEqual(await noTrust.symbols(snapshot), []);
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
  const value = bundle(`from graphql_desk import gql\nq=gql('query Check {user{nam}}')\np=gql.persisted("sha256:${"0".repeat(64)}", q)`);
  const publications = [];
  const providers = {};
  let disposed = 0;
  const handle = () => ({ dispose() { disposed += 1; } });
  const vscode = {
    Uri: { parse(value) { return { toString: () => value }; } },
    Range: class { constructor(start, end) { this.start = start; this.end = end; } },
    Location: class { constructor(uri, range) { Object.assign(this, { uri, range }); } },
    TextEdit: { replace(range, newText) { return { range, newText }; } },
    CompletionItem: class { constructor(label) { this.label = label; } },
    Diagnostic: class { constructor(range, message, severity) { Object.assign(this, { range, message, severity }); } },
    CodeAction: class { constructor(title, kind) { Object.assign(this, { title, kind }); } },
    CodeActionKind: { QuickFix: 1, RefactorExtract: 2 },
    WorkspaceEdit: class { constructor() { this.changes = []; } replace(...change) { this.changes.push(change); } },
    languages: {
      createDiagnosticCollection() { return { set(_uri, diagnostics) { publications.push(diagnostics); }, delete() {}, dispose() { disposed += 1; } }; },
      registerCompletionItemProvider(selector, provider) { providers.complete = provider; assert.equal(selector[0].language, "python"); return handle(); },
      registerHoverProvider(_selector, provider) { providers.hover = provider; return handle(); },
      registerDefinitionProvider(_selector, provider) { providers.definition = provider; return handle(); },
      registerReferenceProvider(_selector, provider) { providers.references = provider; return handle(); },
      registerDocumentSymbolProvider(_selector, provider) { providers.symbols = provider; return handle(); },
      registerCodeActionsProvider(_selector, provider) { providers.actions = provider; return handle(); },
    },
    DocumentSymbol: class { constructor(name, detail, kind, range, selectionRange) { Object.assign(this, { name, detail, kind, range, selectionRange }); } },
    SymbolKind: { Function: 11, Struct: 22 },
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
    getText: () => item.source, positionAt: (offset) => ({ offset }), offsetAt: (position) => position.offset };
  const old = registered.refresh(doc);
  await registered.refresh(doc);
  release();
  await old;
  assert.equal(publications.length, 1);
  assert.ok(publications[0].some(item => item.code === "graphql.validation"));
  const persistedDiagnostic = publications[0].find(item => item.code === "graphql-desk.persisted-hash-mismatch");
  assert.ok(persistedDiagnostic);
  assert.deepEqual(Object.keys(providers).sort(), ["actions", "complete", "definition", "hover", "references", "symbols"]);
  const codeActions = await providers.actions.provideCodeActions(doc, undefined,
    { diagnostics: [persistedDiagnostic] });
  assert.equal(codeActions.length, 1);
  assert.equal(codeActions[0].kind, vscode.CodeActionKind.QuickFix);
  assert.equal(codeActions[0].edit.changes[0][0], doc.uri);
  assert.equal(codeActions[0].edit.changes[0][1].start.offset, persistedDiagnostic.range.start.offset);
  assert.match(codeActions[0].edit.changes[0][2], /^"sha256:[0-9a-f]{64}"$/);
  const validationDiagnostic = publications[0].find(item => item.code === "graphql.validation");
  assert.ok(validationDiagnostic);
  const validationActions = await providers.actions.provideCodeActions(doc, undefined,
    { diagnostics: [validationDiagnostic] });
  assert.equal(validationActions.length, 1);
  assert.equal(doc.getText().slice(validationActions[0].edit.changes[0][1].start.offset,
    validationActions[0].edit.changes[0][1].end.offset), "nam");
  assert.equal(validationActions[0].edit.changes[0][2], "name");
  const documentSymbols = await providers.symbols.provideDocumentSymbols(doc);
  assert.deepEqual(documentSymbols.map(item => [item.name, item.detail, item.kind]), [["Check", "query", 11]]);
  registered.dispose();
  assert.equal(disposed, 7);
  const extractValue = bundle("from graphql_desk import gql\nq=gql('query { user { id name } }')");
  const extractRegistered = registerLanguageFeatures(vscode, options(extractValue));
  const extractItem = extractValue.request.modules[0];
  const extractDoc = { uri: { scheme: "file", toString: () => extractItem.uri }, version: extractItem.version,
    getText: () => extractItem.source, positionAt: (offset) => ({ offset }), offsetAt: (position) => position.offset };
  const start = extractItem.source.indexOf("id name");
  const extraction = await providers.actions.provideCodeActions(extractDoc,
    { start: { offset: start }, end: { offset: start + 7 } }, { diagnostics: [] });
  assert.equal(extraction.length, 1);
  assert.equal(extraction[0].kind, vscode.CodeActionKind.RefactorExtract);
  assert.equal(extraction[0].command.command, "graphqlDesk.extractFragment");
  assert.equal(extraction[0].command.arguments[0].version, extractDoc.version);
  assert.equal(extraction[0].edit, undefined);
  assert.deepEqual(await providers.actions.provideCodeActions(extractDoc,
    { start: { offset: start }, end: { offset: start + 7 } },
    { diagnostics: [], only: { contains: kind => kind === vscode.CodeActionKind.QuickFix } }), []);
  extractRegistered.dispose();
  let resumeAction;
  const delayed = registerLanguageFeatures(vscode, options(extractValue, {
    analyze: async () => { await new Promise(resolve => { resumeAction = resolve; }); return extractValue; },
  }));
  const pendingAction = providers.actions.provideCodeActions(extractDoc,
    { start: { offset: start }, end: { offset: start + 7 } }, { diagnostics: [] });
  extractDoc.version += 1;
  resumeAction();
  assert.deepEqual(await pendingAction, []);
  delayed.dispose();
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
  const completion = providers.complete.provideCompletionItems(doc, { offset: item.source.indexOf("nam") + 2 });
  await reachedFinal;
  pendingRegistration.dispose();
  releaseFinal();
  assert.deepEqual(await completion, []);
});
