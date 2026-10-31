"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs/promises");
const path = require("node:path");
const vscode = require("vscode");
const http = require("node:http");
const { buildSchema, introspectionFromSchema } = require("graphql");
const { schema } = require("./featureFixtures.cjs");
const hoverText = values => (values ?? []).flatMap(value => value.contents ?? []).map(value => typeof value === "string" ? value : value.value ?? "").join("\n");
const target = value => ({ uri: value.uri ?? value.targetUri, range: value.range ?? value.targetSelectionRange ?? value.targetRange });
async function until(action, accept, message) {
  const deadline = Date.now() + 30000;
  let value;
  do { value = await action(); if (accept(value)) return value; await new Promise(resolve => setTimeout(resolve, 100)); } while (Date.now() < deadline);
  let lastValue;
  try { lastValue = JSON.stringify(value)?.slice(0, 2000); } catch { lastValue = String(value).slice(0, 2000); }
  assert.fail(`${message}: last observed ${lastValue}`);
}
async function replace(document, text) {
  const edit = new vscode.WorkspaceEdit();
  edit.replace(document.uri, new vscode.Range(document.positionAt(0), document.positionAt(document.getText().length)), text);
  assert.equal(await vscode.workspace.applyEdit(edit), true);
}
async function run() {
  const workspace = process.env.GRAPHQL_DESK_TEST_WORKSPACE;
  const resultPath = process.env.GRAPHQL_DESK_RESULT_PATH;
  const branchFixture = ["# 😀", "from graphql_desk import gql", "def read(enabled):", "    if enabled:", "        # query note", "        query = gql('query ExtractBranch { user { id name } }')", ""].join("\r\n");
  const fixtures = {
    "schema.graphql": schema,
    "pyproject.toml": '[tool.graphql-desk]\nschema="schema.graphql"\nclient_directives=["local"]\nschema_output="graphql_desk_generated/default.pyi"\n',
    "main.py": 'raise RuntimeError("PROJECT_MUST_NOT_EXECUTE")\nfrom graphql_desk import gql\nq=gql("{ user { missing old } }")\n',
    "recovery.py": ["# 😀", "from graphql_desk import gql", String.raw`q=gql("query Read { user(id: \"m\u0061😀\") { name`].join("\r\n"),
    "outline.py": "from graphql_desk import gql\nq=gql('query Main { user { id } }')\n",
    "extract.py": "from graphql_desk import gql\nq=gql('query Extract { user { id name } }')\n",
    "partial.py": 'from graphql_desk import gql\nq=gql("{ user { na")\n',
    "selected.py": 'outside: int = 1\nfrom graphql_desk import gql\nfrom fragments import card\nq=gql("{ user { name @local ...Card } }",[card])\n',
    "fragments.py": 'from graphql_desk import gql\ncard=gql("fragment Card on User { id }")\n',
    "extract-branch.py": branchFixture,
  };
  for (const [name, text] of Object.entries(fixtures)) await fs.writeFile(path.join(workspace, name), text, { encoding: "utf8", flag: "wx" });
  const documents = new Map();
  for (const name of Object.keys(fixtures)) {
    // Index it on disk before activation, but open it only after the outline checks.
    if (name === "extract-branch.py") continue;
    let document = await vscode.workspace.openTextDocument(vscode.Uri.file(path.join(workspace, name)));
    if (name.endsWith(".py") && document.languageId !== "python") document = await vscode.languages.setTextDocumentLanguage(document, "python");
    documents.set(name, document);
  }
  const extension = vscode.extensions.getExtension("graphql-desk.graphql-desk-editor");
  assert.ok(extension); await extension.activate(); assert.equal(extension.isActive, true);
  const symbolText = (document, range) => document.getText().slice(document.offsetAt(range.start), document.offsetAt(range.end));
  const documentSymbols = async document => await vscode.commands.executeCommand("vscode.executeDocumentSymbolProvider", document.uri) || [];
  const mainSymbols = await until(() => documentSymbols(documents.get("outline.py")), values => values.some(item => item.name === "Main"), "Native operation Outline symbol");
  const fragmentSymbols = await until(() => documentSymbols(documents.get("fragments.py")), values => values.some(item => item.name === "Card"), "Native fragment Outline symbol");
  const operationSymbol = mainSymbols.find(item => item.name === "Main");
  const fragmentSymbol = fragmentSymbols.find(item => item.name === "Card");
  assert.equal(symbolText(documents.get("outline.py"), operationSymbol.selectionRange), "Main");
  assert.equal(symbolText(documents.get("fragments.py"), fragmentSymbol.selectionRange), "Card");
  assert.equal(symbolText(documents.get("outline.py"), operationSymbol.range), "query Main { user { id } }");
  assert.equal(symbolText(documents.get("fragments.py"), fragmentSymbol.range), "fragment Card on User { id }");
  if (process.env.GRAPHQL_DESK_SYMBOLS_ONLY === "1") {
    await fs.writeFile(resultPath, JSON.stringify({ outcome: "read-only-symbols-observed", vscode: vscode.version,
      actualActivation: extension.isActive, operation: operationSymbol.name, fragment: fragmentSymbol.name }, null, 2));
    return;
  }
  for (const [id, version] of [["ms-python.python", "2026.10.0"], ["ms-python.vscode-pylance", "2026.4.1"]]) {
    const native = vscode.extensions.getExtension(id); assert.ok(native); assert.equal(native.packageJSON.version, version); await native.activate();
  }
  assert.equal(vscode.workspace.getConfiguration("graphqlDesk", documents.get("main.py").uri).get("pythonPath"), "", "Host proof must use the Python extension selection without an explicit override");
  const pythonApi = vscode.extensions.getExtension("ms-python.python").exports;
  const selectedEnvironment = pythonApi.environments.getActiveEnvironmentPath(vscode.workspace.workspaceFolders[0].uri);
  const resolvedEnvironment = await pythonApi.environments.resolveEnvironment(selectedEnvironment);
  assert.equal(resolvedEnvironment.executable.uri.scheme, "file");
  assert.equal((await fs.realpath(resolvedEnvironment.executable.uri.fsPath)).toLowerCase(), (await fs.realpath(process.env.GRAPHQL_DESK_PYTHON)).toLowerCase());
  assert.ok((await vscode.commands.getCommands(true)).includes("graphqlDesk.extractFragment"));
  const extractDoc = documents.get("extract.py");
  await vscode.window.showTextDocument(extractDoc);
  const extractStart = extractDoc.getText().indexOf("id name");
  const extractRange = new vscode.Range(extractDoc.positionAt(extractStart), extractDoc.positionAt(extractStart + 7));
  const extractActions = await until(
    () => vscode.commands.executeCommand("vscode.executeCodeActionProvider", extractDoc.uri, extractRange),
    values => Array.isArray(values) && values.some(item => item.title === "Extract GraphQL fragment"),
    "Activated extraction code action",
  );
  const extractAction = extractActions.find(item => item.title === "Extract GraphQL fragment");
  assert.equal(extractAction.command.command, "graphqlDesk.extractFragment");
  assert.equal(await vscode.commands.executeCommand(extractAction.command.command, ...extractAction.command.arguments), true);
  assert.match(extractDoc.getText(), /user_fields = gql\(source="fragment UserFields on User/);
  assert.match(extractDoc.getText(), /\.\.\.UserFields/);
  await vscode.commands.executeCommand("undo");
  assert.equal(extractDoc.getText(), fixtures["extract.py"]);
  const branchPath = path.join(workspace, "extract-branch.py");
  let branchDoc = await vscode.workspace.openTextDocument(vscode.Uri.file(branchPath));
  if (branchDoc.languageId !== "python") branchDoc = await vscode.languages.setTextDocumentLanguage(branchDoc, "python");
  documents.set("extract-branch.py", branchDoc);
  await vscode.window.showTextDocument(branchDoc);
  const branchStart = branchDoc.getText().indexOf("id name");
  const branchRange = new vscode.Range(branchDoc.positionAt(branchStart), branchDoc.positionAt(branchStart + 7));
  const branchActions = await until(
    () => vscode.commands.executeCommand("vscode.executeCodeActionProvider", branchDoc.uri, branchRange),
    values => Array.isArray(values) && values.some(item => item.title === "Extract GraphQL fragment"),
    "Activated nested-suite extraction code action",
  );
  const branchAction = branchActions.find(item => item.title === "Extract GraphQL fragment");
  assert.equal(await vscode.commands.executeCommand(branchAction.command.command, ...branchAction.command.arguments), true);
  assert.match(branchDoc.getText(), /if enabled:\r\n        # query note\r\n        query = gql\(.*fragments=\[gql\(source=/);
  await vscode.commands.executeCommand("undo");
  assert.equal(branchDoc.getText(), branchFixture);
  assert.equal(await fs.readFile(branchPath, "utf8"), branchFixture);
  const diagnostics = document => vscode.languages.getDiagnostics(document.uri).filter(item => item.source === "GraphQL Desk");
  const main = documents.get("main.py");
  await vscode.window.showTextDocument(main);
  const invalid = await until(() => diagnostics(main), values => values.some(item => item.code === "graphql.validation"), "Activated controller must publish actual diagnostics");
  assert.equal(main.getText(invalid.find(item => item.code === "graphql.validation").range), "missing");
  assert.ok(invalid.some(item => item.code === "graphql.deprecation"));
  const partial = documents.get("partial.py");
  const completion = await until(() => vscode.commands.executeCommand("vscode.executeCompletionItemProvider", partial.uri, partial.positionAt(partial.getText().indexOf('na"') + 2)), value => value.items.some(item => item.label === "name"), "Real activated completion");
  assert.equal(partial.getText(completion.items.find(item => item.label === "name").range), "na");
  const recovery = documents.get("recovery.py");
  await vscode.window.showTextDocument(recovery);
  assert.equal(diagnostics(recovery).some(item => item.code === "graphql.validation"), false,
    "Incomplete Python may receive lexical highlighting, but must not become a semantic GraphQL document");
  await replace(recovery, ["# 😀", "from graphql_desk import gql", String.raw`q=gql("query Read { user(id: \"m\u0061😀\") { missing } }")`].join("\r\n"));
  const recoveredError = await until(() => diagnostics(recovery), values => values.some(item => item.code === "graphql.validation"),
    "Repairing Python syntax must restore semantic GraphQL validation through the activated change path");
  assert.equal(recovery.getText(recoveredError.find(item => item.code === "graphql.validation").range), "missing");
  await replace(recovery, ["# 😀", "from graphql_desk import gql", String.raw`q=gql("query Read { user(id: \"m\u0061😀\") { name } }")`].join("\r\n"));
  await until(() => diagnostics(recovery), values => !values.some(item => item.code === "graphql.validation"),
    "Repairing the GraphQL selection must clear the resolved validation error");
  const selected = documents.get("selected.py");
  await vscode.window.showTextDocument(selected);
  const position = selected.positionAt(selected.getText().indexOf("name") + 1);
  await until(() => vscode.commands.executeCommand("vscode.executeHoverProvider", selected.uri, position), value => /Display name/.test(hoverText(value)), "Activated GraphQL hover");
  const pythonHover = await until(() => vscode.commands.executeCommand("vscode.executeHoverProvider", selected.uri, selected.positionAt(selected.getText().indexOf("outside") + 2)), value => /outside/.test(hoverText(value)) && /int|Literal/.test(hoverText(value)), "Pylance native hover must coexist");
  assert.doesNotMatch(hoverText(pythonHover), /Display name/);
  const schemaUri = documents.get("schema.graphql").uri.toString();
  const definitions = await until(
    () => vscode.commands.executeCommand("vscode.executeDefinitionProvider", selected.uri, position),
    value => Array.isArray(value) && value.map(target).some(item => item.uri.toString() === schemaUri),
    "Schema definition should resolve to schema.graphql",
  );
  const fragmentDocument = documents.get("fragments.py");
  await until(
    () => vscode.commands.executeCommand("vscode.executeDefinitionProvider", selected.uri, selected.positionAt(selected.getText().indexOf("...Card") + 4)),
    values => Array.isArray(values) && values.map(target).some(value =>
      value.uri.toString() === fragmentDocument.uri.toString() && fragmentDocument.getText(value.range) === "Card"),
    "Fragment definition should resolve to fragments.py",
  );
  await until(() => diagnostics(selected), values => !values.some(item => /Unknown directive/.test(item.message)), "Configured client directive must survive production capture");
  const directiveCompletion = await until(() => vscode.commands.executeCommand("vscode.executeCompletionItemProvider", selected.uri,
    selected.positionAt(selected.getText().indexOf("@local") + 3)), value => value.items.some(item => item.label === "local"),
  "Configured client directive completion");
  const localDirective = directiveCompletion.items.find(item => item.label === "local");
  assert.equal(selected.getText(localDirective.range), "local");
  assert.equal(localDirective.insertText, "local");
  const configDoc = documents.get("pyproject.toml");
  const invalidConfig = fixtures["pyproject.toml"].replace('schema="schema.graphql"', "schema=");
  await replace(configDoc, invalidConfig);
  const configDiagnostic = await until(() => diagnostics(configDoc), values => values.some(item => item.code === "graphql-desk.configuration"),
    "Malformed project configuration must be diagnosed on pyproject.toml");
  assert.equal(configDiagnostic.find(item => item.code === "graphql-desk.configuration").message, "Invalid pyproject TOML");
  await replace(configDoc, fixtures["pyproject.toml"]);
  await until(() => diagnostics(configDoc), values => !values.some(item => item.code === "graphql-desk.configuration"),
    "Repairing project configuration clears its diagnostic");
  await replace(configDoc, fixtures["pyproject.toml"].replace('client_directives=["local"]', 'client_directives=[]'));
  await until(() => diagnostics(selected), values => values.some(item => /Unknown directive.*local/.test(item.message)), "Dirty config updates client directives");
  await replace(configDoc, fixtures["pyproject.toml"]);
  const fragmentDoc = documents.get("fragments.py");
  await replace(fragmentDoc, fixtures["fragments.py"].replace("{ id }", "{ fragmentMissing }"));
  await vscode.window.showTextDocument(fragmentDoc);
  await until(() => diagnostics(fragmentDoc), values => values.some(item => /fragmentMissing/.test(item.message)), "Dirty fragment dependency receives exact diagnostics");
  await replace(fragmentDoc, fixtures["fragments.py"]);
  // Source edits use buffer text; no file save or generated overlay is involved.
  await vscode.window.showTextDocument(main);
  await replace(main, 'from graphql_desk import gql\nq=gql("{user {name}}")\n');
  const repairedNamePosition = main.positionAt(main.getText().indexOf("name") + 1);
  await until(async () => diagnostics(main).length === 0 && /Display name/.test(hoverText(
    await vscode.commands.executeCommand("vscode.executeHoverProvider", main.uri, repairedNamePosition))),
  value => value, "Repair clears stale diagnostics and restores fresh GraphQL hover");
  await replace(main, 'from graphql_desk import gql\nq=gql("{user {differentMissing}}")\n');
  const changed = await until(() => diagnostics(main), values => values.some(item => /differentMissing/.test(item.message)), "Unsaved source publishes only new diagnostics");
  assert.equal(main.getText(changed.find(item => /differentMissing/.test(item.message)).range), "differentMissing");
  // An associated new source is absent from findFiles and still has actual URI features.
  const unsavedPath = path.join(workspace, "new_unsaved.py");
  let unsaved = await vscode.workspace.openTextDocument(vscode.Uri.file(unsavedPath).with({ scheme: "untitled" }));
  unsaved = await vscode.languages.setTextDocumentLanguage(unsaved, "python");
  await replace(unsaved, 'from graphql_desk import gql\nq=gql("{user {name}}")\n');
  await vscode.window.showTextDocument(unsaved);
  const unsavedPosition = unsaved.positionAt(unsaved.getText().indexOf("name") + 1);
  await until(() => vscode.commands.executeCommand("vscode.executeHoverProvider", unsaved.uri, unsavedPosition), value => /Display name/.test(hoverText(value)), "Associated untitled source must receive GraphQL hover");
  const schemaDoc = documents.get("schema.graphql");
  await replace(schemaDoc, schema.replace("name: String", "renamed: String"));
  await until(() => vscode.commands.executeCommand("vscode.executeHoverProvider", unsaved.uri, unsavedPosition), value => !/Display name/.test(hoverText(value)), "Dirty schema invalidates old semantic hover");
  let remoteRequests = 0;
  const credentialName = "GRAPHQL_DESK_ACTIVATION_TOKEN";
  const previousCredential = process.env[credentialName];
  let credential = "TEST_ONLY_PRIVATE_INITIAL";
  process.env[credentialName] = credential;
  let denyRemote = false;
  let description = "Display name";
  const receivedCredentials = [];
  const remote = http.createServer((request, response) => {
    remoteRequests += 1; request.resume();
    receivedCredentials.push(request.headers.authorization);
    if (denyRemote || request.headers.authorization !== `Bearer ${credential}`) {
      response.writeHead(401); response.end(`PRIVATE_RESPONSE_${credential}`); return;
    }
    const payload = JSON.stringify({ data: introspectionFromSchema(buildSchema(schema.replace("Display name", description))) });
    response.setHeader("Content-Type", "application/json"); response.end(payload);
  });
  await new Promise(resolve => remote.listen(0, "127.0.0.1", resolve));
  try {
    const remoteConfig = `[tool.graphql-desk]\nschema={url="http://127.0.0.1:${remote.address().port}/graphql",headers={Authorization="Bearer \${env:${credentialName}}"}}\nschema_output="graphql_desk_generated/default.pyi"\n`;
    await replace(configDoc, remoteConfig);
    await vscode.commands.executeCommand("vscode.executeHoverProvider", unsaved.uri, unsavedPosition);
    assert.equal(remoteRequests, 0, "Typing/config capture must never fetch automatically");
    await vscode.commands.executeCommand("graphqlDesk.refreshSchema");
    await until(() => vscode.commands.executeCommand("vscode.executeHoverProvider", unsaved.uri, unsavedPosition), value => /Display name/.test(hoverText(value)), "Explicit production refresh restores remote features");
    assert.ok(remoteRequests > 0);
    const fetched = remoteRequests;
    await replace(unsaved, unsaved.getText() + "# another unsaved edit\n");
    await until(() => vscode.commands.executeCommand("vscode.executeHoverProvider", unsaved.uri, unsavedPosition), value => /Display name/.test(hoverText(value)), "Fetched remote context survives source typing");
    assert.equal(remoteRequests, fetched, "Source edits retain schema without automatic HTTP");
    assert.ok(receivedCredentials.every(value => value === `Bearer ${credential}`), "Captured environment headers reach authenticated introspection");
    denyRemote = true;
    await vscode.commands.executeCommand("graphqlDesk.refreshSchema");
    await until(() => vscode.commands.executeCommand("vscode.executeHoverProvider", unsaved.uri, unsavedPosition), value => !/Display name/.test(hoverText(value)), "Authentication failure must retire the last-good remote schema from analysis");
    assert.doesNotMatch(JSON.stringify(diagnostics(configDoc).concat(diagnostics(unsaved))), /TEST_ONLY_PRIVATE|PRIVATE_RESPONSE/);
    denyRemote = false; description = "Recovered remote name";
    await vscode.commands.executeCommand("graphqlDesk.refreshSchema");
    await until(() => vscode.commands.executeCommand("vscode.executeHoverProvider", unsaved.uri, unsavedPosition), value => /Recovered remote name/.test(hoverText(value)), "Explicit retry recovers the newly fetched authenticated schema");
    const beforeRotation = remoteRequests;
    credential = "TEST_ONLY_PRIVATE_ROTATED"; process.env[credentialName] = credential;
    await until(() => vscode.commands.executeCommand("vscode.executeHoverProvider", unsaved.uri, unsavedPosition), value => !/Recovered remote name/.test(hoverText(value)), "Credential rotation invalidates the old authenticated context before any fetch");
    assert.equal(remoteRequests, beforeRotation, "Credential changes do not enable implicit HTTP");
    description = "Rotated remote name";
    await vscode.commands.executeCommand("graphqlDesk.refreshSchema");
    await until(() => vscode.commands.executeCommand("vscode.executeHoverProvider", unsaved.uri, unsavedPosition), value => /Rotated remote name/.test(hoverText(value)), "Explicit refresh uses the current credential revision");
    assert.equal(receivedCredentials.at(-1), `Bearer ${credential}`);
    description = "Polled remote name";
    await replace(configDoc, remoteConfig + "schema_refresh_interval=1\n");
    await until(() => vscode.commands.executeCommand("vscode.executeHoverProvider", unsaved.uri, unsavedPosition), value => /Polled remote name/.test(hoverText(value)), "Trusted opt-in polling fetches the authenticated remote schema");
    await replace(configDoc, remoteConfig);
    await vscode.commands.executeCommand("vscode.executeHoverProvider", unsaved.uri, unsavedPosition);
  } finally {
    if (previousCredential === undefined) delete process.env[credentialName];
    else process.env[credentialName] = previousCredential;
    remote.closeAllConnections();
    await new Promise(resolve => remote.close(resolve));
  }
  assert.equal(main.isDirty, true); assert.equal(schemaDoc.isDirty, true); assert.equal(unsaved.isDirty, true);
  assert.equal(recovery.isDirty, true, "The edit-cycle fixture must remain unsaved in its original editor buffer");
  for (const [name, text] of Object.entries(fixtures)) assert.equal(await fs.readFile(path.join(workspace, name), "utf8"), text);
  await assert.rejects(fs.stat(unsavedPath), { code: "ENOENT" });
  await assert.rejects(fs.stat(path.join(workspace, "graphql_desk_generated")), { code: "ENOENT" });
  await require("../src/extension").deactivate();
  assert.ok(!(await vscode.commands.getCommands(true)).includes("graphqlDesk.extractFragment"));
  await fs.writeFile(resultPath, JSON.stringify({ outcome: "read-only-activation-observed", vscode: vscode.version, actualActivation: true, actualWorker: true, officialSelectedEnvironment: true, blankInterpreterOverride: true, diagnostics: true, configurationDiagnostics: true, completion: true, hover: true, definitions: true, configuredDirectives: true, dirtyConfiguration: true, dirtyFragment: true, sourceRepair: true, incompleteGraphqlRecovery: true, recoveryDirtyBufferPreserved: true, dirtySchema: true, associatedUntitled: true, explicitRemoteRefresh: true, authenticatedRemoteRecovery: true, credentialRotation: true, authenticatedPolling: true, remoteTypingRetention: true, noAutomaticHttp: true, nativePylanceCoexistence: true, diskUnchanged: true, dirtyBuffersPreserved: true }, null, 2));
}
module.exports = { run };
