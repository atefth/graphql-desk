"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs/promises");
const path = require("node:path");
const { execFileSync, spawnSync } = require("node:child_process");
const vscode = require("vscode");

const config = '[tool.graphql-desk]\nschema="schema.graphql"\nschema_output="graphql_desk_generated/default.pyi"\n';
const source = 'raise RuntimeError("Discovery must never execute project Python")\n';

async function run() {
  const workspace = process.env.GRAPHQL_DESK_TEST_WORKSPACE;
  const resultPath = process.env.GRAPHQL_DESK_RESULT_PATH;
  const python = process.env.GRAPHQL_DESK_PYTHON;
  for (const value of [workspace, resultPath, python]) assert.ok(value && path.isAbsolute(value));
  const root = vscode.Uri.file(workspace);
  const write = async (name, text) => {
    const target = path.join(workspace, name);
    await fs.mkdir(path.dirname(target), { recursive: true });
    await fs.writeFile(target, text, { encoding: "utf8", flag: "wx" });
  };
  const names = ["root.py", "untracked.py", "git_ignored/query.py", "ignore_ignored/query.py",
    "file_excluded/query.py", "search_excluded/query.py", "watcher_excluded/query.py",
    ".hidden/query.py", "nested/deeper/query.py", "café.py"];
  await write("pyproject.toml", config + 'exclude=[".hidden/**"]\n');
  await write("schema.graphql", "type Query { value: String }\n");
  await write(".gitignore", "git_ignored/\n");
  await write(".ignore", "ignore_ignored/\n");
  for (const name of names) await write(name, source);
  execFileSync("git", ["-c", "init.templateDir=", "-c", "core.hooksPath=", "init", "--quiet", workspace], { windowsHide: true });
  assert.equal(execFileSync("git", ["-C", workspace, "ls-files", "--others", "--exclude-standard", "untracked.py"], { encoding: "utf8", windowsHide: true }).trim(), "untracked.py");
  for (const [section, key, value] of [
    ["files", "exclude", { "**/file_excluded/**": true }],
    ["search", "exclude", { "**/search_excluded/**": true }],
    ["files", "watcherExclude", { "**/watcher_excluded/**": true }],
    ["search", "useIgnoreFiles", true],
    ["search", "experimental.useIgnoreFilesInFindFiles", true],
  ]) await vscode.workspace.getConfiguration(section, root).update(key, value, vscode.ConfigurationTarget.Workspace);
  const pattern = new vscode.RelativePattern(root, "**/*.py");
  const relative = (uri) => path.relative(workspace, uri.fsPath).split(path.sep).join("/");
  const find = async (exclude) => (await vscode.workspace.findFiles(pattern, exclude)).map(relative).sort();
  const found = await find(null);
  for (const name of names) assert.ok(found.includes(name), `null exclusions must retain ${name}`);
  let defaults;
  for (let attempt = 0; attempt < 20; attempt += 1) {
    defaults = await find(undefined);
    if (!["file_excluded/query.py", "git_ignored/query.py", "ignore_ignored/query.py"].some(name => defaults.includes(name))) break;
    await new Promise(resolve => setTimeout(resolve, 100));
  }
  for (const name of ["file_excluded/query.py", "git_ignored/query.py", "ignore_ignored/query.py"]) assert.ok(!defaults.includes(name), `default finder excludes ${name}`);
  assert.ok(defaults.includes("search_excluded/query.py"), "search.exclude is not the default findFiles filter");

  const events = [];
  const watcher = vscode.workspace.createFileSystemWatcher(pattern);
  const subscription = watcher.onDidCreate(uri => events.push(relative(uri)));
  try {
    await write("watch_control.py", source);
    await write("watcher_excluded/later.py", source);
    await new Promise(resolve => setTimeout(resolve, 750));
  } finally { subscription.dispose(); watcher.dispose(); }

  const associatedPath = path.join(workspace, "new_unsaved.py");
  const untitled = await vscode.workspace.openTextDocument(vscode.Uri.file(associatedPath).with({ scheme: "untitled" }));
  const edit = new vscode.WorkspaceEdit();
  edit.insert(untitled.uri, new vscode.Position(0, 0), "# caller-known associated buffer 😀\n" + source);
  assert.equal(await vscode.workspace.applyEdit(edit), true);
  assert.equal(untitled.isDirty, true);
  assert.equal(await fs.stat(associatedPath).then(() => true, error => { if (error.code === "ENOENT") return false; throw error; }), false);
  const diskCandidates = await find(null);
  assert.ok(!diskCandidates.includes("new_unsaved.py"));

  await write("symlink_project/pyproject.toml", config + 'source_roots=["src"]\n');
  await write("symlink_project/schema.graphql", "type Query { value: String }\n");
  await write("symlink_project/src/inside.py", source);
  await write("symlink_project/outside/query.py", source);
  let symlink = { supported: false };
  try {
    await fs.symlink(path.join(workspace, "symlink_project/src/inside.py"), path.join(workspace, "symlink_project/src/alias_inside.py"), "file");
    await fs.symlink(path.join(workspace, "symlink_project/outside/query.py"), path.join(workspace, "symlink_project/src/alias_escape.py"), "file");
    symlink = { supported: true };
  } catch (error) {
    if (!["EPERM", "EACCES", "ENOTSUP", "ENOSYS"].includes(error.code)) throw error;
    symlink.skipReason = error.code;
  }
  const program = `import json,sys
from pathlib import Path
from graphql_desk.config import load_project_config
from graphql_desk.source_selection import select_project_sources
from graphql_desk.editor_snapshot import OpenBuffer,acquire_editor_snapshot
p=json.load(sys.stdin); root=Path(p['workspace'])
paths=tuple(root/name for name in p['candidates'])
config=load_project_config(root/'pyproject.toml')
selected=select_project_sources(config,paths,workspace=root)
modules={s.module for s in selected}
assert {'git_ignored.query','ignore_ignored.query','file_excluded.query','search_excluded.query','watcher_excluded.query'} <= modules
assert all('.hidden' not in str(s.path.relative_to(root)) for s in selected)
buffer=OpenBuffer(root/'new_unsaved.py',p['version'],p['text'],True)
snapshot=acquire_editor_snapshot(workspace=root,candidates=paths+(buffer.path,),buffers=(buffer,),generation=1)
match=[m for m in snapshot.request['modules'] if m['module']=='new_unsaved']
assert len(match)==1 and match[0]['source']==buffer.text and match[0]['version']==buffer.version
assert not buffer.path.exists()
result={'coreSelected':sorted(modules),'unsavedBufferContent':True,'symlink':p['symlink']}
if p['symlink']['supported']:
 sub=root/'symlink_project'; cfg=load_project_config(sub/'pyproject.toml')
 inside=sub/'src/inside.py'; alias=sub/'src/alias_inside.py'
 assert len(select_project_sources(cfg,(inside,alias),workspace=root))==1
 try: select_project_sources(cfg,(sub/'src/alias_escape.py',),workspace=root)
 except ValueError: pass
 else: raise AssertionError('Symlink escaped configured source root')
 result['symlink'].update(insideDeduplicated=True,escapeRejected=True)
print(json.dumps(result,ensure_ascii=False))`;
  // Finite fixture candidates only; no Python import or execution of source fixtures.
  const child = spawnSync(python, ["-I", "-X", "utf8", "-c", program], {
    input: JSON.stringify({ workspace, candidates: diskCandidates,
      version: untitled.version, text: untitled.getText(), symlink }), encoding: "utf8", windowsHide: true,
  });
  assert.equal(child.status, 0, child.stderr || String(child.error));
  const core = JSON.parse(child.stdout);
  for (const name of names) assert.equal(await fs.readFile(path.join(workspace, name), "utf8"), source);
  assert.equal(untitled.isDirty, true);
  assert.equal(await fs.stat(associatedPath).then(() => true, error => { if (error.code === "ENOENT") return false; throw error; }), false);
  await fs.writeFile(resultPath, JSON.stringify({ outcome: "read-only-discovery-observed", vscode: vscode.version,
    nullExclusions: found, defaultExclusions: defaults, expectedDiskFixtures: names,
    watcherCreateEvents: events, watcherCompletenessClaim: false,
    unsaved: { callerKnownAssociation: true, finderReturned: false, explicitlyAdded: true, remainsDirty: true, diskAbsent: true },
    core, sourceFilesUnchanged: true }, null, 2), { encoding: "utf8", flag: "wx" });
}

module.exports = { run };
