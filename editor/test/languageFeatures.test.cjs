"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const { createLanguageFeatures, registerLanguageFeatures, positionAt } = require("../src/languageFeatures");
const { bundle, document, options } = require("./featureFixtures.cjs");

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
