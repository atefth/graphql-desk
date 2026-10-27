"use strict";

const path = require("node:path");
const fs = require("node:fs");
const { createHash } = require("node:crypto");
const { fileURLToPath } = require("node:url");
const { createAnalysisController } = require("./analysisController");
const { registerLanguageFeatures } = require("./languageFeatures");
const { registerHighlighting } = require("./highlighting");
const { createManagedStubPath } = require("./managedStubPath");
const { createOwnedAnalysisArtifacts } = require("./ownedAnalysisArtifacts");
const { createAnalysisGenerations } = require("./analysisGenerations");

function operationDependencyImpact(previous, current) {
  if (!Array.isArray(previous) || !Array.isArray(current)) return null;
  if ([...previous, ...current].some(group => !group || !Array.isArray(group.operations))) return null;
  const location = value => {
    if (typeof value !== "string") return value;
    let filename = value;
    try { if (value.startsWith("file:")) filename = fileURLToPath(value); }
    catch { return value; }
    if (!path.isAbsolute(filename)) return value;
    try { filename = fs.realpathSync.native(filename); } catch { filename = path.resolve(filename); }
    return process.platform === "win32" ? filename.toLowerCase() : filename;
  };
  const canonicalPaths = value => Array.isArray(value) ? value.map(canonicalPaths)
    : value && typeof value === "object" ? Object.fromEntries(Object.entries(value).map(([name, item]) =>
      [name, name === "uri" || name === "project" ? location(item) : canonicalPaths(item)])) : value;
  const same = (left, right) => JSON.stringify(canonicalPaths(left)) === JSON.stringify(canonicalPaths(right));
  const sameSchemaIdentity = (left, right) => left === null || right === null ? left === right
    : Array.isArray(left) && Array.isArray(right) && left.length === 2 && right.length === 2
      ? location(left[0]) === location(right[0]) && left[1] === right[1] : same(left, right);
  const key = item => {
    const module = item?.owner?.module;
    return module && typeof item.binding === "string"
      ? JSON.stringify([location(module.project), module.module, location(module.uri), item.binding, item.selector, item.operation_name, item.operation_kind])
      : null;
  };
  const flatten = manifest => manifest.flatMap(group => group.operations);
  const before = flatten(previous);
  const after = flatten(current);
  const previousKeyCounts = new Map();
  for (const item of before) {
    const identity = key(item);
    if (!identity) return null;
    previousKeyCounts.set(identity, (previousKeyCounts.get(identity) ?? 0) + 1);
  }
  const byKey = new Map();
  for (const item of after) {
    const identity = key(item);
    if (!identity) return null;
    const matches = byKey.get(identity) ?? [];
    matches.push(item);
    byKey.set(identity, matches);
  }
  const impacted = [];
  const impactedKeys = new Set();
  const markImpacted = owner => {
    const encoded = JSON.stringify(owner);
    if (!impactedKeys.has(encoded)) {
      impactedKeys.add(encoded);
      impacted.push(owner);
    }
  };
  const factsChanged = (left, right, name) => {
    const a = left[name];
    const b = right[name];
    if (!Array.isArray(a) || !Array.isArray(b)) return null;
    const encode = value => JSON.stringify(canonicalPaths(value));
    const leftMap = new Map(a.map(item => [name === "schema_dependencies" ? item.coordinate : `${item.kind}\0${item.name}`, name === "schema_dependencies" ? item.fingerprint : item.value]));
    const rightMap = new Map(b.map(item => [name === "schema_dependencies" ? item.coordinate : `${item.kind}\0${item.name}`, name === "schema_dependencies" ? item.fingerprint : item.value]));
    if (leftMap.size !== a.length || rightMap.size !== b.length) return null;
    const keys = new Set([...leftMap.keys(), ...rightMap.keys()]);
    return [...keys].some(item => encode(leftMap.get(item)) !== encode(rightMap.get(item)));
  };
  for (const old of before) {
    const identity = key(old);
    if (!identity) return null;
    if (previousKeyCounts.get(identity) !== 1) {
      markImpacted(old.owner);
      continue;
    }
    const matches = byKey.get(identity) ?? [];
    const next = matches.length === 1 ? matches[0] : null;
    if (!next) {
      markImpacted(old.owner);
      continue;
    }
    byKey.delete(identity);
    if (old.status !== "precise" || next.status !== "precise" ||
        !old.source_dependencies_known || !next.source_dependencies_known ||
        old.mapping_dependencies === "unknown" || next.mapping_dependencies === "unknown" ||
        old.schema_dependencies === "unknown" || next.schema_dependencies === "unknown" ||
        old.config_dependencies === "unknown" || next.config_dependencies === "unknown" ||
        !Array.isArray(old.schema_identity) || old.schema_identity.length !== 2 ||
        !Array.isArray(next.schema_identity) || next.schema_identity.length !== 2) {
      markImpacted(old.owner);
      continue;
    }
    const schemaChanged = factsChanged(old, next, "schema_dependencies");
    const configChanged = factsChanged(old, next, "config_dependencies");
    if (schemaChanged === null || configChanged === null ||
        schemaChanged || configChanged ||
        !same(old.mapping_dependencies, next.mapping_dependencies) ||
        !same(old.owner, next.owner) || !same(old.canonical_owner, next.canonical_owner) ||
        !same(old.definition_owners, next.definition_owners) || !same(old.direct_fragments, next.direct_fragments) ||
        !same(old.dependencies, next.dependencies) || !same(old.source_exports, next.source_exports) ||
        old.selector_known !== next.selector_known ||
        !sameSchemaIdentity(old.schema_identity, next.schema_identity)) markImpacted(old.owner);
  }
  return impacted;
}

function typingFingerprint(groups) {
  const files = [...groups].flatMap(([packageName, artifacts]) =>
    artifacts.filter(file => file.relative_path.endsWith(".pyi") || file.relative_path.endsWith(".typed"))
      .map(file => [packageName, file.relative_path,
        file.text.replace(/^# Generated by GraphQL Desk; analysis artifact [a-f0-9]{64}\n/, "")]))
    .sort(([a, aPath], [b, bPath]) => a.localeCompare(b) || aPath.localeCompare(bPath));
  return createHash("sha256").update(JSON.stringify(files)).digest("hex");
}

async function reconfigureWorkspaceRoots({ roots, folders, create, retire }) {
  const errors = [];
  for (const [folderRoot, promise] of [...roots]) {
    let current;
    try { current = await promise; }
    catch (error) { if (roots.get(folderRoot) === promise) roots.delete(folderRoot); errors.push(error); continue; }
    const folder = folders.find(item => path.resolve(item.uri.fsPath) === folderRoot);
    if (!folder) {
      if (roots.get(folderRoot) === promise) roots.delete(folderRoot);
      try { await retire(folderRoot, current); } catch (error) { errors.push(error); }
      continue;
    }
    let next;
    try { next = await create(folder); }
    catch (error) {
      if (roots.get(folderRoot) === promise) roots.delete(folderRoot);
      try { await retire(folderRoot, current); } catch (cleanupError) { errors.push(cleanupError); }
      errors.push(error);
      continue;
    }
    if (next.root === current.root) { next.cleanup(); continue; }
    const replacement = Promise.resolve(next);
    if (roots.get(folderRoot) === promise) roots.set(folderRoot, replacement);
    try { await retire(folderRoot, current); }
    catch (error) {
      if (roots.get(folderRoot) === replacement) roots.delete(folderRoot);
      next.cleanup();
      errors.push(error);
    }
  }
  if (errors.length) throw new AggregateError(errors, "One or more workspace stub roots could not be reconfigured");
}

let active;
function activate(context) {
  const vscode = require("vscode");
  let language;
  let highlighting;
  let timer;
  let pylanceRestartTimer;
  let disposed = false;
  let generation = 0;
  let refreshCancellation;
  let lastSkipReason;
  const outputs = new Map();
  const generations = new Map();
  const roots = new Map();
  const latestArtifacts = new Map();
  const invalidatedDiagnostics = vscode.languages.createDiagnosticCollection("graphql-desk-typing");
  const invalidatedDiagnosticUris = new Map();
  const configDiagnosticCollection = vscode.languages.createDiagnosticCollection("graphql-desk-config");
  const configDiagnosticsByWorkspace = new Map();
  function updateConfigDiagnostics(workspace, items) {
    const previous = configDiagnosticsByWorkspace.get(workspace) ?? new Map();
    const current = new Map();
    for (const item of items) {
      const diagnostics = current.get(item.uri) ?? [];
      const diagnostic = new vscode.Diagnostic(
        new vscode.Range(new vscode.Position(item.range.start.line, item.range.start.character),
          new vscode.Position(item.range.end.line, item.range.end.character)),
        item.message, vscode.DiagnosticSeverity.Error);
      diagnostic.source = "GraphQL Desk";
      diagnostic.code = item.code;
      diagnostics.push(diagnostic);
      current.set(item.uri, diagnostics);
    }
    configDiagnosticsByWorkspace.set(workspace, current);
    for (const uri of new Set([...previous.keys(), ...current.keys()])) {
      const merged = [...configDiagnosticsByWorkspace.values()].flatMap(byUri => byUri.get(uri) ?? []);
      if (merged.length) configDiagnosticCollection.set(vscode.Uri.parse(uri), merged);
      else configDiagnosticCollection.delete(vscode.Uri.parse(uri));
    }
  }
  const output = vscode.window.createOutputChannel("GraphQL Desk");
  // This command returns before Pylance finishes restarting, so coalesce adjacent writes.
  function schedulePylanceRestart() {
    clearTimeout(pylanceRestartTimer);
    pylanceRestartTimer = setTimeout(() => {
      pylanceRestartTimer = undefined;
      void vscode.commands.executeCommand("python.analysis.restartLanguageServer")
        .catch(error => output.appendLine(`Pylance restart failed: ${String(error)}`));
    }, 250);
  }
  function traceSkip(reason) {
    if (reason === lastSkipReason) return;
    lastSkipReason = reason;
    output.appendLine(`Live typing skipped: ${reason}`);
  }
  function managedRoot(folder, folderRoot) {
    let promise = roots.get(folderRoot);
    if (!promise) {
      promise = createManagedStubPath(vscode, folder, context.workspaceState);
      roots.set(folderRoot, promise);
      promise.catch(() => { if (roots.get(folderRoot) === promise) roots.delete(folderRoot); });
    }
    return promise;
  }
  async function retireRoot(folderRoot, managed) {
    latestArtifacts.delete(folderRoot);
    const prefix = `${folderRoot}\0${managed.root}\0`;
    for (const [key, handle] of outputs) if (key.startsWith(prefix)) {
      await handle.cleanup();
      outputs.delete(key);
    }
    managed.cleanup();
  }
  async function reconfigureRoots() {
    await reconfigureWorkspaceRoots({ roots, folders: vscode.workspace.workspaceFolders ?? [],
      create: folder => createManagedStubPath(vscode, folder, context.workspaceState), retire: retireRoot });
  }
  async function managedStubPathsAreCurrent() {
    if (!roots.size) return false;
    for (const [folderRoot, promise] of roots) {
      const folder = (vscode.workspace.workspaceFolders ?? []).find(item => path.resolve(item.uri.fsPath) === folderRoot);
      if (!folder) return false;
      try {
        const managed = await promise;
        if (!managed.stillOwnsSetting()) return false;
      } catch { return false; }
    }
    return true;
  }
  function clearInvalidatedDiagnostics(folderRoot) {
    for (const uri of invalidatedDiagnosticUris.get(folderRoot) ?? []) invalidatedDiagnostics.delete(vscode.Uri.parse(uri));
    invalidatedDiagnosticUris.delete(folderRoot);
  }
  function setInvalidatedDiagnostics(folderRoot, operations) {
    const byUri = new Map();
    for (const operation of operations) {
      const uri = controller.resolveUri(operation.owner.module.uri);
      const document = vscode.workspace.textDocuments.find(item => item.uri.toString() === uri.toString());
      const position = document?.positionAt(operation.owner.utf16.start) ?? new vscode.Position(0, 0);
      const diagnostic = new vscode.Diagnostic(new vscode.Range(position, position),
        `GraphQL Desk has suspended typing for ${operation.binding ?? operation.operation_name ?? "this GraphQL document"} until analysis succeeds.`,
        vscode.DiagnosticSeverity.Error);
      diagnostic.source = "GraphQL Desk";
      diagnostic.code = "typing-suspended";
      const key = uri.toString();
      byUri.set(key, [...(byUri.get(key) ?? []), diagnostic]);
    }
    const previous = invalidatedDiagnosticUris.get(folderRoot) ?? new Set();
    for (const uri of previous) if (!byUri.has(uri)) invalidatedDiagnostics.delete(vscode.Uri.parse(uri));
    for (const [uri, diagnostics] of byUri) invalidatedDiagnostics.set(vscode.Uri.parse(uri), diagnostics);
    invalidatedDiagnosticUris.set(folderRoot, new Set(byUri.keys()));
  }
  async function quarantinePublishedTyping(sourceUri, ticket, broad = false, sourceRanges = null,
    previousOverride = null, ownersOverride = null, preserveCurrent = false) {
    if (!sourceUri && broad) {
      const results = await Promise.all((vscode.workspace.workspaceFolders ?? []).map(folder =>
        quarantinePublishedTyping(folder.uri.toString(), ticket, true, null, null, null, preserveCurrent)));
      return results.flat().filter(Boolean);
    }
    const parsed = sourceUri ? vscode.Uri.parse(sourceUri) : null;
    if (!parsed || parsed.scheme !== "file") return;
    const folder = vscode.workspace.getWorkspaceFolder(parsed);
    if (!folder) return;
    const folderRoot = path.resolve(folder.uri.fsPath);
    let managed;
    try { managed = await managedRoot(folder, folderRoot); } catch { return; }
    if (disposed || ticket !== generation || !controller.isTrusted()) return;
    let previous = previousOverride ?? latestArtifacts.get(folderRoot);
    const key = `${folderRoot}\0${managed.root}`;
    let publisher = generations.get(key);
    if (!publisher && managed.ownsSetting) {
      publisher = createAnalysisGenerations(vscode, folderRoot, managed.root);
      generations.set(key, publisher);
    }
    if (publisher && managed.selectedSetting && (!previous || previous.managedRoot !== managed.root ||
        (!previousOverride && previous.selectedSetting !== managed.selectedSetting))) {
      const selectedDirectory = path.resolve(folderRoot, managed.selectedSetting);
      const selected = publisher.current(managed).find(item => path.resolve(item.directory) === selectedDirectory);
      if (selected) {
        const packageCounts = new Map();
        for (const group of selected.manifest.operationManifest) {
          packageCounts.set(group.generated_package, (packageCounts.get(group.generated_package) ?? 0) + 1);
        }
        if ([...packageCounts.values()].every(count => count === 1)) {
          previous = { managedRoot: managed.root, selectedSetting: managed.selectedSetting,
            operationManifest: selected.manifest.operationManifest,
            projects: selected.manifest.operationManifest.map(group => {
              const prefix = `${group.generated_package.replaceAll(".", "/")}/`;
              return { project: group.project, generated_package: group.generated_package,
                provenance: group.operations,
                files: [...selected.files].filter(([relative]) => relative.startsWith(prefix))
                  .map(([relative, record]) => ({ relative_path: relative, text: record.bytes.toString("utf8") })) };
            }),
            parserOverlay: [...selected.files].filter(([relative]) => relative.startsWith("graphql_desk-stubs/"))
              .map(([relative, record]) => ({ relative_path: relative, text: record.bytes.toString("utf8").replace(/^# Generated by GraphQL Desk; analysis artifact [^\n]*\n/, "") })),
            inputFingerprint: selected.manifest.inputFingerprint,
            savedSourceFingerprint: selected.manifest.savedSourceFingerprint,
            analysisId: selected.manifest.analysisId };
          latestArtifacts.set(folderRoot, previous);
        }
      }
    }
    if (!previous || previous.managedRoot !== managed.root ||
        (!previousOverride && previous.selectedSetting !== managed.selectedSetting)) {
      if (publisher && managed.ownsSetting) {
        const isCurrent = () => !disposed && ticket === generation && controller.isTrusted();
        await publisher.publish(new Map(), `quarantine-startup-${Date.now()}`, isCurrent, managed,
          "0".repeat(64), "0".repeat(64), []);
      }
      return [];
    }
    const operationManifest = previous.operationManifest;
    const normalizePath = value => process.platform === "win32" ? value.toLowerCase() : value;
    const sourcePath = parsed.scheme === "file"
      ? (() => { try { return normalizePath(fs.realpathSync.native(parsed.fsPath)); } catch { return normalizePath(path.resolve(parsed.fsPath)); } })() : null;
    const projectScope = new Set();
    const workspaceWide = broad && sourcePath === normalizePath(folderRoot);
    if (workspaceWide) {
      for (const group of operationManifest) projectScope.add(group.project);
    } else if (sourcePath) {
      const candidates = operationManifest.flatMap(group => group.operations.map(operation => operation.owner.module.project))
        .filter(project => project.startsWith("file:"))
        .map(project => {
          try { return { project, config: normalizePath(vscode.Uri.parse(project).fsPath) }; } catch { return null; }
        }).filter(Boolean);
      const matching = candidates.filter(({ config }) => {
        const relative = path.relative(path.dirname(config), sourcePath);
        return normalizePath(config) === sourcePath || (!path.isAbsolute(relative) && relative !== ".." && !relative.startsWith(`..${path.sep}`));
      });
      const deepest = Math.max(0, ...matching.map(({ config }) => path.dirname(config).length));
      for (const item of matching) if (path.dirname(item.config).length === deepest) projectScope.add(item.project);
    }
    const sameSource = uri => {
      try {
        const candidate = vscode.Uri.parse(uri);
        if (candidate.scheme !== "file") return false;
        let filename;
        try { filename = fs.realpathSync.native(candidate.fsPath); } catch { filename = path.resolve(candidate.fsPath); }
        return normalizePath(filename) === sourcePath;
      } catch { return false; }
    };
    const hasRanges = Array.isArray(sourceRanges) && sourceRanges.length > 0 && sourceRanges.every(range =>
      Number.isSafeInteger(range?.start) && range.start >= 0 && Number.isSafeInteger(range?.end) && range.end >= range.start);
    const touches = identity => hasRanges && sourceIdentities(identity).some(span => sourceRanges.some(range =>
      range.start === range.end ? range.start >= span.start && range.start <= span.end : range.start < span.end && range.end > span.start));
    const sourceIdentities = identity => {
      const moduleUri = identity?.module?.uri;
      const span = identity?.utf16;
      return moduleUri && span && sameSource(moduleUri) ? [span] : [];
    };
    const affected = ownersOverride ?? operationManifest.flatMap(group => group.operations).filter(operation => {
      if (!sourcePath || !projectScope.size) return true;
      if (broad) return projectScope.has(operation.owner.module.project);
      if (!operation.source_dependencies_known && projectScope.has(operation.owner.module.project)) return true;
      if (hasRanges) {
        const identities = [operation.owner, ...(operation.definition_owners ?? []), ...(operation.direct_fragments ?? [])];
        if (identities.some(touches)) return true;
        if (operation.mapping_dependencies === "unknown" ||
            operation.mapping_dependencies.some(item => sameSource(item.uri))) return true;
        if (identities.some(identity => sourceIdentities(identity).length)) return false;
      }
      return sameSource(operation.owner.module.uri) || operation.dependencies.some(item => sameSource(item.uri));
    });
    if (!affected.length) return broad && !previousOverride ? [{ folderRoot, previous }] : [];
    const projects = previous.projects.map(project => ({ ...project, provenance:
      operationManifest.find(group => group.project === project.project)?.operations ?? [],
      files: project.files.filter(file => file.relative_path.endsWith(".pyi")) }));
    const isCurrent = () => !disposed && ticket === generation && controller.isTrusted();
    const reply = await controller.quarantine({ workspace: folderRoot, projects,
      invalidated_owners: affected.map(item => item.owner), preserve_current: preserveCurrent });
    if (!isCurrent()) return;
    const selectedKeys = new Set(affected.map(item => JSON.stringify(item.owner)));
    const canonicalKeys = new Set(operationManifest.flatMap(group => group.operations)
      .filter(item => selectedKeys.has(JSON.stringify(item.owner)))
      .map(item => JSON.stringify(item.canonical_owner ?? item.owner)));
    const quarantinedOwners = operationManifest.flatMap(group => group.operations)
      .filter(item => canonicalKeys.has(JSON.stringify(item.canonical_owner ?? item.owner)));
    const quarantinedKeys = new Set(quarantinedOwners.map(item => JSON.stringify(item.owner)));
    const nextManifest = operationManifest.map(group => ({ ...group, operations: group.operations.map(item =>
      quarantinedKeys.has(JSON.stringify(item.owner)) ? { ...item, status: "unavailable" } : item) }));
    const packages = new Map();
    for (const group of operationManifest) packages.set(group.generated_package, []);
    packages.set("graphql_desk-stubs", [{ relative_path: "graphql_desk-stubs/__init__.pyi",
      text: "from graphql import DocumentNode as _DocumentNode, Source as _Source\ndef parse(source: str | _Source, no_location: bool = False, max_tokens: int | None = None, experimental_fragment_arguments: bool = False) -> _DocumentNode: ...\n" },
    { relative_path: "graphql_desk-stubs/py.typed", text: "partial\n" }]);
    let nextProjects;
    if (reply?.ok) {
      const quarantinedFiles = new Map(reply.projects.map(project => [project.project, new Map(project.files.map(file => [file.relative_path, file]))]));
      nextProjects = previous.projects.map(project => ({ ...project, files: project.files.map(file =>
        file.relative_path.endsWith(".pyi") ? quarantinedFiles.get(project.project)?.get(file.relative_path) ?? file : file) }));
      for (const project of nextProjects) for (const file of project.files) packages.get(project.generated_package).push(file);
    } else {
      // A failed quarantine drops every stub so no prior precise type remains selectable.
      for (const group of nextManifest) group.operations = group.operations.map(item =>
        ({ ...item, status: "unavailable", declaration_path: null, declaration_symbol: null }));
      packages.clear();
      for (const group of operationManifest) packages.set(group.generated_package, []);
      nextProjects = previous.projects.map(project => ({ ...project, files: [] }));
    }
    try {
      if (managed.ownsSetting) {
        await publisher.publish(packages, `quarantine-${Date.now()}`, isCurrent, managed,
          previous.inputFingerprint, previous.savedSourceFingerprint, nextManifest);
      } else {
        for (const [key, handle] of outputs) if (key.startsWith(`${folderRoot}\0${managed.root}\0`) && !packages.has(key.split("\0")[2])) {
          await handle.publish([], isCurrent);
        }
        for (const [packageName, files] of packages) {
          const key = `${folderRoot}\0${managed.root}\0${packageName}`;
          const handle = outputs.get(key);
          if (handle) await handle.publish(files, isCurrent);
        }
        schedulePylanceRestart();
      }
      if (isCurrent()) {
        latestArtifacts.set(folderRoot, { ...previous, selectedSetting: managed.selectedSetting,
          operationManifest: nextManifest, projects: nextProjects });
        setInvalidatedDiagnostics(folderRoot, reply?.ok ? quarantinedOwners : operationManifest.flatMap(group => group.operations));
      }
    } catch (error) {
      output.appendLine(`Typing quarantine failed: ${String(error)}`);
      traceSkip("typing-quarantine-failed");
    }
    return broad && !previousOverride ? [{ folderRoot, previous }] : [];
  }
  async function publishTypes(editor, ticket, token, quarantineContext) {
    const document = editor.document;
    const snapshot = controller.snapshot(document);
    let artifactFailureReason;
    const captureArtifactFailure = reason => { artifactFailureReason = reason; traceSkip(reason); };
    let result = await controller.artifacts(snapshot, token, captureArtifactFailure);
    if (!result && artifactFailureReason === "worker-artifacts-failed" &&
        !disposed && ticket === generation && !token?.isCancellationRequested) {
      result = await controller.artifacts(snapshot, token, captureArtifactFailure);
    }
    if (!result) return;
    if (disposed || ticket !== generation || !result.isCurrent()) { traceSkip("analysis-snapshot-stale"); return; }
    const folder = vscode.workspace.getWorkspaceFolder(document.uri);
    if (!folder) { traceSkip("document-outside-workspace"); return; }
    const folderRoot = path.resolve(folder.uri.fsPath);
    if (quarantineContext?.previousByFolder.has(folderRoot) && !quarantineContext.refined.has(folderRoot)) {
      quarantineContext.refined.add(folderRoot);
      const previous = quarantineContext.previousByFolder.get(folderRoot);
      const operations = operationDependencyImpact(previous.operationManifest, result.projects.map(
        ({ project, generated_package, provenance }) => ({ project, generated_package, operations: provenance })));
      if (operations?.length) {
        await quarantinePublishedTyping(document.uri.toString(), ticket, true, null, previous,
          operations.map(owner => ({ owner })), true);
      }
    }
    let managed;
    try {
      const managedPromise = managedRoot(folder, folderRoot);
      managed = await managedPromise;
    } catch {
      traceSkip("managed-stub-root-unavailable");
      status.text = "GraphQL Desk: managed typing unavailable";
      status.show();
      return;
    }
    if (disposed || ticket !== generation || !result.isCurrent()) { traceSkip("analysis-snapshot-stale"); return; }
    try {
      const groups = new Map();
      const operationManifest = result.projects.map(({ project, generated_package, provenance }) =>
        ({ project, generated_package, operations: provenance }));
      for (const project of result.projects) {
        const files = project.files;
        if (!groups.has(project.generated_package)) groups.set(project.generated_package, []);
        groups.get(project.generated_package).push(...files);
      }
      groups.set("graphql_desk-stubs", result.parser_overlay);
      const artifactTypingFingerprint = managed.ownsSetting ? undefined : typingFingerprint(groups);
      const typingChanged = artifactTypingFingerprint !== undefined &&
        latestArtifacts.get(folderRoot)?.typingFingerprint !== artifactTypingFingerprint;
      let changedArtifacts = false;
      if (managed.ownsSetting) {
        const key = `${folderRoot}\0${managed.root}`;
        let publisher = generations.get(key);
        if (!publisher) { publisher = createAnalysisGenerations(vscode, folderRoot, managed.root); generations.set(key, publisher); }
        changedArtifacts = await publisher.recover(managed, result.isCurrent,
          result.input_fingerprint, result.saved_source_fingerprint, operationManifest);
        if (disposed || ticket !== generation || !result.isCurrent()) { traceSkip("analysis-snapshot-stale-after-recovery"); return; }
        changedArtifacts = (await publisher.publish(groups, result.id, result.isCurrent, managed,
          result.input_fingerprint, result.saved_source_fingerprint, operationManifest)) || changedArtifacts;
      } else {
        for (const [key, handle] of outputs) if (key.startsWith(`${folderRoot}\0${managed.root}\0`) && !groups.has(key.split("\0")[2])) {
          changedArtifacts = (await handle.publish([], result.isCurrent)) || changedArtifacts;
        }
        for (const [packageName, files] of groups) {
          if (disposed || ticket !== generation || !result.isCurrent()) { traceSkip("analysis-snapshot-stale"); return; }
          const key = `${folderRoot}\0${managed.root}\0${packageName}`;
          let handle = outputs.get(key);
          if (!handle && files.length) {
            handle = createOwnedAnalysisArtifacts(vscode, folderRoot, managed.root, packageName, files);
            outputs.set(key, handle);
            changedArtifacts = true;
          } else if (handle) changedArtifacts = (await handle.publish(files, result.isCurrent)) || changedArtifacts;
        }
      }
      if (changedArtifacts && !managed.ownsSetting && typingChanged) schedulePylanceRestart();
      latestArtifacts.set(folderRoot, { managedRoot: managed.root, selectedSetting: managed.selectedSetting,
        operationManifest, projects: result.projects.map(({ project, generated_package, provenance, files }) =>
          ({ project, generated_package, provenance, files })),
        parserOverlay: result.parser_overlay,
        ...(artifactTypingFingerprint === undefined ? {} : { typingFingerprint: artifactTypingFingerprint }),
        inputFingerprint: result.input_fingerprint, savedSourceFingerprint: result.saved_source_fingerprint, analysisId: result.id });
      if (![...groups.values()].some(files => files.length)) traceSkip("no-generated-stub-files");
      else lastSkipReason = undefined;
      const unavailable = operationManifest.flatMap(group => group.operations)
        .filter(operation => operation.status !== "precise");
      if (unavailable.length) setInvalidatedDiagnostics(folderRoot, unavailable);
      else clearInvalidatedDiagnostics(folderRoot);
    } catch (error) {
      output.appendLine(`Managed stub publication failed: ${String(error)}`);
      traceSkip("managed-stub-publish-failed");
      status.text = "GraphQL Desk: managed typing unavailable";
      status.show();
    }
  }
  const refreshVisible = (sourceUri, invalidateTypes = Boolean(sourceUri), sourceRanges = null) => {
    generation += 1;
    const ticket = generation;
    const parsed = sourceUri ? vscode.Uri.parse(sourceUri) : null;
    const targetedSource = parsed?.scheme === "file" && path.extname(parsed.fsPath).toLowerCase() === ".py";
    const quarantineContext = { previousByFolder: new Map(), refined: new Set() };
    const quarantine = (invalidateTypes ? quarantinePublishedTyping(sourceUri, ticket, !targetedSource,
      sourceRanges, null, null, !sourceUri) : Promise.resolve([]))
      .then(results => { for (const item of results ?? []) quarantineContext.previousByFolder.set(item.folderRoot, item.previous); })
      .catch(error => { output.appendLine(`Typing quarantine failed: ${String(error)}`); traceSkip("typing-quarantine-failed"); });
    refreshCancellation?.cancel();
    refreshCancellation?.dispose();
    refreshCancellation = new vscode.CancellationTokenSource();
    const token = refreshCancellation.token;
    clearTimeout(timer);
    // Drop published semantic diagnostics immediately; rebuild after the edit settles.
    for (const document of vscode.workspace.textDocuments) language?.close(document.uri);
    timer = setTimeout(() => {
      if (disposed) return;
      const editors = vscode.window.visibleTextEditors.filter(editor => editor.document.languageId === "python");
      const publishers = new Set();
      const workspaceRoots = new Set();
      for (const editor of editors) {
        const folder = vscode.workspace.getWorkspaceFolder(editor.document.uri);
        if (!folder) continue;
        const root = path.resolve(folder.uri.fsPath);
        if (workspaceRoots.has(root)) continue;
        workspaceRoots.add(root);
        publishers.add(editor);
      }
      for (const editor of editors) {
        void Promise.allSettled([language.refresh(editor.document), highlighting.refresh(editor)]).then(() => {
          if (!publishers.has(editor) || disposed || ticket !== generation) return;
          return quarantine.then(() => publishTypes(editor, ticket, token, quarantineContext));
        });
      }
    }, 100);
  };
  const status = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Right, 0);
  status.name = "GraphQL Desk";
  status.command = "graphqlDesk.refreshSchema";
  status.tooltip = "GraphQL Desk: refresh a remote schema; positive schema_refresh_interval enables trusted background polling. Check configuration and interpreter settings if unavailable.";
  const controller = createAnalysisController(vscode, { changed: (sourceUri, ranges) => refreshVisible(sourceUri, true, ranges),
    configurationDiagnostics: updateConfigDiagnostics,
    status: message => {
      if (message) { status.text = `GraphQL Desk: ${message}`; status.show(); }
      else status.hide();
    }, report: message => vscode.window.showInformationMessage(message) });
  language = registerLanguageFeatures(vscode, controller);
  highlighting = registerHighlighting(vscode, controller);
  const visible = vscode.window.onDidChangeVisibleTextEditors(() => refreshVisible());
  const configChanged = vscode.workspace.onDidChangeConfiguration(event => {
    if ((vscode.workspace.workspaceFolders ?? []).some(folder =>
      event.affectsConfiguration("python.analysis.stubPath", folder.uri))) {
      // Our own generation switch selects already complete immutable output. Treat a
      // mismatch as an external setting edit and rebuild the manager around it.
      void managedStubPathsAreCurrent().then(ownedSelection => {
        if (ownedSelection) return;
        generation += 1;
        return reconfigureRoots().then(() => refreshVisible(null, true), () => { status.text = "GraphQL Desk: managed typing unavailable"; status.show(); refreshVisible(null, true); });
      });
    } else if (event.affectsConfiguration("graphqlDesk")) refreshVisible(null, true);
  });
  const foldersChanged = vscode.workspace.onDidChangeWorkspaceFolders(() => {
    generation += 1;
    const activeWorkspaces = new Set((vscode.workspace.workspaceFolders ?? []).map(folder => folder.uri.toString()));
    for (const workspace of configDiagnosticsByWorkspace.keys()) {
      if (!activeWorkspaces.has(workspace)) updateConfigDiagnostics(workspace, []);
    }
    void reconfigureRoots().then(refreshVisible, () => {
      status.text = "GraphQL Desk: managed typing unavailable";
      status.show();
      refreshVisible();
    });
  });
  const command = vscode.commands.registerCommand("graphqlDesk.refreshSchema", () => controller.refresh());
  const lifecycle = { async dispose() {
    if (disposed) return;
    disposed = true; generation += 1; refreshCancellation?.cancel(); refreshCancellation?.dispose(); clearTimeout(timer); clearTimeout(pylanceRestartTimer); command.dispose(); status.dispose(); visible.dispose(); foldersChanged.dispose();
    configChanged.dispose(); language.dispose(); highlighting.dispose(); controller.dispose(); output.dispose(); invalidatedDiagnostics.dispose(); configDiagnosticCollection.dispose();
    await Promise.all([...outputs.values()].map(handle => handle.cleanup().catch(() => {})));
    outputs.clear();
    const managedRoots = await Promise.all([...roots.values()].map(promise => promise.catch(() => null)));
    for (const managed of managedRoots) managed?.cleanup();
    roots.clear();
    latestArtifacts.clear();
    try { await vscode.commands.executeCommand("python.analysis.restartLanguageServer"); } catch { /* Pylance may already be deactivating. */ }
  } };
  context.subscriptions.push(lifecycle);
  active = lifecycle;
  refreshVisible(null, true);
}
async function deactivate() { await active?.dispose(); active = undefined; }
module.exports = { activate, deactivate, operationDependencyImpact, reconfigureWorkspaceRoots, typingFingerprint };
