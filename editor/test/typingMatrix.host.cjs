"use strict";
const assert = require("node:assert/strict");
const path = require("node:path");
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

exports.run = async () => {
  const results = { host: vscode.version, pylance: {}, lifecycle: {} };
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
    const consumer = await vscode.workspace.openTextDocument(consumerUri);
    await vscode.window.showTextDocument(consumer, { preview: false });
    await extension.activate();

    const uri = consumerUri;
    const document = consumer;
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
    await fs.writeFile(process.env.GRAPHQL_DESK_RESULT_PATH, JSON.stringify(results, null, 2));
    console.log(`GRAPHQL_DESK_TYPING_MATRIX ${JSON.stringify(results)}`);
  }
};
