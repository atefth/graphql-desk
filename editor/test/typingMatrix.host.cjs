"use strict";
const assert = require("node:assert/strict");
const path = require("node:path");
const { spawnSync } = require("node:child_process");
const vscode = require("vscode");
const fs = require("node:fs/promises");

async function until(check, label, milliseconds = 30000) {
  const deadline = Date.now() + milliseconds;
  while (Date.now() < deadline) {
    if (await check()) return true;
    await new Promise(resolve => setTimeout(resolve, 100));
  }
  console.log(`NOT_OBSERVED: ${label}`);
  return false;
}

async function replace(document, text) {
  const edit = new vscode.WorkspaceEdit();
  edit.replace(document.uri, new vscode.Range(document.positionAt(0), document.positionAt(document.getText().length)), text);
  assert.equal(await vscode.workspace.applyEdit(edit), true);
}

async function replaceText(document, before, after) {
  const source = document.getText();
  const start = source.indexOf(before);
  assert.ok(start >= 0, `Expected source text ${before}`);
  const edit = new vscode.WorkspaceEdit();
  edit.replace(document.uri, new vscode.Range(document.positionAt(start), document.positionAt(start + before.length)), after);
  assert.equal(await vscode.workspace.applyEdit(edit), true);
}

function pythonVersion(python) {
  const result = spawnSync(python, ["-I", "-X", "utf8", "-c", "import sys; print(sys.version.split()[0])"], { encoding: "utf8" });
  assert.equal(result.status, 0, result.stderr);
  return result.stdout.trim();
}

exports.run = async () => {
  const suite = process.env.GRAPHQL_DESK_TEST_SUITE;
  const results = { host: vscode.version, pylance: {}, lifecycle: { invalidationAndRepair: {} } };
  let settingChanges;
  let diagnosticChanges;
  try {
    for (const id of ["ms-python.python", "ms-python.vscode-pylance"]) {
      const extension = vscode.extensions.getExtension(id);
      assert.ok(extension, `${id} must be installed in the isolated host`);
      await extension.activate();
      const version = extension.packageJSON.version;
      assert.equal(version, id === "ms-python.python" ? "2026.10.0" : "2026.4.1");
      results.pylance[id] = version;
    }

    const workspace = process.env.GRAPHQL_DESK_TEST_WORKSPACE;
    const consumerUri = vscode.Uri.file(path.join(workspace, "consumer.py"));
    const extension = vscode.extensions.getExtension("graphql-desk.graphql-desk-editor");
    assert.ok(extension, "GraphQL Desk extension must be loaded in the host");
    await extension.activate();
    const consumer = await vscode.workspace.openTextDocument(consumerUri);
    await vscode.window.showTextDocument(consumer, { preview: false });

    const uri = consumerUri;
    const document = consumer;
    const originalConsumerSource = document.getText();
    const position = name => {
      const offset = document.getText().indexOf(name) + 1;
      assert.ok(offset > 0, `Expected fixture variable ${name}`);
      return document.positionAt(offset);
    };
    const hover = async name => {
      const items = await vscode.commands.executeCommand("vscode.executeHoverProvider", uri, position(name)) || [];
      return items.flatMap(item => item.contents.map(content => typeof content === "string" ? content : content.value)).join("\n");
    };
    const lineDiagnostics = name => vscode.languages.getDiagnostics(uri)
      .filter(item => item.range.start.line === position(name).line).map(item => item.message);
    const inferredType = (name, text) => text.match(new RegExp(`\\(variable\\) ${name}: ([^\\n]+)`))?.[1] || "";
    const observed = {};
    if (process.env.GRAPHQL_DESK_TEST_SUITE === "mixedLists") {
      const ready = await until(async () => {
        observed.nullableList = await hover("nullable_list_value");
        observed.nullableItems = await hover("nullable_items_value");
        observed.nullableListType = inferredType("nullable_list_value", observed.nullableList);
        observed.nullableItemsType = inferredType("nullable_items_value", observed.nullableItems);
        observed.nullableListPositiveErrors = lineDiagnostics("nullable_list_ok");
        observed.nullableItemsPositiveErrors = lineDiagnostics("nullable_items_ok");
        observed.nullableListNegativeErrors = lineDiagnostics("nullable_list_bad");
        observed.nullableItemsNegativeErrors = lineDiagnostics("nullable_items_bad");
        return observed.nullableListType === "list[String] | None" &&
          observed.nullableItemsType === "list[String | None]" &&
          observed.nullableListPositiveErrors.length === 0 && observed.nullableItemsPositiveErrors.length === 0 &&
          observed.nullableListNegativeErrors.length > 0 && observed.nullableItemsNegativeErrors.length > 0;
      }, "Pylance distinguishes nullable list containers from nullable list items", 60000);
      assert.ok(ready, JSON.stringify(observed));
      results.pylance = { ...results.pylance, ...observed };
      const settings = JSON.parse(await fs.readFile(path.join(workspace, ".vscode", "settings.json"), "utf8"));
      results.lifecycle.configuredStubPath = settings["python.analysis.stubPath"];
      results.lifecycle.activeStubRoot = path.resolve(workspace, results.lifecycle.configuredStubPath);
      results.outcome = "mixed-list-nullability-observed";
      return;
    }
    if (process.env.GRAPHQL_DESK_TEST_SUITE === "fragmentTyping") {
      const ready = await until(async () => {
        observed.visibleName = await hover("visible_name");
        observed.visibleNameType = inferredType("visible_name", observed.visibleName);
        observed.hiddenNameErrors = lineDiagnostics("hidden_name");
        observed.hiddenNameRejected = observed.hiddenNameErrors.some(message => /"name" is not a defined key/.test(message));
        observed.positiveErrors = lineDiagnostics("fragment_name_ok");
        return observed.visibleNameType === "String" && observed.hiddenNameRejected &&
          observed.positiveErrors.length === 0;
      }, "Pylance masks spread-only fragment fields until the generated reader is used", 60000);
      assert.ok(ready, JSON.stringify(observed));
      results.pylance = { ...results.pylance, ...observed };
      const settings = JSON.parse(await fs.readFile(path.join(workspace, ".vscode", "settings.json"), "utf8"));
      results.lifecycle.configuredStubPath = settings["python.analysis.stubPath"];
      results.lifecycle.activeStubRoot = path.resolve(workspace, results.lifecycle.configuredStubPath);
      results.outcome = "fragment-masking-live-typing-observed";
      return;
    }
    if (process.env.GRAPHQL_DESK_TEST_SUITE === "externalMappings") {
      const ready = await until(async () => {
        observed.externalDate = await hover("mapped_date");
        observed.externalColor = await hover("mapped_color");
        observed.betaExternalDate = await hover("beta_mapped_date");
        observed.betaExternalColor = await hover("beta_mapped_color");
        observed.externalDateErrors = lineDiagnostics("mapped_date_bad");
        observed.externalColorErrors = lineDiagnostics("mapped_color_bad");
        observed.externalDateVariableErrors = lineDiagnostics("mapped_wrong_date");
        observed.externalColorVariableErrors = lineDiagnostics("mapped_wrong_color");
        observed.betaExternalDateErrors = lineDiagnostics("beta_mapped_date_bad");
        observed.betaExternalColorErrors = lineDiagnostics("beta_mapped_color_bad");
        observed.crossSchemaInputDiagnostics = {
          alphaFromBeta: lineDiagnostics("mapped_cross_input"),
          betaFromAlpha: lineDiagnostics("beta_cross_input"),
        };
        observed.crossSchemaResultDiagnostics = {
          alphaDateFromBeta: lineDiagnostics("mapped_cross_date_result"),
          betaDateFromAlpha: lineDiagnostics("beta_cross_date_result"),
          alphaColorFromBeta: lineDiagnostics("mapped_cross_color_result"),
          betaColorFromAlpha: lineDiagnostics("beta_cross_color_result"),
        };
        observed.externalPositiveErrors = [
          ...lineDiagnostics("mapped_date_ok"),
          ...lineDiagnostics("mapped_color_ok"),
          ...lineDiagnostics("beta_mapped_date_ok"),
          ...lineDiagnostics("beta_mapped_color_ok"),
          ...lineDiagnostics("mapped_result"),
          ...lineDiagnostics("beta_mapped_result"),
        ];
        observed.externalMappingDiagnostics = vscode.languages.getDiagnostics(uri)
          .filter(item => item.source === "GraphQL Desk" && /mapping-(?:unresolved|unverified)/.test(String(item.code)));
        return /ExternalDateOutput/.test(observed.externalDate) && /ExternalColor/.test(observed.externalColor) &&
          /ExternalDateOutput/.test(observed.betaExternalDate) && /ExternalColor/.test(observed.betaExternalColor) &&
          observed.externalDateErrors.length > 0 && observed.externalColorErrors.length > 0 &&
          observed.externalDateErrors.some(message => /AlphaDateOutput/.test(message)) &&
          observed.externalColorErrors.some(message => /AlphaColor/.test(message)) &&
          observed.externalDateVariableErrors.length > 0 && observed.externalColorVariableErrors.length > 0 &&
          observed.betaExternalDateErrors.length > 0 && observed.betaExternalColorErrors.length > 0 &&
          observed.betaExternalDateErrors.some(message => /BetaDateOutput/.test(message)) &&
          observed.betaExternalColorErrors.some(message => /BetaColor/.test(message)) &&
          Object.values(observed.crossSchemaInputDiagnostics).every(errors => errors.length > 0) &&
          Object.values(observed.crossSchemaResultDiagnostics).every(errors => errors.length > 0) &&
          observed.externalPositiveErrors.length === 0 && observed.externalMappingDiagnostics.length === 0;
      }, "Pylance keeps external scalar and enum mappings isolated between schemas", 60000);
      assert.ok(ready, JSON.stringify(observed));
      results.pylance = { ...results.pylance, ...observed };
      const settings = JSON.parse(await fs.readFile(path.join(workspace, ".vscode", "settings.json"), "utf8"));
      results.lifecycle.configuredStubPath = settings["python.analysis.stubPath"];
      results.lifecycle.activeStubRoot = path.resolve(workspace, results.lifecycle.configuredStubPath);
      results.outcome = "external-mappings-observed";
      return;
    }
    if (process.env.GRAPHQL_DESK_TEST_SUITE !== "parserOverlay") {
      const ready = await until(async () => {
        observed.nested = await hover("nested_value");
        observed.nullable = await hover("nullable_value");
        observed.nestedType = inferredType("nested_value", observed.nested);
        observed.nullableType = inferredType("nullable_value", observed.nullable);
        observed.nestedErrors = lineDiagnostics("nested_bad");
        observed.nullableErrors = lineDiagnostics("nullable_bad");
        observed.defaultsErrors = lineDiagnostics("defaults_bad");
        observed.defaultAcceptedErrors = lineDiagnostics("defaults_result");
        return observed.nestedType === "list[list[String]]" &&
          observed.nullableType === "list[String | None] | None" &&
          observed.nestedErrors.length > 0 && observed.nullableErrors.length > 0 &&
          observed.defaultsErrors.length > 0 && observed.defaultAcceptedErrors.length === 0;
      }, "Pylance live typing for aliases, nested list wrappers, nullable lists and defaulted variables", 60000);
      assert.ok(ready, JSON.stringify(observed));
      results.pylance = { ...results.pylance, ...observed };
    }

    const parserUri = vscode.Uri.file(path.join(workspace, "parser_consumer.py"));
    const parserDocument = await vscode.workspace.openTextDocument(parserUri);
    await vscode.window.showTextDocument(parserDocument, { preview: false });
    const parserPosition = () => parserDocument.positionAt(parserDocument.getText().indexOf("parsed_name") + 2);
    const parserHover = async () => {
      const items = await vscode.commands.executeCommand("vscode.executeHoverProvider", parserUri, parserPosition()) || [];
      return items.flatMap(item => item.contents.map(content => typeof content === "string" ? content : content.value)).join("\n");
    };
    await until(async () => {
      let settings;
      try { settings = JSON.parse(await fs.readFile(path.join(workspace, ".vscode", "settings.json"), "utf8")); }
      catch { return false; }
      if (typeof settings["python.analysis.stubPath"] !== "string") return false;
      const root = path.resolve(workspace, settings["python.analysis.stubPath"]);
      return Promise.all([
        fs.access(path.join(root, "graphql_desk-stubs", "__init__.pyi")),
        fs.readFile(path.join(root, "graphql_desk-stubs", "py.typed"), "utf8").then(text => assert.equal(text, "partial\n")),
      ]).then(() => true, () => false);
    }, "the worker publishes a schema-independent parser companion");
    assert.match(parserDocument.getText(), /^from graphql_desk import parse/m);
    const parserTyped = await until(async () => {
      const hover = await parserHover();
      results.pylance.parserName = hover;
      return /Literal\[['\"]ParserQuery['\"]\]/.test(hover);
    }, "ordinary graphql_desk.parse imports provide exact Pylance AST literal inference", 60000);
    assert.ok(parserTyped, results.pylance.parserName);
    assert.equal(parserDocument.isDirty, false, "GraphQL Desk does not rewrite the user's source");
    assert.equal(await fs.readFile(parserUri.fsPath, "utf8"),
      "from graphql_desk import parse\ndocument = parse('query ParserQuery { selected }')\nparsed_name = document.definitions[0].name.value\n");
    const packageExportsUri = vscode.Uri.file(path.join(workspace, "package_exports.py"));
    await fs.writeFile(packageExportsUri.fsPath, "from graphql_desk import gql, TypedDocument, parse\n");
    const packageExports = await vscode.workspace.openTextDocument(packageExportsUri);
    await vscode.window.showTextDocument(packageExports, { preview: false });
    const packageExportsReady = await until(async () => {
      const diagnostics = vscode.languages.getDiagnostics(packageExportsUri)
        .filter(item => /cannot import|unknown import symbol/i.test(item.message));
      const gqlOffset = packageExports.getText().indexOf("gql") + 1;
      const hovers = await vscode.commands.executeCommand("vscode.executeHoverProvider", packageExportsUri,
        packageExports.positionAt(gqlOffset)) || [];
      results.pylance.packageExports = { diagnostics: diagnostics.map(item => item.message), hoverCount: hovers.length };
      return diagnostics.length === 0 && hovers.length > 0;
    }, "partial parser stub preserves existing GraphQL Desk root exports", 30000);
    assert.ok(packageExportsReady, JSON.stringify(results.pylance.packageExports));
    results.pylance.parserLiteralInference = true;
    await vscode.window.showTextDocument(document, { preview: false });
    if (process.env.GRAPHQL_DESK_TEST_SUITE === "parserOverlay") {
      const configuration = vscode.workspace.getConfiguration("python", uri);
      results.lifecycle.configuredStubPath = configuration.get("analysis.stubPath");
      results.lifecycle.activeStubRoot = path.resolve(workspace, results.lifecycle.configuredStubPath);
      results.outcome = "parser-overlay-observed";
      return;
    }

    const multiSource = [
      "from graphql_desk import TypedDocument",
      "from graphql_desk_generated.default import graphql",
      "from typing import TypeVar",
      "R = TypeVar('R')",
      "V = TypeVar('V')",
      "def execute(document: TypedDocument[R, V], variables: V) -> R: ...",
      "text_doc = graphql('query Text { selected } query Count($limit: Int!) { count(limit: $limit) }', operation='Text')",
      "count_doc = graphql('query Text { selected } query Count($limit: Int!) { count(limit: $limit) }', operation='Count')",
      "text_name = text_doc.operation_name",
      "count_name = count_doc.operation_name",
      "text_result = execute(text_doc, {})",
      "text_value = text_result['selected']",
      "count_result = execute(count_doc, {'limit': 1})",
      "count_value = count_result['count']",
      "text_bad_variables = execute(text_doc, {'limit': 1})",
      "count_bad_variables = execute(count_doc, {})",
      "text_bad_field = text_result['count']",
      "count_bad_field = count_result['selected']",
      "",
    ].join("\n");
    const capturePublisherSnapshot = async () => {
      const settings = JSON.parse(await fs.readFile(path.join(workspace, ".vscode", "settings.json"), "utf8"));
      const generationRoot = path.resolve(workspace, settings["python.analysis.stubPath"]);
      const complete = JSON.parse(await fs.readFile(path.join(generationRoot, "complete.json"), "utf8"));
      const generatedRoot = path.join(generationRoot, "graphql_desk_generated");
      const factory = await fs.readFile(path.join(generatedRoot, "default.pyi"), "utf8").catch(() => "");
      const source = await fs.readFile(path.join(generatedRoot, "sources", "operation_semantics.pyi"), "utf8").catch(() => "");
      return {
        generation: { id: complete.id, sequence: complete.sequence, analysisId: complete.analysisId },
        operationManifest: complete.operationManifest.map(project => ({
          project: project.project,
          operations: project.operations.map(({ owner, binding, operation_name, status, declaration_path, declaration_symbol, source_exports }) =>
            ({ owner, binding, operation_name, status, declaration_path, declaration_symbol, source_exports })),
        })),
        sourceArtifacts: complete.files.map(file => file.path).filter(file => file.includes("/sources/") || file.endsWith("/default.pyi")),
        factoryOverloads: factory.split("\n").filter(line => /Text|Count|Inputs/.test(line)),
        sourceDeclarations: source.split("\n").filter(line => /Text|Count|Inputs|Filter|Choice/.test(line)),
      };
    };
    await replace(document, multiSource);
    const multiUri = uri;
    const multiDocument = document;
    const multiPosition = marker => multiDocument.positionAt(multiSource.indexOf(marker) + 1);
    const multiHover = async marker => {
      const items = await vscode.commands.executeCommand("vscode.executeHoverProvider", multiUri, multiPosition(marker)) || [];
      return items.flatMap(item => item.contents.map(content => typeof content === "string" ? content : content.value)).join("\n");
    };
    const multiLine = marker => multiSource.slice(0, multiSource.indexOf(marker)).split("\n").length - 1;
    const multiDiagnostics = marker => vscode.languages.getDiagnostics(multiUri)
      .filter(item => item.range.start.line === multiLine(marker));
    const multiProbe = {};
    const multiReady = await until(async () => {
      multiProbe.textName = await multiHover("text_name");
      multiProbe.countName = await multiHover("count_name");
      multiProbe.textValue = await multiHover("text_value");
      multiProbe.countValue = await multiHover("count_value");
      multiProbe.textVariableErrors = multiDiagnostics("text_bad_variables").map(item => item.message);
      multiProbe.countVariableErrors = multiDiagnostics("count_bad_variables").map(item => item.message);
      multiProbe.textFieldErrors = multiDiagnostics("text_bad_field").map(item => item.message);
      multiProbe.countFieldErrors = multiDiagnostics("count_bad_field").map(item => item.message);
      return /Text/.test(multiProbe.textName) && /Count/.test(multiProbe.countName) &&
        /String|str/.test(multiProbe.textValue) && /Int|int/.test(multiProbe.countValue) &&
        multiProbe.textVariableErrors.length > 0 && multiProbe.countVariableErrors.length > 0 &&
        multiProbe.textFieldErrors.length > 0 && multiProbe.countFieldErrors.length > 0;
    }, "Pylance infers isolated results and variables for same-source operation selectors", 60000);
    if (!multiReady) {
      try { results.pylance.publisherSnapshot = await capturePublisherSnapshot(); }
      catch (error) { results.pylance.publisherSnapshotError = String(error); }
    }
    assert.ok(multiReady, JSON.stringify({ ...multiProbe, publisherSnapshot: results.pylance.publisherSnapshot }));
    results.pylance.multiOperationIsolation = multiProbe;
    await replace(document, originalConsumerSource);
    const multiRestoreReady = await until(async () => {
      results.pylance.multiOperationRestore = await hover("other_id");
      return /\bID\b/.test(results.pylance.multiOperationRestore);
    }, "Pylance restores prior document typing after the multi-operation edit", 60000);
    assert.ok(multiRestoreReady, JSON.stringify(results.pylance.multiOperationRestore));

    const semanticsSchema = await vscode.workspace.openTextDocument(vscode.Uri.file(path.join(workspace, "schema.graphql")));
    await replace(semanticsSchema, [
      "directive @defer(if: Boolean = true) on INLINE_FRAGMENT | FRAGMENT_SPREAD",
      "directive @required on FIELD",
      "directive @_required on FIELD",
      "directive @optional on FIELD",
      "directive @_optional on FIELD",
      "interface Node { id: ID! }",
      "type User implements Node { id: ID! name: String! }",
      "type Org implements Node { id: ID! title: String! }",
      "union Search = User | Org",
      "enum Color { RED BLUE }",
      "input Filter { term: String! nested: Filter tags: [String!]! limit: Int! = 5 color: Color }",
      "input Choice @oneOf { byId: ID byName: String }",
      "type Query { selected: String! count(limit: Int!): Int! user: User! nested: [[String!]!]! nullable: [String] node: Node! search: [Search!]! filtered(filter: Filter!, choice: Choice!): [User!]! }",
      "",
    ].join("\n"));
    const semanticPath = path.join(workspace, "operation_semantics.py");
    const semanticDiskBaseline = await fs.readFile(semanticPath, "utf8");
    assert.equal(semanticDiskBaseline, "", "The semantic fixture exists before live typing edits it");
    const semanticSource = [
      "from graphql_desk import TypedDocument",
      "from graphql_desk_generated.default import graphql",
      "from graphql_desk_generated.sources.operation_semantics import AbstractResult",
      "from typing import TypeVar",
      "R = TypeVar('R')",
      "V = TypeVar('V')",
      "def execute(document: TypedDocument[R, V], variables: V) -> R: ...",
      "abstract = graphql('query Abstract { node { __typename ... on User { name } ... on Org { title } } search { kind: __typename ... on User { name } ... on Org { title } } }')",
      "abstract_result = execute(abstract, {})",
      "node = abstract_result['node']",
      "if node['__typename'] == 'User':",
      "    user_name = node['name']",
      "    wrong_title = node['title']",
      "else:",
      "    org_title = node['title']",
      "    wrong_name = node['name']",
      "search_item = abstract_result['search'][0]",
      "if search_item['kind'] == 'User':",
      "    aliased_user_name = search_item['name']",
      "    aliased_wrong_title = search_item['title']",
      "without_implicit_typename: AbstractResult = {'node': {'__typename': 'User', 'name': 'x'}, 'search': [{'kind': 'User', 'name': 'x'}]}",
      "inputs = graphql('query Inputs($filter: Filter!, $choice: Choice!) { filtered(filter: $filter, choice: $choice) { name } }')",
      "inputs_result = execute(inputs, {'filter': {'term': 'x', 'tags': ['a'], 'nested': {'term': 'y', 'tags': []}, 'color': 'RED'}, 'choice': {'byId': '1'}})",
      "input_name = inputs_result['filtered'][0]['name']",
      "input_name_ok: str = input_name",
      "valid_input_literal = graphql('query ValidInputLiteral { filtered(filter: {term: \\\"x\\\", tags: []}, choice: {byName: \\\"x\\\"}) { name } }')",
      "invalid_input_literal = graphql('query InvalidInputLiteral { filtered(filter: {term: \\\"x\\\", tags: []}, choice: {}) { name } }')",
      "invalid_input_extra_literal = graphql('query InvalidInputExtraLiteral { filtered(filter: {term: \\\"x\\\", tags: []}, choice: {byId: \\\"1\\\", unknown: \\\"x\\\"}) { name } }')",
      "input_choice_empty = execute(inputs, {'filter': {'term': 'x', 'tags': []}, 'choice': {}})",
      "input_choice_multiple = execute(inputs, {'filter': {'term': 'x', 'tags': []}, 'choice': {'byId': '1', 'byName': 'x'}})",
      "input_choice_extra = execute(inputs, {'filter': {'term': 'x', 'tags': []}, 'choice': {'byId': '1', 'extra': 'x'}})",
      "input_choice_null = execute(inputs, {'filter': {'term': 'x', 'tags': []}, 'choice': {'byId': None}})",
      "input_filter_missing = execute(inputs, {'filter': {'tags': []}, 'choice': {'byId': '1'}})",
      "input_filter_extra = execute(inputs, {'filter': {'term': 'x', 'tags': [], 'unknown': 1}, 'choice': {'byId': '1'}})",
      "input_filter_bad_list = execute(inputs, {'filter': {'term': 'x', 'tags': [None]}, 'choice': {'byId': '1'}})",
      "input_filter_default_null = execute(inputs, {'filter': {'term': 'x', 'tags': [], 'limit': None}, 'choice': {'byId': '1'}})",
      "input_filter_nullable_none = execute(inputs, {'filter': {'term': 'x', 'tags': [], 'nested': None}, 'choice': {'byName': 'x'}})",
      "input_filter_bad_enum = execute(inputs, {'filter': {'term': 'x', 'tags': [], 'color': 'GREEN'}, 'choice': {'byId': '1'}})",
      "scalar_value = graphql.scalar('String', 'identity')",
      "scalar_value_ok: str = scalar_value",
      "scalar_bad = graphql.scalar('String', 1.5)",
      "directives = graphql('query DirectiveSemantics($show: Boolean!) { required: nullable @required required_alias: nullable @_required optional: selected @optional optional_alias: selected @_optional user { id ... @defer(if: $show) { name } } }')",
      "directives_result = execute(directives, {'show': True})",
      "directive_required = directives_result['required']",
      "directive_required_alias = directives_result['required_alias']",
      "directive_optional = directives_result['optional']",
      "directive_optional_alias = directives_result['optional_alias']",
      "directive_user = directives_result['user']",
      "directive_required_ok: list[str | None] = directive_required",
      "directive_required_alias_ok: list[str | None] = directive_required_alias",
      "directive_optional_ok: str | None = directive_optional",
      "directive_optional_alias_ok: str | None = directive_optional_alias",
      "if 'name' in directive_user:",
      "    directive_user_name = directive_user['name']",
      "directive_required_bad: None = directive_required",
      "directive_required_alias_bad: None = directive_required_alias",
      "directive_optional_bad: str = directive_optional",
      "directive_optional_alias_bad: str = directive_optional_alias",
      "",
    ].join("\n");
    const semanticUri = vscode.Uri.file(semanticPath);
    const semanticDocument = await vscode.workspace.openTextDocument(semanticUri);
    await vscode.window.showTextDocument(semanticDocument, { preview: false });
    await replace(semanticDocument, semanticSource);
    const semanticPosition = name => {
      const offset = semanticDocument.getText().indexOf(name) + 1;
      assert.ok(offset > 0, `Expected semantic fixture variable ${name}`);
      return semanticDocument.positionAt(offset);
    };
    const semanticHover = async name => {
      const items = await vscode.commands.executeCommand("vscode.executeHoverProvider", semanticUri, semanticPosition(name)) || [];
      return items.flatMap(item => item.contents.map(content => typeof content === "string" ? content : content.value)).join("\n");
    };
    const semanticErrors = name => vscode.languages.getDiagnostics(semanticUri)
      .filter(item => item.range.start.line === semanticPosition(name).line).map(item => item.message);
    const inputContractReady = await until(async () => {
      results.pylance.inputName = await semanticHover("input_name");
      results.pylance.inputPositiveErrors = semanticErrors("input_name_ok");
      results.pylance.inputDefaultNullErrors = semanticErrors("input_filter_default_null");
      results.pylance.inputNullableNoneErrors = semanticErrors("input_filter_nullable_none");
      results.pylance.inputScalarPositiveErrors = semanticErrors("scalar_value_ok");
      results.pylance.inputLiteralPositiveErrors = semanticErrors("valid_input_literal");
      results.pylance.inputLiteralNegativeErrors = semanticErrors("invalid_input_literal");
      results.pylance.inputLiteralExtraErrors = semanticErrors("invalid_input_extra_literal");
      results.pylance.inputNegativeErrors = Object.fromEntries([
        "input_choice_empty", "input_choice_multiple", "input_choice_extra", "input_choice_null", "input_filter_missing",
        "input_filter_extra", "input_filter_bad_list", "input_filter_bad_enum",
        "scalar_bad",
      ].map(name => [name, semanticErrors(name)]));
      return /: str\b|: String\b/.test(results.pylance.inputName) &&
        results.pylance.inputPositiveErrors.length === 0 &&
        results.pylance.inputDefaultNullErrors.length === 0 &&
        results.pylance.inputNullableNoneErrors.length === 0 &&
        results.pylance.inputScalarPositiveErrors.length === 0 &&
        results.pylance.inputLiteralPositiveErrors.length === 0 &&
        results.pylance.inputLiteralNegativeErrors.some(message => /oneof|one of|exactly one/i.test(message)) &&
        results.pylance.inputLiteralExtraErrors.some(message => /unknown|not defined|field/i.test(message)) &&
        Object.values(results.pylance.inputNegativeErrors).every(errors => errors.length > 0);
    }, "Pylance checks recursive inputs, defaults, enums, oneOf alternatives and schema-bound scalar helpers", 60000);
    if (!inputContractReady) {
      try { results.pylance.publisherSnapshot = await capturePublisherSnapshot(); }
      catch (error) { results.pylance.publisherSnapshotError = String(error); }
    }
    assert.ok(inputContractReady, JSON.stringify(results.pylance));
    results.pylance.inputContracts = true;
    const directiveReady = await until(async () => {
      results.pylance.directiveTypes = {
        required: await semanticHover("directive_required"),
        requiredAlias: await semanticHover("directive_required_alias"),
        optional: await semanticHover("directive_optional"),
        optionalAlias: await semanticHover("directive_optional_alias"),
        deferredName: await semanticHover("directive_user_name"),
      };
      results.pylance.directivePositiveErrors = [
        "directive_required_ok", "directive_required_alias_ok",
        "directive_optional_ok", "directive_optional_alias_ok",
      ].flatMap(semanticErrors);
      results.pylance.directiveNegativeErrors = [
        "directive_required_bad", "directive_required_alias_bad",
        "directive_optional_bad", "directive_optional_alias_bad",
      ].map(name => [name, semanticErrors(name)]);
      let settings;
      try { settings = JSON.parse(await fs.readFile(path.join(workspace, ".vscode", "settings.json"), "utf8")); }
      catch { return false; }
      const root = path.resolve(workspace, settings["python.analysis.stubPath"]);
      let sourceDeclarations = "";
      try { sourceDeclarations = await fs.readFile(path.join(root, "graphql_desk_generated", "sources", "operation_semantics.pyi"), "utf8"); }
      catch { return false; }
      const resultModule = sourceDeclarations.match(/^from ([\w.]+) import GraphqlDeskResult as DirectivesResult$/m)?.[1];
      if (!resultModule) return false;
      let stub = "";
      try { stub = await fs.readFile(path.join(root, ...resultModule.split(".")) + ".pyi", "utf8"); }
      catch { return false; }
      const deferredVariantNames = stub.match(/^GraphqlDeskResult_user = (.+)$/m)?.[1].split(" | ") ?? [];
      const deferredVariants = deferredVariantNames.map(name =>
        stub.match(new RegExp(`^${name} = TypedDict\\([\\s\\S]*?\\}, closed=True\\)`, "m"))?.[0] ?? "");
      results.pylance.deferredPresenceVariants = deferredVariants;
      return /list\[(?:str|String) \| None\]/.test(results.pylance.directiveTypes.required) &&
        /list\[(?:str|String) \| None\]/.test(results.pylance.directiveTypes.requiredAlias) &&
        /(?:str|String) \| None/.test(results.pylance.directiveTypes.optional) &&
        /(?:str|String) \| None/.test(results.pylance.directiveTypes.optionalAlias) &&
        /: str\b|: String\b/.test(results.pylance.directiveTypes.deferredName) &&
        deferredVariants.length === 2 && deferredVariants.every(variant => variant.includes("'id': 'ID'")) &&
        deferredVariants.some(variant => variant.includes("'name': 'String'")) &&
        deferredVariants.some(variant => !variant.includes("'name': 'String'")) &&
        results.pylance.directivePositiveErrors.length === 0 &&
        results.pylance.directiveNegativeErrors.every(([, errors]) => errors.length > 0);
    }, "Pylance honors @defer and required/optional nullability overrides", 60000);
    assert.ok(directiveReady, JSON.stringify(results.pylance));
    results.pylance.directiveSemantics = true;
    const semanticReady = await until(async () => {
      results.pylance.abstractName = await semanticHover("user_name");
      results.pylance.abstractTitle = await semanticHover("org_title");
      results.pylance.abstractNegative = semanticErrors("wrong_title");
      results.pylance.abstractNegativeOther = semanticErrors("wrong_name");
      results.pylance.abstractAliasName = await semanticHover("aliased_user_name");
      results.pylance.abstractAliasNegative = semanticErrors("aliased_wrong_title");
      results.pylance.abstractNoFabricatedKeyErrors = semanticErrors("without_implicit_typename");
      let settings;
      try { settings = JSON.parse(await fs.readFile(path.join(workspace, ".vscode", "settings.json"), "utf8")); }
      catch { return false; }
      const root = path.resolve(workspace, settings["python.analysis.stubPath"]);
      const sourceStub = path.join(root, "graphql_desk_generated", "sources", "operation_semantics.pyi");
      let sourceDeclarations = "";
      try { sourceDeclarations = await fs.readFile(sourceStub, "utf8"); } catch { return false; }
      const resultModule = sourceDeclarations.match(/^from ([\w.]+) import GraphqlDeskResult as AbstractResult$/m)?.[1];
      if (!resultModule) return false;
      const generated = path.join(root, ...resultModule.split(".")) + ".pyi";
      let stub;
      try { stub = await fs.readFile(generated, "utf8"); } catch { return false; }
      const sourceExcerpt = sourceDeclarations.split("\n").filter(line => /GraphqlDeskResult|def graphql/.test(line)).join("\n");
      const resultExcerpt = stub.split("\n").filter(line => /TypedDict|Literal\[|GraphqlDeskResult|__graphql_desk_variant/.test(line)).slice(0, 40).join("\n");
      results.pylance.abstractArtifacts = { root, resultModule, sourceExcerpt, resultExcerpt,
        hasExpectedAlternatives: stub.includes("Literal['User']") && stub.includes("Literal['Org']"),
        hasExpectedAlias: stub.includes("'kind': \"Literal['User']\"") };
      results.pylance.abstractGenerated = stub.includes("Literal['User']") && stub.includes("Literal['Org']") &&
        stub.includes("'kind': \"Literal['User']\"") && stub.includes("'__typename': NotRequired");
      return /: str\b|: String\b/.test(results.pylance.abstractName) &&
        /: str\b|: String\b/.test(results.pylance.abstractTitle) &&
        results.pylance.abstractNegative.length > 0 && results.pylance.abstractNegativeOther.length > 0 &&
        /: str\b|: String\b/.test(results.pylance.abstractAliasName) &&
        results.pylance.abstractAliasNegative.length > 0 &&
        results.pylance.abstractNoFabricatedKeyErrors.length === 0 && results.pylance.abstractGenerated;
    }, "Pylance consumes abstract alternatives, both typename discriminators and optional type aids", 60000);
    if (!semanticReady) {
      try {
        await vscode.commands.executeCommand("python.analysis.restartLanguageServer");
        results.pylance.abstractRecoveredAfterRestart = await until(async () => {
          results.pylance.abstractNameAfterRestart = await semanticHover("user_name");
          results.pylance.abstractTitleAfterRestart = await semanticHover("org_title");
          results.pylance.abstractErrorsAfterRestart = semanticErrors("wrong_title");
          return /: str\b|: String\b/.test(results.pylance.abstractNameAfterRestart) &&
            /: str\b|: String\b/.test(results.pylance.abstractTitleAfterRestart) &&
            results.pylance.abstractErrorsAfterRestart.length > 0;
        }, "diagnose whether Pylance restart recovers abstract result typing", 30000);
      } catch (error) {
        results.pylance.abstractRestartError = String(error);
      }
    }
    assert.ok(semanticReady, JSON.stringify(results.pylance));
    results.pylance.abstractLiveTyping = true;
    const conditionalSource = [
      "conditional = graphql('query Conditional($show: Boolean!) { selected @include(if: $show) nullable @skip(if: $show) node { id __typename ... on User @include(if: $show) { name } ... on Org @include(if: $show) { title } } }')",
      "conditional_result = execute(conditional, {'show': True})",
      "conditional_selected = conditional_result['selected']",
      "conditional_nullable = conditional_result['nullable']",
      "conditional_node = conditional_result['node']",
      "if conditional_node['__typename'] == 'User' and 'name' in conditional_node:",
      "    conditional_user = conditional_node['name']",
      "    conditional_id = conditional_node['id']",
      "    conditional_wrong_title = conditional_node['title']",
      "",
    ].join("\n");
    await replace(semanticDocument, semanticSource + conditionalSource);
    const conditionalReady = await until(async () => {
      let settings;
      try { settings = JSON.parse(await fs.readFile(path.join(workspace, ".vscode", "settings.json"), "utf8")); }
      catch { return false; }
      const root = path.resolve(workspace, settings["python.analysis.stubPath"]);
      const sourceStubPath = path.join(root, "graphql_desk_generated", "sources", "operation_semantics.pyi");
      let sourceDeclarations = "";
      try { sourceDeclarations = await fs.readFile(sourceStubPath, "utf8"); } catch { return false; }
      const resultModule = sourceDeclarations.match(/^from ([\w.]+) import GraphqlDeskResult as ConditionalResult$/m)?.[1];
      if (!resultModule) return false;
      const generated = path.join(root, ...resultModule.split(".")) + ".pyi";
      let stub = "";
      try { stub = await fs.readFile(generated, "utf8"); } catch { return false; }
      if (!stub.includes("'selected': NotRequired['String']") ||
          !stub.includes("'nullable': NotRequired['list[String | None] | None']")) return false;
      const userVariants = [...stub.matchAll(/__graphql_desk_variant_\d+ = TypedDict\([\s\S]*?\}, closed=True\)/g)]
        .map(match => match[0]).filter(variant => variant.includes(`Literal['User']`));
      results.pylance.conditionalGenerated = stub.includes("'selected': NotRequired['String']") &&
        stub.includes("'nullable': NotRequired['list[String | None] | None']") &&
        userVariants.some(variant => variant.includes("'name': 'String'")) &&
        userVariants.some(variant => !variant.includes("'name': 'String'"));
      if (!results.pylance.conditionalGenerated) return false;
      results.pylance.conditionalSelected = await semanticHover("conditional_selected");
      results.pylance.conditionalNullable = await semanticHover("conditional_nullable");
      results.pylance.conditionalUser = await semanticHover("conditional_user");
      results.pylance.conditionalId = await semanticHover("conditional_id");
      results.pylance.conditionalNegative = semanticErrors("conditional_wrong_title");
      return /: str\b|: String\b/.test(results.pylance.conditionalSelected) &&
        /list\[String \| None\] \| None/.test(results.pylance.conditionalNullable) &&
        /: str\b|: String\b/.test(results.pylance.conditionalUser) &&
        /: str\b|: ID\b/.test(results.pylance.conditionalId) &&
        results.pylance.conditionalNegative.length > 0 &&
        results.pylance.conditionalGenerated;
    }, "an unsaved GraphQL edit publishes conditional-presence and nullable-list types", 60000);
    assert.ok(conditionalReady, JSON.stringify(results.pylance));
    assert.equal(semanticDocument.isDirty, true, "GraphQL Desk preserves the unsaved Python source edit");
    assert.equal(await fs.readFile(semanticPath, "utf8"), semanticDiskBaseline,
      "Live operation typing does not write the user's Python source file");
    results.pylance.conditionalLiveTyping = true;
    const semanticSettings = JSON.parse(await fs.readFile(path.join(workspace, ".vscode", "settings.json"), "utf8"));
    const semanticRoot = path.resolve(workspace, semanticSettings["python.analysis.stubPath"]);
    const runtimeCheck = spawnSync(process.env.GRAPHQL_DESK_PYTHON, ["-c", [
      "from graphql_desk_generated.sources.operation_semantics import unsafe_read_abstract_result",
      "data = {'node': {'__typename': 'User', 'name': 'x'}, 'search': [{'kind': 'User', 'name': 'x'}]}",
      "result = unsafe_read_abstract_result(data)",
      "assert result is data",
      "assert '__typename' not in result['search'][0]",
    ].join("\n")], {
      cwd: path.dirname(workspace),
      env: { ...process.env, PYTHONPATH: [semanticRoot, path.resolve(__dirname, "..", "..", "src")].join(path.delimiter) },
      encoding: "utf8",
    });
    assert.equal(runtimeCheck.status, 0, runtimeCheck.stderr);
    results.pylance.aliasedTypenamePreservesRuntimeIdentity = true;
    await replace(semanticDocument, semanticDiskBaseline);
    assert.equal(await semanticDocument.save(), true, "The temporary typing fixture returns to its saved baseline");
    await vscode.commands.executeCommand("workbench.action.closeActiveEditor");
    await fs.unlink(semanticPath);
    await vscode.window.showTextDocument(document, { preview: false });
    const temporaryCompanionRemoved = await until(async () => {
      let settings;
      try { settings = JSON.parse(await fs.readFile(path.join(workspace, ".vscode", "settings.json"), "utf8")); }
      catch { return false; }
      const root = path.resolve(workspace, settings["python.analysis.stubPath"]);
      return fs.access(path.join(root, "graphql_desk_generated", "sources", "operation_semantics.pyi"))
        .then(() => false, () => true);
    }, "temporary abstract/conditional fixture leaves the selected generation", 60000);
    assert.ok(temporaryCompanionRemoved);

    const otherIdPosition = position("other_id");
    const otherIdHover = async () => {
      const items = await vscode.commands.executeCommand("vscode.executeHoverProvider", uri, otherIdPosition) || [];
      return items.flatMap(item => item.contents.map(content => typeof content === "string" ? content : content.value)).join("\n");
    };
    const selectedPosition = document.positionAt(document.getText().indexOf("selected = ") + 2);
    const selectedHover = async () => {
      const items = await vscode.commands.executeCommand("vscode.executeHoverProvider", uri, selectedPosition) || [];
      return items.flatMap(item => item.contents.map(content => typeof content === "string" ? content : content.value)).join("\n");
    };
    const selectedStubPaths = [];
    const typingDiagnostics = [];
    settingChanges = vscode.workspace.onDidChangeConfiguration(event => {
      if (event.affectsConfiguration("python.analysis.stubPath", consumerUri)) {
        selectedStubPaths.push(vscode.workspace.getConfiguration("python", consumerUri).get("analysis.stubPath", ""));
      }
    });
    diagnosticChanges = vscode.languages.onDidChangeDiagnostics(event => {
      if (event.uris.some(changed => changed.toString() === uri.toString())) {
        typingDiagnostics.push(vscode.languages.getDiagnostics(uri).map(item => ({ source: item.source, message: item.message })));
      }
    });
    const observeQuarantine = async (label, after, selectionOffset) => {
      const deadline = Date.now() + 30000;
      let observed;
      while (Date.now() < deadline) {
        for (const setting of selectedStubPaths.slice(selectionOffset)) {
          try {
            if (typeof setting !== "string" || !setting) continue;
            const selectedGeneration = path.resolve(workspace, setting);
            const manifest = JSON.parse(await fs.readFile(path.join(selectedGeneration, "complete.json"), "utf8"));
            const quarantinedAt = Number(manifest.analysisId?.slice("quarantine-".length));
            if (!Number.isFinite(quarantinedAt) || quarantinedAt < after) continue;
            const operations = Object.fromEntries(manifest.operationManifest.flatMap(group => group.operations.map(item => [item.binding, item])));
            const declaration = operations.doc?.declaration_path;
            const stub = declaration ? await fs.readFile(path.join(selectedGeneration, ...declaration.split("/")), "utf8") : "";
            observed = { selectedGeneration, analysisId: manifest.analysisId, targetStatus: operations.doc?.status,
              siblingStatus: operations.other_doc?.status, targetUsesUnavailableObject: /FixtureDocument\[object, object, object\]/.test(stub) };
            if (observed.targetStatus === "unavailable" && observed.siblingStatus === "precise" && observed.targetUsesUnavailableObject) return observed;
          } catch { /* Wait for the complete selected quarantine generation. */ }
        }
        await new Promise(resolve => setTimeout(resolve, 10));
      }
      assert.fail(`${label} did not select a targeted quarantine generation: ${JSON.stringify(observed)}`);
    };
    const schemaDocument = await vscode.workspace.openTextDocument(vscode.Uri.file(path.join(workspace, "schema.graphql")));
    const originalSchema = schemaDocument.getText();
    const schemaBoolean = originalSchema.replace("selected: String!", "selected: Boolean!");
    assert.notEqual(schemaBoolean, originalSchema, "Schema fixture must contain the selected String field");
    await replace(schemaDocument, schemaBoolean);
    results.lifecycle.invalidationAndRepair.schemaInvalidation = await until(async () => {
      const selectedType = await selectedHover();
      const unaffectedType = await otherIdHover();
      results.lifecycle.invalidationAndRepair.schemaInvalidationProbe = { selectedType, unaffectedType };
      return /Boolean|bool/.test(selectedType) && /\bID\b/.test(unaffectedType);
    }, "schema edit refreshes the selected operation while its sibling keeps the ID type", 60000);
    assert.ok(results.lifecycle.invalidationAndRepair.schemaInvalidation,
      JSON.stringify(results.lifecycle.invalidationAndRepair.schemaInvalidationProbe));
    await replace(schemaDocument, originalSchema);
    results.lifecycle.invalidationAndRepair.schemaRepair = await until(async () => {
      const selectedType = await selectedHover();
      const unaffectedType = await otherIdHover();
      results.lifecycle.invalidationAndRepair.schemaRepairProbe = { selectedType, unaffectedType };
      return /String/.test(selectedType) && /\bID\b/.test(unaffectedType);
    }, "schema repair restores the selected type without disturbing its sibling", 60000);
    assert.ok(results.lifecycle.invalidationAndRepair.schemaRepair,
      JSON.stringify(results.lifecycle.invalidationAndRepair.schemaRepairProbe));

    const finalSchema = originalSchema.replace("selected: String!", "selected: Int!");
    const supersededSchema = originalSchema.replace("selected: String!", "selected: Boolean!");
    assert.notEqual(finalSchema, originalSchema, "Schema fixture must contain the selected String field");
    assert.notEqual(supersededSchema, originalSchema);
    const previousStubPath = vscode.workspace.getConfiguration("python", consumerUri).get("analysis.stubPath", "");
    await replace(schemaDocument, supersededSchema);
    await replace(schemaDocument, finalSchema);
    results.lifecycle.finalGenerationAfterRapidEdits = await until(async () => {
      const selectedType = await selectedHover();
      const unaffectedType = await otherIdHover();
      let settings;
      try { settings = JSON.parse(await fs.readFile(path.join(workspace, ".vscode", "settings.json"), "utf8")); }
      catch { return false; }
      const setting = settings["python.analysis.stubPath"];
      if (typeof setting !== "string" || !setting || setting === previousStubPath) return false;
      const selectedRoot = path.resolve(workspace, setting);
      let manifest;
      try { manifest = JSON.parse(await fs.readFile(path.join(selectedRoot, "complete.json"), "utf8")); }
      catch { return false; }
      const operations = Object.fromEntries(manifest.operationManifest.flatMap(group => group.operations.map(item => [item.binding, item])));
      const declaration = operations.doc?.declaration_path;
      let stub = "";
      try { stub = declaration ? await fs.readFile(path.join(selectedRoot, ...declaration.split("/")), "utf8") : ""; }
      catch { return false; }
      results.lifecycle.finalGenerationAfterRapidEditsProbe = { selectedType, unaffectedType,
        selectedStatus: operations.doc?.status, siblingStatus: operations.other_doc?.status,
        finalTypeInStub: /['\"]selected['\"]:\s*['\"]?Int\b/.test(stub), selectedRoot };
      return /\bint\b|\bInt\b/.test(selectedType) && /\bID\b/.test(unaffectedType) &&
        operations.doc?.status === "precise" && operations.other_doc?.status === "precise" &&
        results.lifecycle.finalGenerationAfterRapidEditsProbe.finalTypeInStub;
    }, "rapid successive schema edits settle on the final Pylance types while the sibling stays precise", 60000);
    assert.ok(results.lifecycle.finalGenerationAfterRapidEdits,
      JSON.stringify(results.lifecycle.finalGenerationAfterRapidEditsProbe));
    await replace(schemaDocument, originalSchema);
    const rapidEditRestore = await until(async () => /\bString\b/.test(await selectedHover()) && /\bID\b/.test(await otherIdHover()),
      "restoring the original schema restores both operation types", 60000);
    assert.ok(rapidEditRestore);

    const configDocument = await vscode.workspace.openTextDocument(vscode.Uri.file(path.join(workspace, "pyproject.toml")));
    const originalConfig = configDocument.getText();
    const mappedConfig = originalConfig.replace("[tool.graphql-desk]\n",
      '[tool.graphql-desk]\nscalars = { String = { input = "builtins.str", output = "builtins.int" } }\n');
    assert.notEqual(mappedConfig, originalConfig, "Config fixture must have the GraphQL Desk table");
    await replace(configDocument, mappedConfig);
    results.lifecycle.invalidationAndRepair.configInvalidation = await until(async () => {
      const selectedType = await selectedHover();
      const unaffectedType = await otherIdHover();
      results.lifecycle.invalidationAndRepair.configInvalidationProbe = { selectedType, unaffectedType };
      return /StringOutput|\bint\b/.test(selectedType) && /\bID\b/.test(unaffectedType);
    }, "scalar mapping edit refreshes generated result typing and preserves the independent operation", 60000);
    assert.ok(results.lifecycle.invalidationAndRepair.configInvalidation,
      JSON.stringify(results.lifecycle.invalidationAndRepair.configInvalidationProbe));
    await replace(configDocument, originalConfig);
    results.lifecycle.invalidationAndRepair.configRepair = await until(async () => {
      const selectedType = await selectedHover();
      const unaffectedType = await otherIdHover();
      results.lifecycle.invalidationAndRepair.configRepairProbe = { selectedType, unaffectedType };
      return /\bString\b/.test(selectedType) && !/StringOutput|\bint\b/.test(selectedType) && /\bID\b/.test(unaffectedType);
    }, "config repair restores generated result typing and preserves the independent operation", 60000);
    assert.ok(results.lifecycle.invalidationAndRepair.configRepair,
      JSON.stringify(results.lifecycle.invalidationAndRepair.configRepairProbe));

    const originalConsumer = document.getText();
    const invalidConsumer = originalConsumer.replace("{ selected count(limit: $limit) }", "{ missing count(limit: $limit) }");
    assert.notEqual(invalidConsumer, originalConsumer, "Consumer fixture must contain the selected operation field");
    const sourceSelectionOffset = selectedStubPaths.length;
    const sourceDiagnosticOffset = typingDiagnostics.length;
    const sourceQuarantineStarted = Date.now();
    await replaceText(document, "{ selected count(limit: $limit) }", "{ missing count(limit: $limit) }");
    results.lifecycle.invalidationAndRepair.immediateQuarantine = await observeQuarantine("Invalid source edit", sourceQuarantineStarted, sourceSelectionOffset);
    results.lifecycle.invalidationAndRepair.invalidEditQuarantine = await until(async () => {
      const selectedType = await selectedHover();
      const unaffectedType = await otherIdHover();
      const diagnostics = vscode.languages.getDiagnostics(uri).map(item => ({ source: item.source, message: item.message }));
      const suspendedTypingObserved = typingDiagnostics.slice(sourceDiagnosticOffset).some(items => items.some(item =>
        item.source === "GraphQL Desk" && /suspended typing/i.test(item.message)));
      let manifestStatus;
      try {
        const settings = JSON.parse(await fs.readFile(path.join(workspace, ".vscode", "settings.json"), "utf8"));
        const selectedGeneration = path.resolve(workspace, settings["python.analysis.stubPath"]);
        const manifest = JSON.parse(await fs.readFile(path.join(selectedGeneration, "complete.json"), "utf8"));
        const operations = manifest.operationManifest.flatMap(group => group.operations);
        manifestStatus = Object.fromEntries(operations.map(item => [item.binding, item.status]));
      } catch { /* Wait for the complete generation selected by GraphQL Desk. */ }
      results.lifecycle.invalidationAndRepair.invalidEditProbe = {
        selectedType, unaffectedType, dirty: document.isDirty, suspendedTypingObserved,
        diskUnchanged: (await fs.readFile(consumerUri.fsPath, "utf8")) === originalConsumer, diagnostics, manifestStatus,
      };
      return /Never|NoReturn|Unknown/.test(selectedType) && /\bID\b/.test(unaffectedType) &&
        manifestStatus?.doc === "unavailable" && manifestStatus?.other_doc === "precise" &&
        suspendedTypingObserved &&
        diagnostics.some(item => /GraphQL/i.test(item.source || "") && /missing/i.test(item.message));
    }, "invalid GraphQL edit makes only its operation unavailable, reports a diagnostic, and preserves its dirty source and sibling typing", 60000);
    assert.ok(results.lifecycle.invalidationAndRepair.invalidEditQuarantine,
      JSON.stringify(results.lifecycle.invalidationAndRepair.invalidEditProbe));
    assert.equal(document.getText(), invalidConsumer, "GraphQL Desk must preserve the user's dirty source buffer");
    assert.equal((await fs.readFile(consumerUri.fsPath, "utf8")), originalConsumer,
      "GraphQL Desk must not write the user's source file while quarantining typing");
    await replaceText(document, "{ missing count(limit: $limit) }", "{ selected count(limit: $limit) }");
    results.lifecycle.invalidationAndRepair.invalidEditRepair = await until(async () => {
      const selectedType = await selectedHover();
      const unaffectedType = await otherIdHover();
      const suspendedDiagnostics = vscode.languages.getDiagnostics(uri)
        .filter(item => item.source === "GraphQL Desk" && /suspended typing/i.test(item.message));
      results.lifecycle.invalidationAndRepair.invalidEditRepairProbe = { selectedType, unaffectedType,
        suspendedDiagnostics: suspendedDiagnostics.map(item => item.message) };
      return /\bString\b/.test(selectedType) && /\bID\b/.test(unaffectedType) && suspendedDiagnostics.length === 0;
    }, "a successful generation restores precise typing after quarantine", 60000);
    assert.ok(results.lifecycle.invalidationAndRepair.invalidEditRepair,
      JSON.stringify(results.lifecycle.invalidationAndRepair.invalidEditRepairProbe));

    const alternatePython = process.env.GRAPHQL_DESK_ALTERNATE_PYTHON;
    assert.ok(path.isAbsolute(alternatePython ?? ""), "Set a second absolute Python environment for the interpreter-switch host proof");
    await fs.access(alternatePython);
    const deskConfiguration = vscode.workspace.getConfiguration("graphqlDesk", uri);
    const originalPython = vscode.workspace.getConfiguration("graphqlDesk", uri).get("pythonPath", "");
    await deskConfiguration.update("pythonPath", alternatePython, vscode.ConfigurationTarget.Workspace);
    results.lifecycle.interpreterChange = {
      from: originalPython,
      to: alternatePython,
      fromPythonVersion: pythonVersion(originalPython || process.env.GRAPHQL_DESK_PYTHON),
      toPythonVersion: pythonVersion(alternatePython),
    };
    const interpreterSchema = await vscode.workspace.openTextDocument(vscode.Uri.file(path.join(workspace, "schema.graphql")));
    const interpreterOriginalSchema = interpreterSchema.getText();
    const interpreterBooleanSchema = interpreterOriginalSchema.replace("selected: String!", "selected: Boolean!");
    assert.notEqual(interpreterBooleanSchema, interpreterOriginalSchema);
    await replace(interpreterSchema, interpreterBooleanSchema);
    results.lifecycle.interpreterChange.refreshed = await until(async () => {
      const selectedType = await selectedHover();
      const unaffectedType = await otherIdHover();
      const selectedPython = vscode.workspace.getConfiguration("graphqlDesk", uri).get("pythonPath", "");
      results.lifecycle.interpreterChange.probe = { selectedType, unaffectedType, selectedPython };
      return path.resolve(selectedPython).toLowerCase() === path.resolve(alternatePython).toLowerCase() &&
        /Boolean|bool/.test(selectedType) && /\bID\b/.test(unaffectedType);
    }, "Pylance typing recovers after switching the configured worker interpreter", 60000);
    assert.ok(results.lifecycle.interpreterChange.refreshed,
      JSON.stringify(results.lifecycle.interpreterChange.probe));
    await deskConfiguration.update("pythonPath", originalPython, vscode.ConfigurationTarget.Workspace);
    await replace(interpreterSchema, interpreterOriginalSchema);
    results.lifecycle.interpreterChange.restored = await until(async () => {
      const selectedType = await selectedHover();
      const unaffectedType = await otherIdHover();
      const selectedPython = vscode.workspace.getConfiguration("graphqlDesk", uri).get("pythonPath", "");
      results.lifecycle.interpreterChange.restoreProbe = { selectedType, unaffectedType, selectedPython };
      return path.resolve(selectedPython || process.env.GRAPHQL_DESK_PYTHON).toLowerCase() ===
        path.resolve(originalPython || process.env.GRAPHQL_DESK_PYTHON).toLowerCase() &&
        /String/.test(selectedType) && /\bID\b/.test(unaffectedType);
    }, "Pylance typing recovers after restoring the original worker interpreter", 60000);
    assert.ok(results.lifecycle.interpreterChange.restored);

    if (suite === "typingMatrix") {
      await Promise.all([
        fs.writeFile(path.join(workspace, "mapping_types.py"),
          "from typing import TypeAlias\nStringInput: TypeAlias = str | (bytes | bool)\nStringOutput: TypeAlias = float | (int | bool)\nBooleanInput: TypeAlias = bool | (int | bytes)\nBooleanOutput: TypeAlias = bool | (int | bytes)\n"),
        fs.writeFile(path.join(workspace, "mapping_consumer.py"), [
          "from typing import TypeVar",
          "from graphql_desk import TypedDocument",
          "from graphql_desk_generated.default import graphql",
          "from mapping_types import StringOutput",
          "R = TypeVar('R')",
          "V = TypeVar('V')",
          "def execute(document: TypedDocument[R, V], variables: V) -> R: ...",
          "mapping_doc = graphql('query Mapping($value: Boolean!) { selected @include(if: $value) }')",
          "result = execute(mapping_doc, {'value': True})",
          "output = result['selected']",
          "output_ok: StringOutput = output",
          "output_bad: str = output",
          "valid_bool_input = execute(mapping_doc, {'value': False})",
          "invalid_input = execute(mapping_doc, {'value': 1.5})",
          "",
        ].join("\n")),
      ]);
      const groupedMappingConfig = originalConfig.replace("[tool.graphql-desk]\n",
        '[tool.graphql-desk]\nscalars = { String = { input = "mapping_types.StringInput", output = "mapping_types.StringOutput" }, Boolean = { input = "mapping_types.BooleanInput", output = "mapping_types.BooleanOutput" } }\n');
      assert.notEqual(groupedMappingConfig, originalConfig);
      await replace(configDocument, groupedMappingConfig);
      const mappingUri = vscode.Uri.file(path.join(workspace, "mapping_consumer.py"));
      const mappingDocument = await vscode.workspace.openTextDocument(mappingUri);
      await vscode.window.showTextDocument(mappingDocument, { preview: false });
      const mappingPosition = name => {
        const offset = mappingDocument.getText().indexOf(name) + 1;
        assert.ok(offset > 0, `Expected grouped mapping variable ${name}`);
        return mappingDocument.positionAt(offset);
      };
      const mappingHover = async name => {
        const items = await vscode.commands.executeCommand("vscode.executeHoverProvider", mappingUri, mappingPosition(name)) || [];
        return items.flatMap(item => item.contents.map(content => typeof content === "string" ? content : content.value)).join("\n");
      };
      const mappingDiagnostics = name => vscode.languages.getDiagnostics(mappingUri)
        .filter(item => item.range.start.line === mappingPosition(name).line).map(item => item.message);
      results.lifecycle.groupedMappingAlias = await until(async () => {
        const output = await mappingHover("output =");
        const outputPositiveErrors = [...mappingDiagnostics("output_ok"), ...mappingDiagnostics("valid_bool_input")];
        const outputNegativeErrors = mappingDiagnostics("output_bad");
        const inputNegativeErrors = mappingDiagnostics("invalid_input");
        const mappingDiagnosticsFromExtension = vscode.languages.getDiagnostics(mappingUri)
          .filter(item => item.source === "GraphQL Desk").map(item => item.message);
        results.lifecycle.groupedMappingAliasProbe = { output, outputPositiveErrors, outputNegativeErrors,
          inputNegativeErrors, mappingDiagnosticsFromExtension };
        return /StringOutput/.test(output) && outputPositiveErrors.length === 0 &&
          outputNegativeErrors.length > 0 && inputNegativeErrors.length > 0 && mappingDiagnosticsFromExtension.length === 0;
      }, "Pylance applies grouped PEP 604 input and output aliases to live scalar typing", 60000);
      assert.ok(results.lifecycle.groupedMappingAlias,
        JSON.stringify(results.lifecycle.groupedMappingAliasProbe));
      const groupedStubPath = vscode.workspace.getConfiguration("python", consumerUri).get("analysis.stubPath", "");
      assert.ok(groupedStubPath);
      await replace(configDocument, originalConfig);
      results.lifecycle.groupedMappingAliasRestored = await until(async () => {
        const output = await mappingHover("output =");
        const stubPath = vscode.workspace.getConfiguration("python", consumerUri).get("analysis.stubPath", "");
        results.lifecycle.groupedMappingAliasRestoreProbe = { output, stubPath };
        return stubPath !== groupedStubPath && /: str\b|: String\b/.test(output) && !/StringOutput/.test(output);
      }, "restoring the original mapping config republishes baseline typing", 60000);
      assert.ok(results.lifecycle.groupedMappingAliasRestored,
        JSON.stringify(results.lifecycle.groupedMappingAliasRestoreProbe));
    }

    const configuration = vscode.workspace.getConfiguration("python", uri);
    results.lifecycle.configuredStubPath = configuration.get("analysis.stubPath");
    results.lifecycle.activeStubRoot = path.resolve(workspace, results.lifecycle.configuredStubPath || ".graphql-desk/typings");
    assert.ok(results.lifecycle.configuredStubPath);
    results.outcome = "live-typing-matrix-observed";
  } catch (error) {
    results.error = String(error.stack || error);
    results.outcome = "live-typing-matrix-failed";
    throw error;
  } finally {
    settingChanges?.dispose();
    diagnosticChanges?.dispose();
    await fs.writeFile(process.env.GRAPHQL_DESK_RESULT_PATH, JSON.stringify(results, null, 2));
    console.log(`GRAPHQL_DESK_TYPING_MATRIX ${JSON.stringify(results)}`);
  }
};
