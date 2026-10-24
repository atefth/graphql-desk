"use strict";

const fs = require("node:fs");
const path = require("node:path");
const { createHash, randomUUID } = require("node:crypto");
const { createOwnedAnalysisArtifacts, renderOwnedAnalysisArtifact } = require("./ownedAnalysisArtifacts");

const MANIFEST = "complete.json";
const FORMAT = 2;
const MAX_FILES = 4096;
const MAX_FILE_BYTES = 1024 * 1024;
const MAX_TOTAL_BYTES = 32 * 1024 * 1024;
const MAX_OPERATIONS = 8192;
const MAX_OPERATION_MANIFEST_BYTES = 16 * 1024 * 1024;
const MAX_GENERATION_MANIFEST_BYTES = 18 * 1024 * 1024;
const digest = bytes => createHash("sha256").update(bytes).digest("hex");
const normalized = value => process.platform === "win32" ? path.resolve(value).toLowerCase() : path.resolve(value);
const inside = (root, value) => {
  const relative = path.relative(root, value);
  return relative === "" || (!path.isAbsolute(relative) && relative !== ".." && !relative.startsWith(`..${path.sep}`));
};
const same = (left, right) => left.dev === right.dev && left.ino === right.ino;

const exactKeys = (value, keys) => value && Object.getPrototypeOf(value) === Object.prototype &&
  Object.keys(value).sort().join(",") === [...keys].sort().join(",");
const validSpan = value => exactKeys(value, ["start", "end"]) && Number.isSafeInteger(value.start) && value.start >= 0 &&
  Number.isSafeInteger(value.end) && value.end >= value.start;
const validModule = value => exactKeys(value, ["project", "module", "uri", "version"]) &&
  [value.project, value.module, value.uri].every(item => typeof item === "string" && item.length > 0 && item.length <= 4096) &&
  Number.isSafeInteger(value.version) && value.version >= 0;
const validIdentity = value => exactKeys(value, ["module", "codepoint", "utf16"]) &&
  validModule(value.module) && validSpan(value.codepoint) && validSpan(value.utf16);
const validIdentifier = value => typeof value === "string" && /^[\p{ID_Start}_][\p{ID_Continue}_]*$/u.test(value);
const coordinateDependencies = facts => facts === "unknown" || Array.isArray(facts) && facts.length <= 16384 &&
  facts.every(item => exactKeys(item, ["coordinate", "fingerprint"]) && typeof item.coordinate === "string" &&
    item.coordinate.length > 0 && item.coordinate.length <= 2048 && typeof item.fingerprint === "string" && /^[a-f0-9]{64}$/.test(item.fingerprint)) &&
  facts.every((item, index) => index === 0 || facts[index - 1].coordinate < item.coordinate);
const configDependencies = facts => facts === "unknown" || Array.isArray(facts) && facts.length <= 4096 &&
  facts.every(item => exactKeys(item, ["kind", "name", "value"]) && ["scalar-input", "scalar-output", "enum-mapping", "client-directive", "disable-masking"].includes(item.kind) &&
    typeof item.name === "string" && item.name.length > 0 && item.name.length <= 512 &&
    (item.value === null && ["scalar-input", "scalar-output", "enum-mapping"].includes(item.kind) ||
      typeof item.value === "boolean" && ["client-directive", "disable-masking"].includes(item.kind) ||
      typeof item.value === "string" && ["scalar-input", "scalar-output", "enum-mapping"].includes(item.kind) && item.value.length <= 4096)) &&
  facts.every((item, index) => index === 0 || facts[index - 1].kind < item.kind ||
    facts[index - 1].kind === item.kind && facts[index - 1].name < item.name);
const validExports = items => Array.isArray(items) && items.length <= 4096 && items.every(item => exactKeys(item, ["relative_path", "symbol", "alias"]) &&
  typeof item.relative_path === "string" && !item.relative_path.includes("\\") && !path.posix.isAbsolute(item.relative_path) &&
  item.relative_path.split("/").every(part => part && part !== "." && part !== "..") && validIdentifier(item.symbol) && validIdentifier(item.alias));
const validIdentities = items => items === null || Array.isArray(items) && items.length <= 8192 && items.every(validIdentity);

function validOperationManifest(value) {
  if (!Array.isArray(value) || value.length > MAX_FILES) return false;
  const owners = new Set();
  const projects = new Set();
  let operations = 0;
  for (const group of value) {
    if (!group || Object.getPrototypeOf(group) !== Object.prototype ||
        Object.keys(group).sort().join(",") !== "generated_package,operations,project" ||
        typeof group.project !== "string" || !group.project.length || group.project.length > 4096 ||
        typeof group.generated_package !== "string" || !/^[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*$/.test(group.generated_package) ||
        projects.has(group.project) || !Array.isArray(group.operations)) return false;
    projects.add(group.project);
    for (const operation of group.operations) {
      operations += 1;
      const expected = ["owner", "selector", "operation_name", "operation_kind", "schema_identity", "context_token", "binding", "status",
        "declaration_path", "declaration_symbol", "source_exports", "dependencies", "direct_fragments", "definition_owners",
        "canonical_owner", "selector_known", "source_dependencies_known", "mapping_dependencies", "schema_dependencies", "config_dependencies"];
      const owner = operation?.owner;
      if (operations > MAX_OPERATIONS || !exactKeys(operation, expected) || !validIdentity(owner) || owner.module.project !== group.project ||
          !(operation.selector === null || typeof operation.selector === "string" && operation.selector.length <= 512) ||
          !(operation.operation_name === null || typeof operation.operation_name === "string" && operation.operation_name.length <= 512) ||
          !(operation.operation_kind === null || ["query", "mutation", "subscription", "fragment"].includes(operation.operation_kind)) ||
          !(operation.schema_identity === null || Array.isArray(operation.schema_identity) && operation.schema_identity.length === 2 && operation.schema_identity.every(item => typeof item === "string" && item.length <= 512)) ||
          !(operation.context_token === null || typeof operation.context_token === "string" && operation.context_token.length <= 4096) ||
          !(operation.binding === null || typeof operation.binding === "string" && operation.binding.length <= 512) ||
          !["precise", "unavailable", "rejected"].includes(operation.status) ||
          !(operation.declaration_path === null || typeof operation.declaration_path === "string" && !operation.declaration_path.includes("\\") &&
            !path.posix.isAbsolute(operation.declaration_path) && operation.declaration_path.split("/").every(part => part && part !== "." && part !== "..") &&
            operation.declaration_path.startsWith(`${group.generated_package.replaceAll(".", "/")}/`) && operation.declaration_path.endsWith(".pyi")) ||
          !(operation.declaration_symbol === null || validIdentifier(operation.declaration_symbol)) ||
          ((operation.declaration_path === null) !== (operation.declaration_symbol === null)) ||
          !validExports(operation.source_exports) || !Array.isArray(operation.dependencies) || operation.dependencies.length > 16384 || !operation.dependencies.every(validModule) ||
          !validIdentities(operation.direct_fragments) || !validIdentities(operation.definition_owners) ||
          !(operation.canonical_owner === null || validIdentity(operation.canonical_owner)) ||
          typeof operation.selector_known !== "boolean" || typeof operation.source_dependencies_known !== "boolean" ||
          !(operation.mapping_dependencies === "unknown" || Array.isArray(operation.mapping_dependencies) && operation.mapping_dependencies.length <= 16384 && operation.mapping_dependencies.every(item => validModule(item) && item.project === group.project)) ||
          !coordinateDependencies(operation.schema_dependencies) || !configDependencies(operation.config_dependencies)) return false;
      const ownerKey = JSON.stringify(owner);
      if (owners.has(ownerKey)) return false;
      owners.add(ownerKey);
    }
  }
  try { return Buffer.byteLength(JSON.stringify(value), "utf8") <= MAX_OPERATION_MANIFEST_BYTES; }
  catch { return false; }
}

function generationFiles(vscode, workspace, directory) {
  const manifestPath = path.join(directory, MANIFEST);
  const stat = fs.lstatSync(manifestPath);
  if (stat.isSymbolicLink() || !stat.isFile() || stat.size > MAX_GENERATION_MANIFEST_BYTES) throw new Error("Invalid generation manifest");
  const manifestBytes = fs.readFileSync(manifestPath);
  const manifest = JSON.parse(manifestBytes.toString("utf8"));
  if (manifest?.owner !== "graphql-desk-analysis-generation" || manifest.format !== FORMAT ||
      manifest.id !== path.basename(directory) || !Number.isSafeInteger(manifest.sequence) || manifest.sequence < 1 ||
      !Array.isArray(manifest.files) || manifest.files.length > MAX_FILES ||
      typeof manifest.inputFingerprint !== "string" || !/^[a-f0-9]{64}$/.test(manifest.inputFingerprint) ||
      (manifest.savedSourceFingerprint !== undefined && (typeof manifest.savedSourceFingerprint !== "string" || !/^[a-f0-9]{64}$/.test(manifest.savedSourceFingerprint))) ||
      typeof manifest.artifactFingerprint !== "string" || !/^[a-f0-9]{64}$/.test(manifest.artifactFingerprint) ||
      !validOperationManifest(manifest.operationManifest)) throw new Error("Invalid generation manifest");
  const expected = new Map();
  const expectedDirectories = new Set();
  let totalBytes = 0;
  for (const item of manifest.files) {
    if (!item || typeof item.path !== "string" || typeof item.sha256 !== "string" || !/^[a-f0-9]{64}$/.test(item.sha256) ||
        item.path.includes("\\") || item.path.includes("\0") || path.posix.isAbsolute(item.path) ||
        item.path.split("/").some((part, index, parts) => !/^[A-Za-z_]\w*$/.test(part) && !(index === parts.length - 1 && /^[A-Za-z_]\w*\.pyi?$/.test(part))) ||
        !/\.pyi?$/.test(item.path) || expected.has(item.path)) throw new Error("Invalid generation manifest");
    const file = path.join(directory, ...item.path.split("/"));
    if (!inside(directory, file)) throw new Error("Invalid generation path");
    let parent = path.posix.dirname(item.path);
    while (parent !== ".") {
      expectedDirectories.add(parent);
      parent = path.posix.dirname(parent);
    }
    const fileStat = fs.lstatSync(file);
    if (fileStat.isSymbolicLink() || !fileStat.isFile() || fileStat.size > MAX_FILE_BYTES + 160 || totalBytes + fileStat.size > MAX_TOTAL_BYTES) throw new Error("Invalid generated file");
    const bytes = fs.readFileSync(file);
    totalBytes += bytes.length;
    if (digest(bytes) !== item.sha256 || !bytes.subarray(0, 36).toString("utf8").startsWith("# Generated by GraphQL Desk;")) throw new Error("Generation artifact changed");
    if (vscode.workspace.textDocuments.some(document => document.isDirty && ["file", "untitled"].includes(document.uri.scheme) &&
        (document.uri.fsPath && normalized(document.uri.fsPath) === normalized(file) || document.uri.toString() === vscode.Uri.file(file).toString()))) throw new Error("Generation contains user edits; preserved");
    expected.set(item.path, { file, bytes, identity: fileStat });
  }
  for (const item of expected.keys()) {
    if (item.endsWith(".py") && !expected.has(`${item}i`)) throw new Error("Generation runtime artifact is missing its paired stub");
  }
  for (const group of manifest.operationManifest) for (const operation of group.operations) {
    if (operation.declaration_path && !expected.has(operation.declaration_path)) throw new Error("Generation operation declaration is missing");
  }
  const actual = new Set();
  const directories = new Map();
  function visit(folder, relative = "") {
    for (const name of fs.readdirSync(folder)) {
      const file = path.join(folder, name);
      const child = fs.lstatSync(file);
      if (child.isSymbolicLink()) throw new Error("Generation contains a symbolic link; preserved");
      const childRelative = relative ? `${relative}/${name}` : name;
      if (child.isDirectory()) {
        if (!expectedDirectories.has(childRelative)) throw new Error("Generation contains a foreign directory; preserved");
        directories.set(childRelative, child); visit(file, childRelative);
      }
      else if (file !== manifestPath) actual.add(childRelative);
    }
  }
  visit(directory);
  if (actual.size !== expected.size || [...actual].some(file => !expected.has(file))) throw new Error("Generation contains foreign files; preserved");
  const fingerprint = digest(Buffer.from(JSON.stringify([...expected].map(([file, record]) => ({ path: file, sha256: digest(record.bytes) }))
    .sort((left, right) => left.path.localeCompare(right.path)))));
  if (fingerprint !== manifest.artifactFingerprint) throw new Error("Generation fingerprint mismatch");
  return { directory, manifest, manifestFingerprint: digest(manifestBytes), files: expected, directories, manifestBytes, manifestIdentity: stat, identity: fs.lstatSync(directory),
    verify: () => generationFiles(vscode, workspace, directory) };
}

function createAnalysisGenerations(vscode, workspaceRoot, managedRoot) {
  if (!path.isAbsolute(workspaceRoot) || !path.isAbsolute(managedRoot)) throw new Error("Invalid analysis generation location");
  const workspace = path.resolve(workspaceRoot);
  const root = path.resolve(managedRoot);
  if (!inside(workspace, root) || root === workspace) throw new Error("Analysis generation root must be inside its workspace");
  const generationsRoot = path.join(root, "generations");
  let publicationQueue = Promise.resolve();
  const initialRoot = fs.lstatSync(root);
  if (initialRoot.isSymbolicLink() || !initialRoot.isDirectory()) throw new Error("Analysis generation root is not safe");
  const workspaceStat = fs.lstatSync(workspace);
  if (workspaceStat.isSymbolicLink() || !workspaceStat.isDirectory()) throw new Error("Workspace path is not safe");
  const rootAncestors = [[workspace, workspaceStat]];
  let ancestor = workspace;
  for (const part of path.relative(workspace, root).split(path.sep).filter(Boolean)) {
    ancestor = path.join(ancestor, part);
    const stat = fs.lstatSync(ancestor);
    if (stat.isSymbolicLink() || !stat.isDirectory()) throw new Error("Analysis generation ancestor is not safe");
    rootAncestors.push([ancestor, stat]);
  }
  try { fs.mkdirSync(generationsRoot); } catch (error) { if (error.code !== "EEXIST") throw error; }
  const parentIdentity = fs.lstatSync(generationsRoot);
  if (parentIdentity.isSymbolicLink() || !parentIdentity.isDirectory()) throw new Error("Analysis generations directory is not safe");
  rootAncestors.push([generationsRoot, parentIdentity]);

  function currentGenerations(managed) {
    return fs.readdirSync(generationsRoot).flatMap(name => {
      if (!/^g-[0-9]+-[0-9a-f-]{36}$/.test(name)) return [];
      const directory = path.join(generationsRoot, name);
      try {
        const stat = fs.lstatSync(directory);
        if (stat.isSymbolicLink() || !stat.isDirectory()) return [];
        const generation = generationFiles(vscode, workspace, directory);
        return managed?.ownsGeneration(directory, generation.manifestFingerprint) ? [generation] : [];
      } catch { return []; }
    }).sort((left, right) => right.manifest.sequence - left.manifest.sequence);
  }

  function verifyRoot() {
    const owner = vscode.workspace.getWorkspaceFolder(vscode.Uri.file(root));
    if (vscode.workspace.isTrusted !== true || !owner || normalized(owner.uri.fsPath) !== normalized(workspace) ||
        rootAncestors.some(([directory, original]) => {
          const current = fs.lstatSync(directory);
          return current.isSymbolicLink() || !current.isDirectory() || !same(original, current);
        }) || !same(workspaceStat, fs.lstatSync(workspace)) || !same(initialRoot, fs.lstatSync(root)) ||
        !same(parentIdentity, fs.lstatSync(generationsRoot))) throw new Error("Analysis generation root changed; preserved");
  }

  function removeGeneration(generation) {
    verifyRoot();
    const preflight = generationFiles(vscode, workspace, generation.directory);
    if (preflight.manifest.id !== generation.manifest.id || !preflight.manifestBytes.equals(generation.manifestBytes) ||
        !same(preflight.identity, generation.identity) || preflight.files.size !== generation.files.size || preflight.directories.size !== generation.directories.size) return false;
    for (const [relative, record] of generation.files) {
      const current = preflight.files.get(relative);
      if (!current || !same(current.identity, record.identity) || !current.bytes.equals(record.bytes)) return false;
    }
    for (const [relative, identity] of generation.directories) {
      const current = preflight.directories.get(relative);
      if (!current || !same(current, identity)) return false;
    }
    const generationNow = fs.lstatSync(generation.directory);
    if (generationNow.isSymbolicLink() || !same(generation.identity, generationNow)) throw new Error("Generation directory changed; preserved");
    for (const [relative, record] of generation.files) {
      verifyRoot();
      if (!same(generation.identity, fs.lstatSync(generation.directory))) throw new Error("Generation directory changed; preserved");
      const file = path.join(generation.directory, ...relative.split("/"));
      const now = fs.lstatSync(file);
      if (now.isSymbolicLink() || !same(record.identity, now) || !record.bytes.equals(fs.readFileSync(file))) throw new Error("Generation artifact changed; preserved");
      if (vscode.workspace.textDocuments.some(document => document.isDirty && document.uri.fsPath && normalized(document.uri.fsPath) === normalized(file))) throw new Error("Generation contains user edits; preserved");
      fs.unlinkSync(file);
    }
    verifyRoot();
    const manifest = path.join(generation.directory, MANIFEST);
    const manifestStat = fs.lstatSync(manifest);
    if (manifestStat.isSymbolicLink() || !same(generation.manifestIdentity, manifestStat) || !generation.manifestBytes.equals(fs.readFileSync(manifest))) throw new Error("Generation manifest changed; preserved");
    fs.unlinkSync(manifest);
    const directories = [];
    function collect(folder) {
      for (const name of fs.readdirSync(folder)) {
        const child = path.join(folder, name);
        const stat = fs.lstatSync(child);
        if (stat.isSymbolicLink()) throw new Error("Generation contains a symbolic link; preserved");
        if (stat.isDirectory()) collect(child);
        directories.push([child, stat]);
      }
    }
    collect(generation.directory);
    for (const [directory, stat] of directories) {
      const now = fs.lstatSync(directory);
      if (!same(stat, now) || fs.readdirSync(directory).length) throw new Error("Generation directory changed; preserved");
      fs.rmdirSync(directory);
    }
    const current = fs.lstatSync(generation.directory);
    if (!same(generation.identity, current) || fs.readdirSync(generation.directory).length) throw new Error("Generation directory changed; preserved");
    fs.rmdirSync(generation.directory);
    return true;
  }

  async function recoverNow(managed, isCurrent, inputFingerprint, savedSourceFingerprint, operationManifest) {
    if (!managed.ownsSetting || typeof isCurrent !== "function" || isCurrent() !== true ||
        typeof inputFingerprint !== "string" || !/^[a-f0-9]{64}$/.test(inputFingerprint) ||
        typeof savedSourceFingerprint !== "string" || !/^[a-f0-9]{64}$/.test(savedSourceFingerprint) ||
        !validOperationManifest(operationManifest)) return false;
    verifyRoot();
    const newest = currentGenerations(managed).find(item => item.manifest.inputFingerprint === inputFingerprint &&
      item.manifest.savedSourceFingerprint === savedSourceFingerprint &&
      JSON.stringify(item.manifest.operationManifest) === JSON.stringify(operationManifest));
    if (!newest || path.resolve(managed.selectedSetting ? path.resolve(workspace, managed.selectedSetting) : "") === newest.directory) return false;
    if (isCurrent() !== true) return false;
    verifyRoot();
    return managed.selectGeneration(newest.directory, isCurrent, newest);
  }

  async function publishNow(packages, analysisId, isCurrent, managed, inputFingerprint, savedSourceFingerprint, operationManifest) {
    if (!managed.ownsSetting || typeof isCurrent !== "function" || isCurrent() !== true ||
        typeof inputFingerprint !== "string" || !/^[a-f0-9]{64}$/.test(inputFingerprint) ||
        typeof savedSourceFingerprint !== "string" || !/^[a-f0-9]{64}$/.test(savedSourceFingerprint) ||
        !validOperationManifest(operationManifest)) return false;
    verifyRoot();
    const existing = currentGenerations(managed);
    const groups = [...packages].filter(([, files]) => files.length).sort(([a], [b]) => a.localeCompare(b));
    let totalInputBytes = 0;
    const paths = new Set();
    const outputFiles = [];
    for (const [packageName, files] of groups) {
      if (!/^[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*$/.test(packageName)) throw new Error("Invalid generated package name");
      const prefix = `${packageName.replaceAll(".", "/")}/`;
      for (const file of files) {
        const parts = file.relative_path.split("/");
        const bytes = Buffer.byteLength(file.text, "utf8");
        if (paths.has(file.relative_path) || !file.relative_path.startsWith(prefix) || parts.some((part, index) => !/^[A-Za-z_]\w*$/.test(part) && !(index === parts.length - 1 && /^[A-Za-z_]\w*\.pyi?$/.test(part))) ||
        !/\.pyi?$/.test(file.relative_path) || bytes > MAX_FILE_BYTES || totalInputBytes + bytes > MAX_TOTAL_BYTES) throw new Error("Invalid or oversized generation artifact");
        paths.add(file.relative_path);
        totalInputBytes += bytes;
        outputFiles.push({ path: file.relative_path, sha256: digest(renderOwnedAnalysisArtifact(packageName, file.relative_path.slice(prefix.length), file.text)) });
      }
    }
    for (const file of paths) {
      if (file.endsWith(".py") && !paths.has(`${file}i`)) throw new Error("Runtime generation artifact is missing its paired stub");
    }
    outputFiles.sort((a, b) => a.path.localeCompare(b.path));
    const outputFingerprint = digest(Buffer.from(JSON.stringify(outputFiles)));
    if (existing[0]?.manifest.inputFingerprint === inputFingerprint &&
        existing[0]?.manifest.savedSourceFingerprint === savedSourceFingerprint &&
        existing[0]?.manifest.artifactFingerprint === outputFingerprint &&
        JSON.stringify(existing[0]?.manifest.operationManifest) === JSON.stringify(operationManifest) &&
        path.resolve(workspace, managed.selectedSetting) === existing[0].directory) return false;
    let sequence = Math.max(0, ...existing.map(item => item.manifest.sequence)) + 1;
    const id = `g-${sequence}-${randomUUID()}`;
    const directory = path.join(generationsRoot, id);
    if (isCurrent() !== true) return false;
    verifyRoot();
    fs.mkdirSync(directory);
    const directoryIdentity = fs.lstatSync(directory);
    const handles = [];
    let completed = false;
    let manifestTemporary;
    let manifestTemporaryBytes;
    let temporaryIdentity;
    try {
      for (const [packageName, files] of groups) {
        if (isCurrent() !== true) throw new Error("Superseded analysis");
        handles.push(createOwnedAnalysisArtifacts(vscode, workspace, directory, packageName, files));
      }
      const manifestFiles = [];
      for (const [packageName, files] of groups) for (const item of files) {
        const prefix = `${packageName.replaceAll(".", "/")}/`;
        if (!item.relative_path.startsWith(prefix)) throw new Error("Invalid generation artifact path");
        const relative = item.relative_path;
        const file = path.join(directory, ...relative.split("/"));
        const bytes = fs.readFileSync(file);
        manifestFiles.push({ path: relative, sha256: digest(bytes) });
      }
      manifestFiles.sort((a, b) => a.path.localeCompare(b.path));
      if (isCurrent() !== true) throw new Error("Superseded analysis");
      verifyRoot();
      const artifactFingerprint = digest(Buffer.from(JSON.stringify(manifestFiles)));
      const manifest = { owner: "graphql-desk-analysis-generation", format: FORMAT, id, sequence,
        analysisId: typeof analysisId === "string" && analysisId.length <= 256 ? analysisId : null, operationManifest,
        inputFingerprint, savedSourceFingerprint, artifactFingerprint, files: manifestFiles };
      const manifestBytes = Buffer.from(`${JSON.stringify(manifest, null, 2)}\n`, "utf8");
      manifestTemporaryBytes = manifestBytes;
      manifestTemporary = path.join(directory, `.graphql-desk-${randomUUID()}.tmp`);
      const descriptor = fs.openSync(manifestTemporary, "wx");
      temporaryIdentity = fs.fstatSync(descriptor);
      try { fs.writeFileSync(descriptor, manifestBytes); fs.fsyncSync(descriptor); }
      finally { fs.closeSync(descriptor); }
      if (isCurrent() !== true) { fs.unlinkSync(manifestTemporary); throw new Error("Superseded analysis"); }
      fs.renameSync(manifestTemporary, path.join(directory, MANIFEST));
      const verified = generationFiles(vscode, workspace, directory);
      completed = true;
      if (isCurrent() !== true) return false;
      verifyRoot();
      if (!await managed.recordGeneration(directory, verified.manifestFingerprint)) return false;
      if (isCurrent() !== true) return false;
      const selected = await managed.selectGeneration(directory, isCurrent, verified);
      if (!selected) return false;
      const obsolete = currentGenerations(managed).slice(2);
      for (const old of obsolete) {
        try {
          if (removeGeneration(old)) await managed.forgetGeneration(old.directory, old.manifestFingerprint);
        } catch { /* Keep any generation whose ownership is no longer exact. */ }
      }
      return true;
    } catch (error) {
      if (!completed) {
        if (manifestTemporary) try {
          const stat = fs.lstatSync(manifestTemporary);
          const bytes = fs.readFileSync(manifestTemporary);
          if (!stat.isSymbolicLink() && stat.isFile() && temporaryIdentity && same(temporaryIdentity, stat) &&
              manifestTemporaryBytes && bytes.length <= manifestTemporaryBytes.length && manifestTemporaryBytes.subarray(0, bytes.length).equals(bytes) &&
              !vscode.workspace.textDocuments.some(document => document.isDirty && document.uri.fsPath && normalized(document.uri.fsPath) === normalized(manifestTemporary))) fs.unlinkSync(manifestTemporary);
        } catch { /* Preserve unverified staging data. */ }
        for (const handle of handles.reverse()) try { await handle.cleanup(); } catch { /* Preserve any changed artifact. */ }
        try {
          verifyRoot();
          const current = fs.lstatSync(directory);
          if (same(directoryIdentity, current) && fs.readdirSync(directory).length === 0) fs.rmdirSync(directory);
        } catch { /* Preserve incomplete or changed staging data for manual inspection. */ }
      }
      throw error;
    }
  }

  function serialize(operation) {
    const pending = publicationQueue.then(operation, operation);
    publicationQueue = pending.catch(() => {});
    return pending;
  }

  return {
    publish: (...args) => serialize(() => publishNow(...args)),
    recover: (...args) => serialize(() => recoverNow(...args)),
    current: managed => currentGenerations(managed),
  };
}

module.exports = { createAnalysisGenerations, validOperationManifest };
