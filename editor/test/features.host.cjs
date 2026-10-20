"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs/promises");
const path = require("node:path");
const vscode = require("vscode");
const { registerLanguageFeatures } = require("../src/languageFeatures");
const { registerHighlighting } = require("../src/highlighting");
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
    "partial.py": "from graphql_desk import gql\nq=gql('{ user { na')\n",
    "selected.py": "outside: int = 1\nfrom graphql_desk import gql\nfrom fragments import card\nq=gql('{ user { name ...Card } }',[card])\n",
    "fragments.py": "from graphql_desk import gql\ncard=gql('fragment Card on User { id }')\n",
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
        modules: snapshot.uri === fragmentDoc.uri.toString() ? [] : [{ project: "p", module: "fragments",
          uri: fragmentDoc.uri.toString(), version: fragmentDoc.version, source: fragmentDoc.getText(), is_package: false }],
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
  const observations = { outcome: "read-only-features-observed", vscode: vscode.version, diagnostics: false, completion: false, hover: false,
    schemaDefinition: false, fragmentDefinition: false, nativeCoexistence: false,
    highlighting: false, nativePythonHover: false, untrustedNoAnalysis: false, diskUnchanged: false, cleanBuffers: false };
  try {
    const invalid = documents.get("main.py");
    await registration.refresh(invalid);
    const diagnostics = vscode.languages.getDiagnostics(invalid.uri).filter((item) => item.source === "GraphQL Desk");
    const missing = diagnostics.find((item) => item.code === "graphql.validation");
    assert.ok(missing);
    assert.equal(invalid.getText(missing.range), "missing");
    assert.ok(diagnostics.some((item) => item.code === "graphql.deprecation"));
    observations.diagnostics = true;
    const partial = documents.get("partial.py");
    const completion = await vscode.commands.executeCommand("vscode.executeCompletionItemProvider", partial.uri,
      partial.positionAt(partial.getText().indexOf("na'") + 2));
    const name = completion.items.find((item) => item.label === "name");
    assert.ok(name);
    assert.equal(partial.getText(name.range), "na");
    observations.completion = true;
    const selected = documents.get("selected.py");
    const position = selected.positionAt(selected.getText().indexOf("name") + 1);
    const hover = await vscode.commands.executeCommand("vscode.executeHoverProvider", selected.uri, position);
    assert.match(hoverText(hover), /Display name/);
    assert.match(hoverText(hover), /native-python-fixture/);
    observations.hover = true;
    const outside = await vscode.commands.executeCommand("vscode.executeHoverProvider", selected.uri, new vscode.Position(0, 1));
    assert.match(hoverText(outside), /native-python-fixture/);
    assert.doesNotMatch(hoverText(outside), /Display name/);
    observations.nativeCoexistence = true;
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
    const definitions = await vscode.commands.executeCommand("vscode.executeDefinitionProvider", selected.uri, position);
    assert.ok(definitions.map(locationTarget).some((item) => item.uri.toString() === schemaDoc.uri.toString() && schemaDoc.getText(item.range) === "name"));
    observations.schemaDefinition = true;
    const fragments = await vscode.commands.executeCommand("vscode.executeDefinitionProvider", selected.uri,
      selected.positionAt(selected.getText().indexOf("...Card") + 4));
    assert.ok(fragments.map(locationTarget).some((item) => item.uri.toString() === fragmentDoc.uri.toString() && fragmentDoc.getText(item.range) === "Card"));
    observations.fragmentDefinition = true;
    const editor = await vscode.window.showTextDocument(selected);
    let decorations = 0;
    await highlighting.refresh({ document: selected, setDecorations(type, ranges) {
      decorations += ranges.length;
      editor.setDecorations(type, ranges);
    } });
    assert.ok(decorations > 0);
    observations.highlighting = true;
    trusted = false;
    const before = analyses;
    await registration.refresh(invalid);
    await highlighting.refresh(editor);
    assert.equal(analyses, before);
    assert.equal(vscode.languages.getDiagnostics(invalid.uri).filter((item) => item.source === "GraphQL Desk").length, 0);
    observations.untrustedNoAnalysis = true;
  } finally {
    highlighting.dispose();
    registration.dispose();
    native.dispose();
  }
  for (const [name, text] of Object.entries(fixtures)) {
    assert.equal(await fs.readFile(path.join(workspace, name), "utf8"), text);
    assert.equal(documents.get(name).isDirty, false);
  }
  observations.diskUnchanged = true;
  observations.cleanBuffers = true;
  await fs.writeFile(resultPath, JSON.stringify(observations, null, 2), "utf8");
}

module.exports = { run };
