"use strict";

const assert = require("node:assert/strict");
const childProcess = require("node:child_process");
const fs = require("node:fs");
const Module = require("node:module");
const path = require("node:path");

const delay = ms => new Promise(resolve => setTimeout(resolve, ms));
const absolute = name => {
  assert.ok(path.isAbsolute(name));
  const normalized = path.normalize(name);
  return process.platform === "win32" ? normalized.toLowerCase() : normalized;
};
const canonical = name => absolute(fs.realpathSync(name));
const label = item => typeof item.label === "string" ? item.label : item.label.label;

async function run() {
  const vscode = require("vscode");
  const fixture = require("./expectations.json");
  const output = path.join(fixture.task, "host-evidence.json");
  const result = { vscode: vscode.version, trusted: vscode.workspace.isTrusted, workers: [], decorations: [], writes: [] };
  const beforeLoad = Module._load;
  const beforeSpawn = childProcess.spawn;
  const beforeFork = childProcess.fork;
  let watcher;
  let failure;

  try {
    assert.equal(vscode.version, fixture.vscode);
    assert.equal(vscode.workspace.isTrusted, fixture.trusted);
    assert.equal(vscode.workspace.workspaceFolders.length, 1);
    const folder = vscode.workspace.workspaceFolders[0];
    assert.equal(canonical(folder.uri.fsPath), canonical(fixture.workspace));
    assert.equal(absolute(vscode.workspace.getConfiguration("graphqlDesk", folder.uri).get("pythonPath")), absolute(fixture.python));
    const extension = vscode.extensions.getExtension(fixture.productId);
    assert.ok(extension);
    assert.equal(extension.packageJSON.version, fixture.productVersion);
    assert.equal(canonical(extension.extensionPath), canonical(fixture.product));
    assert.equal(extension.isActive, false);
    result.product = { id: extension.id, version: extension.packageJSON.version, path: extension.extensionPath };

    childProcess.spawn = function (file, args, options) {
      if (Array.isArray(args) && args.includes("graphql_desk.editor_worker")) {
        result.workers.push({ kind: "python", executable: absolute(file), args: [...args], cwd: options?.cwd });
      }
      return beforeSpawn.apply(this, arguments);
    };
    childProcess.fork = function (file) {
      if (typeof file === "string" && path.basename(file) === "renameServer.js") {
        result.workers.push({ kind: "rename", executable: canonical(file) });
      }
      return beforeFork.apply(this, arguments);
    };
    if (!fixture.trusted) {
      // Observe real editor decoration calls while forwarding their exact arguments.
      watcher = fs.watch(fixture.workspace, { recursive: true }, (event, name) => result.writes.push([event, name]));
      watcher.on("error", error => result.writes.push(["watcher-error", String(error)]));
      const productMain = canonical(path.join(fixture.product, "src/extension.js"));
      Module._load = function (request, parent) {
        const loaded = beforeLoad.apply(this, arguments);
        if (request !== "./highlighting" || !parent?.filename || canonical(parent.filename) !== productMain) return loaded;
        return { ...loaded, registerHighlighting(...args) {
          const registration = loaded.registerHighlighting(...args);
          return { ...registration, refresh(editor, token) {
            return registration.refresh({
              document: editor.document,
              setDecorations(decoration, ranges) {
                const value = editor.setDecorations(decoration, ranges);
                for (const range of ranges) result.decorations.push(editor.document.getText(range));
                return value;
              },
            }, token);
          } };
        } };
      };
    }

    const document = await vscode.workspace.openTextDocument(vscode.Uri.file(path.join(fixture.workspace, "main.py")));
    assert.equal(document.languageId, "python");
    const original = document.getText();
    const version = document.version;
    await vscode.window.showTextDocument(document, { preview: false });
    const position = document.positionAt(original.indexOf("deskLa") + "deskLa".length);
    const diagnostics = () => vscode.languages.getDiagnostics(document.uri).filter(item => item.source === "GraphQL Desk");
    const complete = async () => {
      const response = await vscode.commands.executeCommand("vscode.executeCompletionItemProvider", document.uri, position);
      return response?.items.find(item => label(item) === "deskLabel");
    };
    const deadline = performance.now() + 28_000;
    if (fixture.trusted) {
      do {
        const invalid = diagnostics().find(item => item.code === "graphql.validation" &&
          document.getText(item.range) === "noSuchDeskField");
        if (invalid) {
          assert.match(invalid.message, /Cannot query field "noSuchDeskField" on type "Desk"/);
          const item = await complete();
          if (item) {
            assert.equal(item.detail, "String!");
            assert.equal(item.insertText, "deskLabel");
            result.diagnostic = { code: invalid.code, message: invalid.message, text: document.getText(invalid.range) };
            result.completion = { label: label(item), detail: item.detail };
            break;
          }
        }
        await delay(150);
      } while (performance.now() < deadline);
      assert.ok(result.diagnostic && result.completion, "Expected schema diagnostic and completion");
      const workers = result.workers.filter(worker => worker.kind === "python");
      assert.ok(workers.length, "Expected isolated Python worker");
      for (const worker of workers) {
        assert.equal(worker.executable, absolute(fixture.python));
        assert.equal(canonical(worker.cwd), canonical(fixture.tmp));
        assert.deepEqual(worker.args.slice(0, 6), ["-I", "-B", "-X", "utf8", "-m", "graphql_desk.editor_worker"]);
      }
    } else {
      do {
        if (extension.isActive && result.decorations.includes("query") && result.decorations.includes("noSuchDeskField")) break;
        await delay(100);
      } while (performance.now() < deadline);
      assert.ok(result.decorations.includes("query") && result.decorations.includes("noSuchDeskField"), "Expected actual lexical decoration ranges");
      assert.equal(await complete(), undefined);
      await vscode.commands.executeCommand("graphqlDesk.refreshSchema");
      await delay(500);
      assert.deepEqual(diagnostics(), []);
      assert.deepEqual(result.workers, []);
      assert.deepEqual(result.writes, []);
      result.decorations = [...new Set(result.decorations)];
    }
    assert.equal(extension.isActive, true, "Python onLanguage event must activate the installed extension");
    assert.equal(vscode.workspace.isTrusted, fixture.trusted);
    assert.equal(document.getText(), original);
    assert.equal(document.version, version);
    assert.equal(document.isDirty, false);
    result.passed = true;
  } catch (error) {
    failure = error;
    result.error = String(error.stack ?? error);
  } finally {
    watcher?.close();
    Module._load = beforeLoad;
    childProcess.spawn = beforeSpawn;
    childProcess.fork = beforeFork;
    fs.writeFileSync(output, JSON.stringify(result, null, 2), { flag: "wx" });
  }
  if (failure) throw failure;
}

exports.run = run;
