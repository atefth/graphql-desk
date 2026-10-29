"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs/promises");
const path = require("node:path");
const vscode = require("vscode");
const { registerLanguageFeatures } = require("../src/languageFeatures");
const { createHighlighting, registerHighlighting } = require("../src/highlighting");
const { bundle, schema } = require("./featureFixtures.cjs");

function hoverText(hovers) {
  return (hovers ?? []).flatMap((hover) => hover.contents ?? [])
    .map((content) => typeof content === "string" ? content : content.value ?? "")
    .join("\n");
}

function locationTarget(item) {
  return { uri: item.uri ?? item.targetUri, range: item.range ?? item.targetSelectionRange ?? item.targetRange };
}

async function run() {
  const workspace = process.env.GRAPHQL_DESK_TEST_WORKSPACE;
  const resultPath = process.env.GRAPHQL_DESK_RESULT_PATH;
  assert.ok(workspace && path.isAbsolute(workspace), "An isolated test workspace is required");
  assert.ok(resultPath && path.isAbsolute(resultPath), "An isolated observations path is required");
  const fixtures = {
    "schema.graphql": schema,
    "main.py": "# 😀\r\nfrom graphql_desk import gql\r\nq=gql('{ user { missing old } }')\r\n",
    "persisted.py": `from graphql_desk import gql\nquery = gql('query Saved { user { name } }')\nsaved = gql.persisted("sha256:${"0".repeat(64)}", query)\n`,
    "missingPersisted.py": "from graphql_desk import gql\nquery = gql('query Saved { user { name } }')\nsaved = gql.persisted(document=query)\n",
    "partial.py": "from graphql_desk import gql\nq=gql('{ user { na')\n",
    "blank.py": "# 😀\r\nfrom graphql_desk import gql\r\nq=gql('''\r\n\r\n''')\r\n",
    "lexical.py": "from graphql_desk import gql\nq=gql('''query Read { user { name",
    "lexicalEscaped.py": ["# 😀", "from graphql_desk import gql", String.raw`q=gql("query Q { user(name: \"m\u0061😀\") { name`].join("\r\n"),
    "recovery.py": ["# 😀", "from graphql_desk import gql", String.raw`q=gql("query Read { user(id: \"m\u0061😀\") { name`].join("\r\n"),
    "selected.py": "outside: int = 1\nfrom graphql_desk import gql\nfrom fragments import card\nq=gql('{ user { name ...Card } }',[card])\n",
    "fragments.py": "from graphql_desk import gql\ncard=gql('fragment Card on User { id }')\n",
    "other.py": "# 😀\r\nfrom graphql_desk import gql\r\ncard=gql('fragment Card on User { name }')\r\n",
    "ambiguous.py": "# 😀\r\nfrom graphql_desk import gql\r\nfrom fragments import card as a\r\nfrom other import card as b\r\nq=gql('{ user { name ...Card } }',[a,b])\r\n",
    "duplicate.py": "# 😀\r\nfrom graphql_desk import gql\r\nq=gql('{ user { name ...Card } } fragment Card on User { id } fragment Card on User { name }')\r\n",
  };
  for (const [name, text] of Object.entries(fixtures)) await fs.writeFile(path.join(workspace, name), text, { encoding: "utf8", flag: "wx" });
  const documents = new Map();
  for (const name of Object.keys(fixtures)) {
    let document = await vscode.workspace.openTextDocument(vscode.Uri.file(path.join(workspace, name)));
    if (name.endsWith(".py") && document.languageId !== "python") document = await vscode.languages.setTextDocumentLanguage(document, "python");
    documents.set(name, document);
    assert.equal(document.isDirty, false);
  }
  const schemaDoc = documents.get("schema.graphql");
  const fragmentDoc = documents.get("fragments.py");
  for (const [id, version] of [["ms-python.python", "2026.10.0"], ["ms-python.vscode-pylance", "2026.4.1"]]) {
    const extension = vscode.extensions.getExtension(id);
    assert.ok(extension, `${id} must be installed in the isolated host`);
    assert.equal(extension.packageJSON.version, version);
    await extension.activate();
  }
  let trusted = true;
  let analyses = 0;
  const options = {
    isTrusted: () => trusted,
    analyze: async (snapshot) => {
      analyses += 1;
      return bundle(snapshot.text, { uri: snapshot.uri, version: snapshot.version,
        schema: schemaDoc.getText(), schemaUri: schemaDoc.uri.toString(), schemaVersion: schemaDoc.version,
        schemaToken: `schema-${schemaDoc.version}`,
        modules: ["fragments.py", "other.py"].map(name => documents.get(name))
          .filter(document => snapshot.uri !== document.uri.toString())
          .map(document => ({ project: "p", module: path.basename(document.fileName, ".py"),
            uri: document.uri.toString(), version: document.version, source: document.getText(), is_package: false })),
      });
    },
    isCurrent: async (value) => value.request.modules.every((item) => [...documents.values()].some((document) =>
      document.uri.toString() === item.uri && !document.isClosed && document.version === item.version && document.getText() === item.source)) &&
      value.request.schemas.every((item) => item.uri === schemaDoc.uri.toString() && item.version === schemaDoc.version && item.text === schemaDoc.getText()),
  };
  const registration = registerLanguageFeatures(vscode, options);
  const highlighting = registerHighlighting(vscode, options);
  const native = vscode.languages.registerHoverProvider({ language: "python", scheme: "file" }, {
    provideHover() { return new vscode.Hover("native-python-fixture"); },
  });
  const observations = { outcome: "read-only-features-observed", vscode: vscode.version, diagnostics: false, completion: false, boundaryCompletion: false, hover: false,
    schemaDefinition: false, fragmentDefinition: false, ambiguousFragmentDefinition: false, nativeCoexistence: false,
    highlighting: false, lexicalHighlighting: false, escapedLexicalHighlighting: false, highlightRecovery: false,
    nativePythonHover: false, untrustedNoAnalysis: false, persistedIdAction: false, persistedIdInsertion: false,
    diskUnchanged: false, cleanBuffers: false, progress: "fixtures-ready" };
  const recordProgress = async progress => {
    observations.progress = progress;
    await fs.writeFile(resultPath, JSON.stringify(observations), "utf8");
  };
  try {
    const invalid = documents.get("main.py");
    await registration.refresh(invalid);
    const diagnostics = vscode.languages.getDiagnostics(invalid.uri).filter((item) => item.source === "GraphQL Desk");
    const missing = diagnostics.find((item) => item.code === "graphql.validation");
    assert.ok(missing);
    assert.equal(invalid.getText(missing.range), "missing");
    assert.ok(diagnostics.some((item) => item.code === "graphql.deprecation"));
    observations.diagnostics = true;
    await recordProgress("diagnostics");
    const persistedDocument = documents.get("persisted.py");
    await recordProgress("persisted-refresh-start");
    await registration.refresh(persistedDocument);
    await recordProgress("persisted-refresh-complete");
    const staleId = vscode.languages.getDiagnostics(persistedDocument.uri)
      .find(item => item.code === "graphql-desk.persisted-hash-mismatch");
    assert.ok(staleId);
    assert.match(persistedDocument.getText(staleId.range), /^"sha256:/);
    const persistedActions = await vscode.commands.executeCommand("vscode.executeCodeActionProvider",
      persistedDocument.uri, staleId.range);
    await recordProgress("persisted-update-actions-complete");
    const updateId = persistedActions.find(item => /Update persisted ID/.test(item.title));
    assert.ok(updateId);
    const edit = updateId.edit.entries().find(([uri]) => uri.toString() === persistedDocument.uri.toString())?.[1]?.[0];
    assert.ok(edit);
    assert.ok(edit.range.isEqual(staleId.range));
    const indexed = bundle(persistedDocument.getText(), { uri: persistedDocument.uri.toString(),
      version: persistedDocument.version, schema, schemaUri: schemaDoc.uri.toString(), schemaVersion: schemaDoc.version,
      schemaToken: `schema-${schemaDoc.version}`, modules: ["fragments.py", "other.py"].map(name => documents.get(name))
        .map(document => ({ project: "p", module: path.basename(document.fileName, ".py"),
          uri: document.uri.toString(), version: document.version, source: document.getText(), is_package: false })) });
    assert.equal(edit.newText, JSON.stringify(indexed.response.persisted[0].expected_id));
    observations.persistedIdAction = true;
    await recordProgress("persisted-id-update-action");
    const missingPersistedDocument = documents.get("missingPersisted.py");
    await recordProgress("missing-persisted-refresh-start");
    await registration.refresh(missingPersistedDocument);
    await recordProgress("missing-persisted-refresh-complete");
    const missingPersisted = vscode.languages.getDiagnostics(missingPersistedDocument.uri)
      .find(item => item.code === "graphql-desk.persisted-id-missing");
    assert.ok(missingPersisted);
    const insertActions = await vscode.commands.executeCommand("vscode.executeCodeActionProvider",
      missingPersistedDocument.uri, missingPersisted.range);
    const insertId = insertActions.find(item => /Insert persisted ID/.test(item.title));
    assert.ok(insertId);
    const insertEdit = insertId.edit.entries().find(([uri]) => uri.toString() === missingPersistedDocument.uri.toString())?.[1]?.[0];
    assert.ok(insertEdit);
    assert.ok(insertEdit.range.isEmpty);
    assert.equal(missingPersistedDocument.getText(insertEdit.range), "");
    assert.match(insertEdit.newText, /^id="sha256:[0-9a-f]{64}", $/);
    observations.persistedIdInsertion = true;
    await recordProgress("persisted-id-insertion-action");
    const partial = documents.get("partial.py");
    const completion = await vscode.commands.executeCommand("vscode.executeCompletionItemProvider", partial.uri,
      partial.positionAt(partial.getText().indexOf("na'") + 2));
    const name = completion.items.find((item) => item.label === "name");
    assert.ok(name);
    assert.equal(partial.getText(name.range), "na");
    observations.completion = true;
    const blank = documents.get("blank.py");
    const blankPosition = blank.positionAt(blank.getText().indexOf("'''\r\n") + 5);
    const blankCompletion = await vscode.commands.executeCommand("vscode.executeCompletionItemProvider", blank.uri, blankPosition);
    const query = blankCompletion.items.find(item => item.label === "query");
    assert.ok(query);
    assert.equal(query.insertText, "query");
    assert.ok(query.range.isEmpty);
    assert.ok(query.range.start.isEqual(blankPosition));
    assert.ok(query.range.end.isEqual(blankPosition));
    observations.boundaryCompletion = true;
    const selected = documents.get("selected.py");
    const position = selected.positionAt(selected.getText().indexOf("name") + 1);
    const hover = await vscode.commands.executeCommand("vscode.executeHoverProvider", selected.uri, position);
    assert.match(hoverText(hover), /Display name/);
    assert.match(hoverText(hover), /native-python-fixture/);
    const fieldHover = await vscode.commands.executeCommand("vscode.executeHoverProvider", selected.uri,
      selected.positionAt(selected.getText().indexOf("user") + 1));
    const signature = 'Query.user(id: ID = 1, color: Color, filter: Filter): User';
    const graphqlHover = fieldHover.find(item => hoverText([item]).includes(signature));
    assert.ok(graphqlHover);
    assert.equal(selected.getText(graphqlHover.range), "user");
    assert.match(hoverText([graphqlHover]), /Current user/);
    assert.match(hoverText(fieldHover), /native-python-fixture/);
    observations.hover = true;
    const outside = await vscode.commands.executeCommand("vscode.executeHoverProvider", selected.uri, new vscode.Position(0, 1));
    assert.match(hoverText(outside), /native-python-fixture/);
    assert.doesNotMatch(hoverText(outside), /Display name/);
    observations.nativeCoexistence = true;
    await recordProgress("native-coexistence");
    const deadline = Date.now() + 30000;
    let pythonHover;
    do {
      pythonHover = await vscode.commands.executeCommand("vscode.executeHoverProvider", selected.uri,
        selected.positionAt(selected.getText().indexOf("outside") + 2));
      if (/outside/.test(hoverText(pythonHover)) && /int|Literal/.test(hoverText(pythonHover))) break;
      await new Promise((resolve) => setTimeout(resolve, 200));
    } while (Date.now() < deadline);
    assert.match(hoverText(pythonHover), /outside/);
    assert.match(hoverText(pythonHover), /int|Literal/);
    observations.nativePythonHover = true;
    await recordProgress("native-python-hover");
    const definitions = await vscode.commands.executeCommand("vscode.executeDefinitionProvider", selected.uri, position);
    assert.ok(definitions.map(locationTarget).some((item) => item.uri.toString() === schemaDoc.uri.toString() && schemaDoc.getText(item.range) === "name"));
    observations.schemaDefinition = true;
    const fragments = await vscode.commands.executeCommand("vscode.executeDefinitionProvider", selected.uri,
      selected.positionAt(selected.getText().indexOf("...Card") + 4));
    assert.ok(fragments.map(locationTarget).some((item) => item.uri.toString() === fragmentDoc.uri.toString() && fragmentDoc.getText(item.range) === "Card"));
    observations.fragmentDefinition = true;
    for (const [filename, code] of [["ambiguous.py", "conflicting-fragment"], ["duplicate.py", "duplicate-definition"]]) {
      const document = documents.get(filename);
      const targets = await vscode.commands.executeCommand("vscode.executeDefinitionProvider", document.uri,
        document.positionAt(document.getText().indexOf("...Card") + 4));
      assert.deepEqual(targets, [], filename);
      const fields = await vscode.commands.executeCommand("vscode.executeDefinitionProvider", document.uri,
        document.positionAt(document.getText().indexOf("name ...") + 1));
      assert.ok(fields.map(locationTarget).some(item => item.uri.toString() === schemaDoc.uri.toString() && schemaDoc.getText(item.range) === "name"), filename);
      await registration.refresh(document);
      assert.ok(vscode.languages.getDiagnostics(document.uri).some(item => item.code === `graphql-desk.${code}`), filename);
    }
    observations.ambiguousFragmentDefinition = true;
    const editor = await vscode.window.showTextDocument(selected);
    let decorations = 0;
    await highlighting.refresh({ document: selected, setDecorations(type, ranges) {
      decorations += ranges.length;
      editor.setDecorations(type, ranges);
    } });
    assert.ok(decorations > 0);
    observations.highlighting = true;
    const lexicalDocument = documents.get("lexical.py");
    await registration.refresh(lexicalDocument);
    assert.equal(vscode.languages.getDiagnostics(lexicalDocument.uri).some(item => item.code === "graphql.validation"), false,
      "Lexical candidates from incomplete Python must not become typed GraphQL operations");
    const lexicalEditor = await vscode.window.showTextDocument(lexicalDocument);
    const lexicalTokens = [];
    await highlighting.refresh({ document: lexicalDocument, setDecorations(_type, ranges) {
      lexicalTokens.push(...ranges.map(range => lexicalDocument.getText(range)));
      lexicalEditor.setDecorations(_type, ranges);
    } });
    assert.ok(lexicalTokens.includes("query"));
    observations.lexicalHighlighting = true;
    const escapedLexical = documents.get("lexicalEscaped.py");
    await registration.refresh(escapedLexical);
    assert.equal(vscode.languages.getDiagnostics(escapedLexical.uri).some(item => item.code === "graphql.validation"), false,
      "Lexical candidates from escaped incomplete Python must not become typed GraphQL operations");
    const escapedEditor = await vscode.window.showTextDocument(escapedLexical);
    const escapedRanges = [];
    await highlighting.refresh({ document: escapedLexical, setDecorations(_type, ranges) {
      escapedRanges.push(...ranges);
      escapedEditor.setDecorations(_type, ranges);
    } });
    const escapedQueryStart = escapedLexical.getText().indexOf("query");
    const escapedStringStart = escapedLexical.getText().indexOf(String.raw`\"m`);
    const escapedStringEnd = escapedLexical.getText().indexOf(String.raw`\"`, escapedStringStart + 2) + 2;
    const expectedQueryRange = new vscode.Range(escapedLexical.positionAt(escapedQueryStart), escapedLexical.positionAt(escapedQueryStart + 5));
    const expectedStringRange = new vscode.Range(escapedLexical.positionAt(escapedStringStart), escapedLexical.positionAt(escapedStringEnd));
    assert.ok(escapedRanges.some(range => range.isEqual(expectedQueryRange)), "Lexical keyword decoration must use the exact UTF-16 host range");
    assert.ok(escapedRanges.some(range => range.isEqual(expectedStringRange)), "Escaped GraphQL string decoration must cover the exact Python source escape span");
    observations.escapedLexicalHighlighting = true;
    const recovery = documents.get("recovery.py");
    await vscode.window.showTextDocument(recovery);
    const decoratedRanges = async () => {
      const ranges = [];
      await highlighting.refresh({ document: recovery, setDecorations(_type, values) { ranges.push(...values); } });
      return ranges;
    };
    const highlightService = createHighlighting(options);
    const observeRecovery = async () => highlightService.highlight({
      uri: recovery.uri.toString(), version: recovery.version, text: recovery.getText(),
    });
    await registration.refresh(recovery);
    assert.equal(vscode.languages.getDiagnostics(recovery.uri).some(item => item.code === "graphql.validation"), false);
    const incompleteItems = await observeRecovery();
    assert.ok(incompleteItems.length && incompleteItems.every(item => item.mode === "lexical"));
    const incompleteDecorations = await decoratedRanges();
    const queryStart = recovery.getText().indexOf("query");
    assert.ok(incompleteDecorations.some(range => range.isEqual(new vscode.Range(
      recovery.positionAt(queryStart), recovery.positionAt(queryStart + 5),
    ))));
    const incompleteEscape = recovery.getText().indexOf(String.raw`\u0061`);
    assert.ok(incompleteItems.some(item => item.start <= incompleteEscape && item.end > incompleteEscape));
    const resolvedSource = ["# 😀", "from graphql_desk import gql", String.raw`q=gql("query Read { user(id: \"m\u0061😀\") { missing } }")`].join("\r\n");
    const recoveryEdit = new vscode.WorkspaceEdit();
    recoveryEdit.replace(recovery.uri, new vscode.Range(recovery.positionAt(0), recovery.positionAt(recovery.getText().length)), resolvedSource);
    assert.equal(await vscode.workspace.applyEdit(recoveryEdit), true);
    await registration.refresh(recovery);
    const invalidItems = await observeRecovery();
    const invalidDecorations = await decoratedRanges();
    const missingStart = recovery.getText().indexOf("missing");
    assert.ok(invalidItems.some(item => item.mode === "resolved" && item.start === missingStart && item.end === missingStart + 7));
    assert.ok(invalidDecorations.some(range => range.isEqual(new vscode.Range(
      recovery.positionAt(missingStart), recovery.positionAt(missingStart + 7),
    ))));
    assert.ok(vscode.languages.getDiagnostics(recovery.uri).some(item => item.code === "graphql.validation" && recovery.getText(item.range) === "missing"));
    const repairedSource = resolvedSource.replace("missing", "name");
    const repairEdit = new vscode.WorkspaceEdit();
    repairEdit.replace(recovery.uri, new vscode.Range(recovery.positionAt(0), recovery.positionAt(recovery.getText().length)), repairedSource);
    assert.equal(await vscode.workspace.applyEdit(repairEdit), true);
    await registration.refresh(recovery);
    const repairedItems = await observeRecovery();
    assert.ok(repairedItems.length && repairedItems.every(item => item.mode === "resolved"));
    const repairedDecorations = await decoratedRanges();
    const nameStart = recovery.getText().indexOf("name");
    assert.ok(repairedDecorations.some(range => range.isEqual(new vscode.Range(
      recovery.positionAt(nameStart), recovery.positionAt(nameStart + 4),
    ))));
    assert.equal(vscode.languages.getDiagnostics(recovery.uri).some(item => item.code === "graphql.validation"), false);
    assert.equal(recovery.isDirty, true);
    observations.highlightRecovery = true;
    trusted = false;
    const before = analyses;
    await registration.refresh(invalid);
    await highlighting.refresh(editor);
    assert.equal(analyses, before);
    assert.equal(vscode.languages.getDiagnostics(invalid.uri).filter((item) => item.source === "GraphQL Desk").length, 0);
    observations.untrustedNoAnalysis = true;
  } catch (error) {
    await fs.writeFile(resultPath, JSON.stringify({ ...observations, error: String(error) }), "utf8");
    throw error;
  } finally {
    highlighting.dispose();
    registration.dispose();
    native.dispose();
  }
  for (const [name, text] of Object.entries(fixtures)) {
    assert.equal(await fs.readFile(path.join(workspace, name), "utf8"), text);
    assert.equal(documents.get(name).isDirty, name === "recovery.py");
  }
  observations.diskUnchanged = true;
  observations.recoveryDirtyBufferPreserved = documents.get("recovery.py").isDirty;
  observations.cleanBuffers = [...documents].filter(([name]) => name !== "recovery.py").every(([, document]) => !document.isDirty);
  await fs.writeFile(resultPath, JSON.stringify(observations, null, 2), "utf8");
}

module.exports = { run };

