type GenerationHost = Pick<typeof import("vscode"), "workspace" | "Uri" | "WorkspaceEdit" | "Range" | "Position">;
type Span = { start: number; end: number };
type ModuleIdentity = { project: string; module: string; uri: string; version: number };
type DocumentIdentity = { module: ModuleIdentity; codepoint: Span; utf16: Span };
type SourceExport = { relative_path: string; symbol: string; alias: string };
type CoordinateDependency = { coordinate: string; fingerprint: string };
type ConfigDependency = { kind: "scalar-input" | "scalar-output" | "enum-mapping"; name: string; value: string | null } |
  { kind: "client-directive" | "disable-masking"; name: string; value: boolean };
type Operation = { owner: DocumentIdentity; selector: string | null; operation_name: string | null;
  operation_kind: "query" | "mutation" | "subscription" | "fragment" | null; schema_identity: [string, string] | null;
  context_token: string | null; binding: string | null; status: "precise" | "unavailable" | "rejected";
  declaration_path: string | null; declaration_symbol: string | null; source_exports: SourceExport[];
  dependencies: ModuleIdentity[]; direct_fragments: DocumentIdentity[] | null; definition_owners: DocumentIdentity[] | null;
  canonical_owner: DocumentIdentity | null; selector_known: boolean; source_dependencies_known: boolean;
  mapping_dependencies: "unknown" | ModuleIdentity[]; schema_dependencies: "unknown" | CoordinateDependency[];
  config_dependencies: "unknown" | ConfigDependency[] };
type OperationManifest = { project: string; generated_package: string; operations: Operation[] }[];
type FileDigest = { path: string; sha256: string };
type StagingMarker = { owner: string; format: number; id: string; sequence: number; token: string; files: FileDigest[] };
type GenerationManifest = { owner: string; format: number; id: string; sequence: number; analysisId?: unknown;
  inputFingerprint: string; savedSourceFingerprint?: string; artifactFingerprint: string; files: FileDigest[];
  operationManifest: OperationManifest };
type ReadArtifact = { file: string; bytes: Buffer; identity: fs.Stats };
type ReadGeneration = { directory: string; manifest: GenerationManifest; manifestFingerprint: string;
  files: Map<string, ReadArtifact>; directories: Map<string, fs.Stats>; manifestBytes: Buffer;
  manifestIdentity: fs.Stats; identity: fs.Stats; verify(): ReadGeneration };
type StagingRecord = { fingerprint?: string; markerTemporary?: string };
type ManagedRoot = { readonly ownsSetting: boolean; readonly selectedSetting: string;
  ownsGeneration(directory: string, fingerprint: string): boolean;
  recordGeneration(directory: string, fingerprint: string): boolean | PromiseLike<boolean>;
  forgetGeneration(directory: string, fingerprint: string): boolean | PromiseLike<boolean>;
  ownsStaging(directory: string, fingerprint: string): boolean;
  recordStaging(directory: string, fingerprint: string, temporary: string): boolean | PromiseLike<boolean>;
  forgetStaging(directory: string, fingerprint: string): boolean | PromiseLike<boolean>;
  stagingEntries?(): Record<string, StagingRecord>;
  stillOwnsSetting?(): boolean;
  selectGeneration(directory: string, isCurrent: () => boolean, verification: ReadGeneration): boolean | PromiseLike<boolean> };
type ArtifactFile = { relative_path: string; text: string };
type Packages = Iterable<[string, readonly ArtifactFile[]]>;
type SelectionProof = Pick<ReadGeneration, "directory" | "manifestFingerprint" | "identity" | "manifestIdentity"> & { analysisId: unknown };

// The existing safe-integer guards also narrow untrusted JSON numbers.
declare const Number: Omit<NumberConstructor, "isSafeInteger"> & { (value?: unknown): number; isSafeInteger(value: unknown): value is number };

import fs = require("node:fs");
import path = require("node:path");
const { createHash, randomUUID }: typeof import("node:crypto") = require("node:crypto");
const { createOwnedAnalysisArtifacts, renderOwnedAnalysisArtifact }: typeof import("./ownedAnalysisArtifacts") = require("./ownedAnalysisArtifacts");

const MANIFEST = "complete.json";
const STAGING_SUFFIX = ".staging.json";
const FORMAT = 2;
const STAGING_FORMAT = 1;
const MAX_FILES = 4096;
const MAX_FILE_BYTES = 1024 * 1024;
const MAX_TOTAL_BYTES = 32 * 1024 * 1024;
const MAX_OPERATIONS = 8192;
const MAX_OPERATION_MANIFEST_BYTES = 16 * 1024 * 1024;
const MAX_GENERATION_MANIFEST_BYTES = 18 * 1024 * 1024;
const digest = (bytes: Buffer) => createHash("sha256").update(bytes).digest("hex");
const normalized = (value: string) => process.platform === "win32" ? path.resolve(value).toLowerCase() : path.resolve(value);
const inside = (root: string, value: string) => {
  const relative = path.relative(root, value);
  return relative === "" || (!path.isAbsolute(relative) && relative !== ".." && !relative.startsWith(`..${path.sep}`));
};
const same = (left: fs.Stats, right: fs.Stats) => left.dev === right.dev && left.ino === right.ino;

const exactKeys = <K extends string>(value: unknown, keys: readonly K[]): value is Record<K, unknown> => (value as boolean) && Object.getPrototypeOf(value) === Object.prototype &&
  Object.keys(value as object).sort().join(",") === [...keys].sort().join(",");
const validArtifactPath = (value: unknown): value is string => typeof value === "string" && !value.includes("\\") && !value.includes("\0") &&
  !path.posix.isAbsolute(value) &&
  (["graphql_desk-stubs/__init__.pyi", "graphql_desk-stubs/typed_document.pyi", "graphql_desk-stubs/fragment_refs.pyi",
    "graphql_desk-stubs/testing.pyi", "graphql_desk-stubs/py.typed"].includes(value) ||
    value.split("/").every((part, index) => /^[A-Za-z_]\w*$/.test(part) || index === value.split("/").length - 1 && /^[A-Za-z_]\w*\.pyi?$/.test(part)) && /\.pyi?$/.test(value));
function readStagingMarker(markerPath: string, id: string): { marker: StagingMarker; bytes: Buffer; identity: fs.Stats; fingerprint: string } {
  const stat = fs.lstatSync(markerPath);
  if (stat.isSymbolicLink() || !stat.isFile() || stat.size > 8 * 1024 * 1024) throw new Error("Invalid staging marker");
  const bytes = fs.readFileSync(markerPath);
  const marker: unknown = JSON.parse(bytes.toString("utf8"));
  const sequence = Number(/^g-(\d+)-/.exec(id)?.[1]);
  if (!exactKeys(marker, ["owner", "format", "id", "sequence", "token", "files"]) ||
      marker.owner !== "graphql-desk-analysis-staging" || marker.format !== STAGING_FORMAT || marker.id !== id ||
      !Number.isSafeInteger(sequence) || marker.sequence !== sequence ||
      typeof marker.token !== "string" || !/^[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}$/i.test(marker.token) ||
      !Array.isArray(marker.files) || marker.files.length > MAX_FILES ||
      marker.files.some(item => !exactKeys(item, ["path", "sha256"]) || !validArtifactPath(item.path) || typeof item.sha256 !== "string" || !/^[a-f0-9]{64}$/.test(item.sha256)) ||
      new Set(marker.files.map(item => item.path)).size !== marker.files.length ||
      marker.files.some((item, index) => index > 0 && (marker.files as FileDigest[])[index - 1].path >= item.path)) throw new Error("Invalid staging marker");
  return { marker: marker as StagingMarker, bytes, identity: stat, fingerprint: digest(bytes) };
}
const validSpan = (value: unknown): value is Span => exactKeys(value, ["start", "end"]) && Number.isSafeInteger(value.start) && value.start >= 0 &&
  Number.isSafeInteger(value.end) && value.end >= value.start;
const validModule = (value: unknown): value is ModuleIdentity => exactKeys(value, ["project", "module", "uri", "version"]) &&
  [value.project, value.module, value.uri].every(item => typeof item === "string" && item.length > 0 && item.length <= 4096) &&
  Number.isSafeInteger(value.version) && value.version >= 0;
const validIdentity = (value: unknown): value is DocumentIdentity => exactKeys(value, ["module", "codepoint", "utf16"]) &&
  validModule(value.module) && validSpan(value.codepoint) && validSpan(value.utf16);
const validIdentifier = (value: unknown): value is string => typeof value === "string" && /^[\p{ID_Start}_][\p{ID_Continue}_]*$/u.test(value);
const coordinateDependencies = (facts: unknown): facts is "unknown" | CoordinateDependency[] => facts === "unknown" || Array.isArray(facts) && facts.length <= 16384 &&
  facts.every(item => exactKeys(item, ["coordinate", "fingerprint"]) && typeof item.coordinate === "string" &&
    item.coordinate.length > 0 && item.coordinate.length <= 2048 && typeof item.fingerprint === "string" && /^[a-f0-9]{64}$/.test(item.fingerprint)) &&
  facts.every((item, index) => index === 0 || facts[index - 1].coordinate < item.coordinate);
const configDependencies = (facts: unknown): facts is "unknown" | ConfigDependency[] => facts === "unknown" || Array.isArray(facts) && facts.length <= 4096 &&
  facts.every(item => exactKeys(item, ["kind", "name", "value"]) && ["scalar-input", "scalar-output", "enum-mapping", "client-directive", "disable-masking"].includes(item.kind as string) &&
    typeof item.name === "string" && item.name.length > 0 && item.name.length <= 512 &&
    (item.value === null && ["scalar-input", "scalar-output", "enum-mapping"].includes(item.kind as string) ||
      typeof item.value === "boolean" && ["client-directive", "disable-masking"].includes(item.kind as string) ||
      typeof item.value === "string" && ["scalar-input", "scalar-output", "enum-mapping"].includes(item.kind as string) && item.value.length <= 4096)) &&
  facts.every((item, index) => index === 0 || facts[index - 1].kind < item.kind ||
    facts[index - 1].kind === item.kind && facts[index - 1].name < item.name);
const validExports = (items: unknown): items is SourceExport[] => Array.isArray(items) && items.length <= 4096 && items.every(item => exactKeys(item, ["relative_path", "symbol", "alias"]) &&
  typeof item.relative_path === "string" && !item.relative_path.includes("\\") && !path.posix.isAbsolute(item.relative_path) &&
  item.relative_path.split("/").every(part => part && part !== "." && part !== "..") && validIdentifier(item.symbol) && validIdentifier(item.alias));
const validIdentities = (items: unknown): items is DocumentIdentity[] | null => items === null || Array.isArray(items) && items.length <= 8192 && items.every(validIdentity);

function validOperationManifest(value: unknown): value is OperationManifest {
  if (!Array.isArray(value) || value.length > MAX_FILES) return false;
  const owners = new Set<string>();
  const projects = new Set<string>();
  let operations = 0;
  for (const group of value as Record<string, unknown>[]) {
    if (!group || Object.getPrototypeOf(group) !== Object.prototype ||
        Object.keys(group).sort().join(",") !== "generated_package,operations,project" ||
        typeof group.project !== "string" || !group.project.length || group.project.length > 4096 ||
        typeof group.generated_package !== "string" || !/^[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*$/.test(group.generated_package) ||
        projects.has(group.project) || !Array.isArray(group.operations)) return false;
    projects.add(group.project);
    for (const operation of group.operations as Record<string, unknown>[]) {
      operations += 1;
      const expected = ["owner", "selector", "operation_name", "operation_kind", "schema_identity", "context_token", "binding", "status",
        "declaration_path", "declaration_symbol", "source_exports", "dependencies", "direct_fragments", "definition_owners",
        "canonical_owner", "selector_known", "source_dependencies_known", "mapping_dependencies", "schema_dependencies", "config_dependencies"];
      const owner = operation?.owner;
      if (operations > MAX_OPERATIONS || !exactKeys(operation, expected) || !validIdentity(owner) || owner.module.project !== group.project ||
          !(operation.selector === null || typeof operation.selector === "string" && operation.selector.length <= 512) ||
          !(operation.operation_name === null || typeof operation.operation_name === "string" && operation.operation_name.length <= 512) ||
          !(operation.operation_kind === null || ["query", "mutation", "subscription", "fragment"].includes(operation.operation_kind as string)) ||
          !(operation.schema_identity === null || Array.isArray(operation.schema_identity) && operation.schema_identity.length === 2 && operation.schema_identity.every(item => typeof item === "string" && item.length <= 512)) ||
          !(operation.context_token === null || typeof operation.context_token === "string" && operation.context_token.length <= 4096) ||
          !(operation.binding === null || typeof operation.binding === "string" && operation.binding.length <= 512) ||
          !["precise", "unavailable", "rejected"].includes(operation.status as string) ||
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

function generationFiles(vscode: GenerationHost, workspace: string, directory: string): ReadGeneration {
  const directoryIdentity = fs.lstatSync(directory);
  if (directoryIdentity.isSymbolicLink() || !directoryIdentity.isDirectory()) throw new Error("Invalid generation directory");
  const manifestPath = path.join(directory, MANIFEST);
  const stat = fs.lstatSync(manifestPath);
  if (stat.isSymbolicLink() || !stat.isFile() || stat.size > MAX_GENERATION_MANIFEST_BYTES) throw new Error("Invalid generation manifest");
  const manifestBytes = fs.readFileSync(manifestPath);
  const manifest: GenerationManifest = JSON.parse(manifestBytes.toString("utf8"));
  if (manifest?.owner !== "graphql-desk-analysis-generation" || manifest.format !== FORMAT ||
      manifest.id !== path.basename(directory) || !Number.isSafeInteger(manifest.sequence) || manifest.sequence < 1 ||
      !Array.isArray(manifest.files) || manifest.files.length > MAX_FILES ||
      typeof manifest.inputFingerprint !== "string" || !/^[a-f0-9]{64}$/.test(manifest.inputFingerprint) ||
      (manifest.savedSourceFingerprint !== undefined && (typeof manifest.savedSourceFingerprint !== "string" || !/^[a-f0-9]{64}$/.test(manifest.savedSourceFingerprint))) ||
      typeof manifest.artifactFingerprint !== "string" || !/^[a-f0-9]{64}$/.test(manifest.artifactFingerprint) ||
      !validOperationManifest(manifest.operationManifest)) throw new Error("Invalid generation manifest");
  const expected = new Map<string, string | ReadArtifact>();
  const expectedDirectories = new Set<string>();
  for (const item of manifest.files) {
    if (!item || typeof item.path !== "string" || typeof item.sha256 !== "string" || !/^[a-f0-9]{64}$/.test(item.sha256) ||
        item.path.includes("\\") || item.path.includes("\0") || path.posix.isAbsolute(item.path) ||
        !(["graphql_desk-stubs/__init__.pyi", "graphql_desk-stubs/typed_document.pyi", "graphql_desk-stubs/fragment_refs.pyi",
          "graphql_desk-stubs/testing.pyi", "graphql_desk-stubs/py.typed"].includes(item.path) ||
          item.path.split("/").every((part, index, parts) => /^[A-Za-z_]\w*$/.test(part) || index === parts.length - 1 && /^[A-Za-z_]\w*\.pyi?$/.test(part)) && /\.pyi?$/.test(item.path)) ||
        expected.has(item.path)) throw new Error("Invalid generation manifest");
    if (!inside(directory, path.join(directory, ...item.path.split("/")))) throw new Error("Invalid generation path");
    let parent = path.posix.dirname(item.path);
    while (parent !== ".") {
      expectedDirectories.add(parent);
      parent = path.posix.dirname(parent);
    }
    expected.set(item.path, item.sha256);
  }
  for (const item of expected.keys()) {
    if (item.endsWith(".py") && !expected.has(`${item}i`)) throw new Error("Generation runtime artifact is missing its paired stub");
  }
  for (const group of manifest.operationManifest) for (const operation of group.operations) {
    if (operation.declaration_path && !expected.has(operation.declaration_path)) throw new Error("Generation operation declaration is missing");
  }
  const directories = new Map<string, fs.Stats>();
  let totalBytes = 0;
  let filesRead = 0;
  function visit(folder: string, relative = "") {
    const entries = fs.opendirSync(folder);
    try {
      for (let entry; (entry = entries.readSync()); ) {
        const file = path.join(folder, entry.name);
        const child = fs.lstatSync(file);
        if (child.isSymbolicLink()) throw new Error("Generation contains a symbolic link; preserved");
        const childRelative = relative ? `${relative}/${entry.name}` : entry.name;
        if (child.isDirectory()) {
          if (!expectedDirectories.has(childRelative)) throw new Error("Generation contains a foreign directory; preserved");
          directories.set(childRelative, child);
          visit(file, childRelative);
          continue;
        }
        if (file === manifestPath) {
          if (!child.isFile() || !same(child, stat)) throw new Error("Invalid generation manifest");
          continue;
        }
        const sha256 = expected.get(childRelative);
        if (typeof sha256 !== "string") throw new Error("Generation contains foreign files; preserved");
        if (!child.isFile() || child.size > MAX_FILE_BYTES + 160 || totalBytes + child.size > MAX_TOTAL_BYTES) {
          throw new Error("Invalid generated file");
        }
        if (vscode.workspace.textDocuments.some(document => document.isDirty && ["file", "untitled"].includes(document.uri.scheme) &&
            (document.uri.fsPath && normalized(document.uri.fsPath) === normalized(file) || document.uri.toString() === vscode.Uri.file(file).toString()))) {
          throw new Error("Generation contains user edits; preserved");
        }
        const bytes = fs.readFileSync(file);
        totalBytes += bytes.length;
        const marker = childRelative === "graphql_desk-stubs/py.typed";
        if (bytes.length !== child.size || digest(bytes) !== sha256 || (marker ? bytes.toString("utf8") !== "partial\n" :
          !bytes.subarray(0, 36).toString("utf8").startsWith("# Generated by GraphQL Desk;"))) throw new Error("Generation artifact changed");
        expected.set(childRelative, { file, bytes, identity: child });
        filesRead += 1;
      }
    } finally { entries.closeSync(); }
  }
  visit(directory);
  if (filesRead !== expected.size || !same(directoryIdentity, fs.lstatSync(directory))) throw new Error("Generation inventory changed; preserved");
  const currentManifest = fs.lstatSync(manifestPath);
  if (!currentManifest.isFile() || !same(stat, currentManifest) || stat.size !== currentManifest.size ||
      stat.mtimeMs !== currentManifest.mtimeMs || stat.ctimeMs !== currentManifest.ctimeMs) {
    throw new Error("Generation manifest changed; preserved");
  }
  const fingerprint = digest(Buffer.from(JSON.stringify(manifest.files.map(({ path, sha256 }) => ({ path, sha256 }))
    .sort((left, right) => left.path.localeCompare(right.path)))));
  if (fingerprint !== manifest.artifactFingerprint) throw new Error("Generation fingerprint mismatch");
  // Every expected file was visited once and replaced with its verified bytes and identity.
  return { directory, manifest, manifestFingerprint: digest(manifestBytes), files: expected as Map<string, ReadArtifact>, directories, manifestBytes, manifestIdentity: stat, identity: directoryIdentity,
    verify: () => generationFiles(vscode, workspace, directory) };
}

function createAnalysisGenerations(vscode: GenerationHost, workspaceRoot: string, managedRoot: string, onRemoved?: (file: string) => void) {
  if (!path.isAbsolute(workspaceRoot) || !path.isAbsolute(managedRoot)) throw new Error("Invalid analysis generation location");
  const workspace = path.resolve(workspaceRoot);
  const root = path.resolve(managedRoot);
  if (!inside(workspace, root) || root === workspace) throw new Error("Analysis generation root must be inside its workspace");
  const generationsRoot = path.join(root, "generations");
  let publicationQueue: Promise<unknown> = Promise.resolve();
  let selectedProof: SelectionProof | undefined;
  const initialRoot = fs.lstatSync(root);
  if (initialRoot.isSymbolicLink() || !initialRoot.isDirectory()) throw new Error("Analysis generation root is not safe");
  const workspaceStat = fs.lstatSync(workspace);
  if (workspaceStat.isSymbolicLink() || !workspaceStat.isDirectory()) throw new Error("Workspace path is not safe");
  const rootAncestors: [string, fs.Stats][] = [[workspace, workspaceStat]];
  let ancestor = workspace;
  for (const part of path.relative(workspace, root).split(path.sep).filter(Boolean)) {
    ancestor = path.join(ancestor, part);
    const stat = fs.lstatSync(ancestor);
    if (stat.isSymbolicLink() || !stat.isDirectory()) throw new Error("Analysis generation ancestor is not safe");
    rootAncestors.push([ancestor, stat]);
  }
  try { fs.mkdirSync(generationsRoot); } catch (error) { if ((error as NodeJS.ErrnoException).code !== "EEXIST") throw error; }
  const parentIdentity = fs.lstatSync(generationsRoot);
  if (parentIdentity.isSymbolicLink() || !parentIdentity.isDirectory()) throw new Error("Analysis generations directory is not safe");
  rootAncestors.push([generationsRoot, parentIdentity]);

  function currentGenerations(managed: ManagedRoot) {
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

  function rememberSelection({ directory, manifestFingerprint, identity, manifestIdentity }: Pick<ReadGeneration, "directory" | "manifestFingerprint" | "identity" | "manifestIdentity">, analysisId: unknown) {
    selectedProof = { directory, manifestFingerprint, identity, manifestIdentity, analysisId: analysisId ?? null };
  }

  function verification(managed: ManagedRoot) {
    const proof = selectedProof;
    if (!proof || !managed.ownsSetting || typeof managed.selectedSetting !== "string" ||
        path.resolve(workspace, managed.selectedSetting) !== proof.directory ||
        !managed.ownsGeneration(proof.directory, proof.manifestFingerprint)) return null;
    verifyRoot();
    const directory = fs.lstatSync(proof.directory);
    if (directory.isSymbolicLink() || !directory.isDirectory() || !same(directory, proof.identity)) return null;
    const checked = generationFiles(vscode, workspace, proof.directory);
    if (checked.manifestFingerprint !== proof.manifestFingerprint || !same(checked.identity, proof.identity) ||
        !same(checked.manifestIdentity, proof.manifestIdentity)) return null;
    return { directory: proof.directory, manifest: { inputFingerprint: checked.manifest.inputFingerprint },
      analysisId: proof.analysisId };
  }

  function hasDirtyDocument(file: string) {
    return vscode.workspace.textDocuments.some(document => document.isDirty &&
      ["file", "untitled"].includes(document.uri.scheme) &&
      (document.uri.fsPath && normalized(document.uri.fsPath) === normalized(file) ||
        document.uri.toString() === vscode.Uri.file(file).toString()));
  }

  function cleanupStagingTemporary(managed: ManagedRoot, id: string, fingerprint: string) {
    const entry = managed.stagingEntries?.()?.[id];
    const name = entry?.fingerprint === fingerprint ? entry.markerTemporary : null;
    if (typeof name !== "string" || !/^\.graphql-desk-[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}-marker-[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}\.tmp$/i.test(name)) return false;
    const temporary = path.join(generationsRoot, name);
    if (path.dirname(temporary) !== generationsRoot) return false;
    let identity;
    let bytes;
    try {
      identity = fs.lstatSync(temporary);
      if (identity.isSymbolicLink() || !identity.isFile() || identity.size > 8 * 1024 * 1024) return false;
      bytes = fs.readFileSync(temporary);
    } catch (error) { return (error as NodeJS.ErrnoException).code === "ENOENT"; }
    if (hasDirtyDocument(temporary)) return false;
    verifyRoot();
    const current = fs.lstatSync(temporary);
    if (!same(identity, current) || current.isSymbolicLink() || !bytes.equals(fs.readFileSync(temporary))) return false;
    fs.unlinkSync(temporary);
    return true;
  }

  async function cleanupStagedGeneration(managed: ManagedRoot, markerName: string) {
    if (!/^g-[0-9]+-[0-9a-f-]{36}\.staging\.json$/i.test(markerName)) return false;
    verifyRoot();
    const id = markerName.slice(0, -STAGING_SUFFIX.length);
    const directory = path.join(generationsRoot, id);
    const markerPath = path.join(generationsRoot, markerName);
    let marker;
    try { marker = readStagingMarker(markerPath, id); } catch { return false; }
    const markerOwned = managed.ownsStaging?.(directory, marker.fingerprint) === true;
    let directoryStat;
    try {
      directoryStat = fs.lstatSync(directory);
      if (directoryStat.isSymbolicLink() || !directoryStat.isDirectory()) return false;
    } catch (error) { if ((error as NodeJS.ErrnoException).code !== "ENOENT") return false; }
    if (!cleanupStagingTemporary(managed, id, marker.fingerprint)) return false;
    if (!directoryStat) {
      if (!markerOwned) return false;
      verifyRoot();
      const current = fs.lstatSync(markerPath);
      if (!same(marker.identity, current) || !marker.bytes.equals(fs.readFileSync(markerPath)) || hasDirtyDocument(markerPath)) return false;
      fs.unlinkSync(markerPath);
      await managed.forgetStaging?.(directory, marker.fingerprint);
      return true;
    }
    if (!markerOwned) return false;
    const manifestPath = path.join(directory, MANIFEST);
    if (fs.existsSync(manifestPath)) {
      let completed;
      try { completed = generationFiles(vscode, workspace, directory); } catch { return false; }
      const stagedFiles = new Map(marker.marker.files.map(item => [item.path, item.sha256]));
      if (stagedFiles.size !== completed.manifest.files.length ||
          completed.manifest.files.some(item => stagedFiles.get(item.path) !== item.sha256)) return false;
      if (!managed.ownsGeneration(directory, completed.manifestFingerprint) &&
          !await managed.recordGeneration(directory, completed.manifestFingerprint)) return false;
      verifyRoot();
      const current = fs.lstatSync(markerPath);
      if (!same(marker.identity, current) || !marker.bytes.equals(fs.readFileSync(markerPath)) || hasDirtyDocument(markerPath)) return false;
      fs.unlinkSync(markerPath);
      await managed.forgetStaging?.(directory, marker.fingerprint);
      return true;
    }

    const expected = new Map(marker.marker.files.map(item => [item.path, item.sha256]));
    const expectedDirectories = new Set<string>();
    for (const relative of expected.keys()) {
      let parent = path.posix.dirname(relative);
      while (parent !== ".") { expectedDirectories.add(parent); parent = path.posix.dirname(parent); }
    }
    const files: { file: string; bytes: Buffer; stat: fs.Stats }[] = [];
    const directories: { file: string; stat: fs.Stats }[] = [];
    let safe = true;
    const uuidPattern = "[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}";
    const stagingPattern = new RegExp(`^\\.graphql-desk-${marker.marker.token}-([0-9a-f]{16})-${uuidPattern}\\.tmp$`, "i");
    const manifestPattern = new RegExp(`^\\.graphql-desk-${marker.marker.token}-manifest-${uuidPattern}\\.tmp$`, "i");
    function visit(folder: string, relative = "") {
      for (const name of fs.readdirSync(folder)) {
        const file = path.join(folder, name);
        const stat = fs.lstatSync(file);
        if (stat.isSymbolicLink()) { safe = false; return; }
        const childRelative = relative ? `${relative}/${name}` : name;
        if (stat.isDirectory()) {
          if (!expectedDirectories.has(childRelative)) { safe = false; return; }
          directories.push({ file, stat });
          visit(file, childRelative);
          if (!safe) return;
          continue;
        }
        if (!stat.isFile()) { safe = false; return; }
        let staging = false;
        if (childRelative === MANIFEST) { safe = false; return; }
        if (name.match(manifestPattern) && relative === "") {
          if (stat.size > MAX_GENERATION_MANIFEST_BYTES) { safe = false; return; }
          staging = true;
        }
        else {
          const match = stagingPattern.exec(name);
          if (match) {
            staging = [...expected.keys()].some(target =>
              path.posix.dirname(target) === (relative || ".") &&
              createHash("sha256").update(target).digest("hex").slice(0, 16) === match[1].toLowerCase());
            if (stat.size > MAX_FILE_BYTES + 160) { safe = false; return; }
          }
        }
        const expectedDigest = expected.get(childRelative);
        if (!staging && expectedDigest === undefined) { safe = false; return; }
        if (!staging) {
          const bytes = fs.readFileSync(file);
          if (bytes.length > MAX_FILE_BYTES + 160 || digest(bytes) !== expectedDigest) { safe = false; return; }
        }
        if (vscode.workspace.textDocuments.some(document => document.isDirty &&
            document.uri.fsPath && normalized(document.uri.fsPath) === normalized(file))) { safe = false; return; }
        files.push({ file, bytes: fs.readFileSync(file), stat });
      }
    }
    try { visit(directory); } catch { return false; }
    if (!safe) return false;
    for (const item of files) {
      verifyRoot();
      const current = fs.lstatSync(item.file);
      if (!same(item.stat, current) || current.isSymbolicLink() || !item.bytes.equals(fs.readFileSync(item.file))) return false;
      fs.unlinkSync(item.file);
    }
    for (const item of directories.reverse()) {
      verifyRoot();
      const current = fs.lstatSync(item.file);
      if (!same(item.stat, current) || current.isSymbolicLink() || fs.readdirSync(item.file).length) return false;
      fs.rmdirSync(item.file);
    }
    verifyRoot();
    const currentDirectory = fs.lstatSync(directory);
    if (!same(directoryStat, currentDirectory) || fs.readdirSync(directory).length) return false;
    fs.rmdirSync(directory);
    verifyRoot();
    const currentMarker = fs.lstatSync(markerPath);
    if (!same(marker.identity, currentMarker) || !marker.bytes.equals(fs.readFileSync(markerPath)) || hasDirtyDocument(markerPath)) return false;
    fs.unlinkSync(markerPath);
    await managed.forgetStaging?.(directory, marker.fingerprint);
    return true;
  }

  async function cleanupStagedGenerations(managed: ManagedRoot) {
    if (!managed?.ownsSetting) return;
    verifyRoot();
    const names = fs.readdirSync(generationsRoot);
    for (const name of names) {
      if (name.endsWith(STAGING_SUFFIX)) await cleanupStagedGeneration(managed, name);
    }
    for (const [id, entry] of Object.entries(managed.stagingEntries?.() ?? {})) {
      const fingerprint = entry?.fingerprint;
      if (!/^g-[0-9]+-[0-9a-f-]{36}$/i.test(id) || !/^[a-f0-9]{64}$/.test(fingerprint ?? "")) continue;
      if (!cleanupStagingTemporary(managed, id, fingerprint!)) continue;
      if (names.includes(`${id}${STAGING_SUFFIX}`)) continue;
      const directory = path.join(generationsRoot, id);
      let exists = true;
      try {
        const stat = fs.lstatSync(directory);
        if (stat.isSymbolicLink() || !stat.isDirectory()) continue;
      } catch (error) {
        if ((error as NodeJS.ErrnoException).code !== "ENOENT") continue;
        exists = false;
      }
      if (!exists) {
        await managed.forgetStaging(directory, fingerprint!);
        continue;
      }
      try {
        const generation = generationFiles(vscode, workspace, directory);
        if (managed.ownsGeneration(directory, generation.manifestFingerprint)) {
          await managed.forgetStaging(directory, fingerprint!);
        }
      } catch { /* Preserve unverified staging state and its directory. */ }
    }
  }

  function removeGeneration(generation: ReadGeneration) {
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
      onRemoved?.(file);
    }
    verifyRoot();
    const manifest = path.join(generation.directory, MANIFEST);
    const manifestStat = fs.lstatSync(manifest);
    if (manifestStat.isSymbolicLink() || !same(generation.manifestIdentity, manifestStat) || !generation.manifestBytes.equals(fs.readFileSync(manifest))) throw new Error("Generation manifest changed; preserved");
    fs.unlinkSync(manifest);
    onRemoved?.(manifest);
    // The verified inventory visits parents first, so remove it in reverse.
    for (const [relative, identity] of [...generation.directories].reverse()) {
      const directory = path.join(generation.directory, ...relative.split("/"));
      const now = fs.lstatSync(directory);
      if (now.isSymbolicLink() || !same(identity, now) || fs.readdirSync(directory).length) throw new Error("Generation directory changed; preserved");
      fs.rmdirSync(directory);
      onRemoved?.(directory);
    }
    const current = fs.lstatSync(generation.directory);
    if (!same(generation.identity, current) || fs.readdirSync(generation.directory).length) throw new Error("Generation directory changed; preserved");
    fs.rmdirSync(generation.directory);
    onRemoved?.(generation.directory);
    return true;
  }

  async function recoverNow(managed: ManagedRoot, isCurrent?: () => boolean, inputFingerprint?: string, savedSourceFingerprint?: string, operationManifest?: unknown) {
    if (!managed.ownsSetting || typeof isCurrent !== "function" || isCurrent() !== true ||
        typeof inputFingerprint !== "string" || !/^[a-f0-9]{64}$/.test(inputFingerprint) ||
        typeof savedSourceFingerprint !== "string" || !/^[a-f0-9]{64}$/.test(savedSourceFingerprint) ||
        !validOperationManifest(operationManifest)) return false;
    await cleanupStagedGenerations(managed);
    verifyRoot();
    const newest = currentGenerations(managed).find(item => item.manifest.inputFingerprint === inputFingerprint &&
      item.manifest.savedSourceFingerprint === savedSourceFingerprint &&
      JSON.stringify(item.manifest.operationManifest) === JSON.stringify(operationManifest));
    if (!newest || path.resolve(managed.selectedSetting ? path.resolve(workspace, managed.selectedSetting) : "") === newest.directory) return false;
    if (isCurrent() !== true) return false;
    verifyRoot();
    return managed.selectGeneration(newest.directory, isCurrent, newest);
  }

  async function publishNow(packages: Packages, analysisId: unknown, isCurrent: (() => boolean) | undefined, managed: ManagedRoot, inputFingerprint?: string, savedSourceFingerprint?: string, operationManifest?: unknown) {
    if (!managed.ownsSetting || typeof isCurrent !== "function" || isCurrent() !== true ||
        typeof inputFingerprint !== "string" || !/^[a-f0-9]{64}$/.test(inputFingerprint) ||
        typeof savedSourceFingerprint !== "string" || !/^[a-f0-9]{64}$/.test(savedSourceFingerprint) ||
        !validOperationManifest(operationManifest)) return false;
    await cleanupStagedGenerations(managed);
    verifyRoot();
    const existing = currentGenerations(managed);
    const groups = [...packages].filter(([, files]) => files.length).sort(([a], [b]) => a.localeCompare(b));
    if (groups.reduce((count, [, files]) => count + files.length, 0) > MAX_FILES) throw new Error("Too many generation artifacts");
    let totalInputBytes = 0;
    const paths = new Set<string>();
    const outputFiles: FileDigest[] = [];
    for (const [packageName, files] of groups) {
      if (packageName !== "graphql_desk-stubs" && !/^[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*$/.test(packageName)) throw new Error("Invalid generated package name");
      const prefix = `${packageName.replaceAll(".", "/")}/`;
      for (const file of files) {
        const parts = file.relative_path.split("/");
        const bytes = Buffer.byteLength(file.text, "utf8");
        const parserOverlayFile = packageName === "graphql_desk-stubs" && ["graphql_desk-stubs/__init__.pyi", "graphql_desk-stubs/typed_document.pyi",
          "graphql_desk-stubs/fragment_refs.pyi", "graphql_desk-stubs/testing.pyi", "graphql_desk-stubs/py.typed"].includes(file.relative_path);
        if (paths.has(file.relative_path) || !file.relative_path.startsWith(prefix) || !parserOverlayFile &&
        (parts.some((part, index) => !/^[A-Za-z_]\w*$/.test(part) && !(index === parts.length - 1 && /^[A-Za-z_]\w*\.pyi?$/.test(part))) || !/\.pyi?$/.test(file.relative_path)) ||
        bytes > MAX_FILE_BYTES || totalInputBytes + bytes > MAX_TOTAL_BYTES) throw new Error("Invalid or oversized generation artifact");
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
        path.resolve(workspace, managed.selectedSetting) === existing[0].directory) {
      if (isCurrent() === true) rememberSelection(existing[0], analysisId);
      return false;
    }
    // Leftover directories are never adopted, but their names still reserve their sequence.
    const diskSequences = fs.readdirSync(generationsRoot).map(name => {
      const match = /^g-(\d+)-[0-9a-f-]{36}$/.exec(name);
      const value = match ? Number(match[1]) : NaN;
      return Number.isSafeInteger(value) ? value : 0;
    });
    const sequence = Math.max(0, ...existing.map(item => item.manifest.sequence), ...diskSequences) + 1;
    if (!Number.isSafeInteger(sequence)) throw new Error("Analysis generation sequence is exhausted");
    const id = `g-${sequence}-${randomUUID()}`;
    const directory = path.join(generationsRoot, id);
    const stagingToken = randomUUID();
    const markerPath = path.join(generationsRoot, `${id}${STAGING_SUFFIX}`);
    const markerTemporaryName = `.graphql-desk-${stagingToken}-marker-${randomUUID()}.tmp`;
    const markerTemporary = path.join(generationsRoot, markerTemporaryName);
    const stagingFiles = [...outputFiles].sort((left, right) => left.path < right.path ? -1 : left.path > right.path ? 1 : 0);
    const markerBytes = Buffer.from(`${JSON.stringify({
      owner: "graphql-desk-analysis-staging",
      format: STAGING_FORMAT,
      id,
      sequence,
      token: stagingToken,
      files: stagingFiles,
    }, null, 2)}\n`, "utf8");
    const markerFingerprint = digest(markerBytes);
    let markerIdentity;
    if (isCurrent() !== true) return false;
    verifyRoot();
    const handles: ReturnType<typeof createOwnedAnalysisArtifacts>[] = [];
    let completed = false;
    let manifestTemporary: string | undefined;
    let manifestTemporaryBytes: Buffer | undefined;
    let temporaryIdentity: fs.Stats | undefined;
    let stagingRecorded = false;
    try {
      if (!await managed.recordStaging(directory, markerFingerprint, markerTemporaryName)) {
        const retained = managed.stagingEntries?.()?.[id]?.fingerprint === markerFingerprint;
        throw new Error(`Could not record staging ownership (stubPathOwned=${managed.stillOwnsSetting?.() === true}; markerRetained=${retained})`);
      }
      stagingRecorded = true;
      const markerDescriptor = fs.openSync(markerTemporary, "wx");
      try { fs.writeFileSync(markerDescriptor, markerBytes); fs.fsyncSync(markerDescriptor); }
      finally { fs.closeSync(markerDescriptor); }
      fs.linkSync(markerTemporary, markerPath);
      markerIdentity = fs.lstatSync(markerPath);
      fs.unlinkSync(markerTemporary);
      if (isCurrent() !== true) throw new Error("Superseded analysis");
      verifyRoot();
      fs.mkdirSync(directory);
      const directoryIdentity = fs.lstatSync(directory);
      for (const [packageName, files] of groups) {
        if (isCurrent() !== true) throw new Error("Superseded analysis");
        handles.push(createOwnedAnalysisArtifacts(vscode, workspace, directory, packageName, files, stagingToken));
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
      manifestTemporary = path.join(directory, `.graphql-desk-${stagingToken}-manifest-${randomUUID()}.tmp`);
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
      verifyRoot();
      const currentMarker = fs.lstatSync(markerPath);
      if (!markerIdentity || !same(markerIdentity, currentMarker) || !markerBytes.equals(fs.readFileSync(markerPath)) || hasDirtyDocument(markerPath)) throw new Error("Analysis staging marker changed or is open with unsaved edits; preserved");
      fs.unlinkSync(markerPath);
      await managed.forgetStaging(directory, digest(markerBytes));
      if (isCurrent() !== true) return false;
      const selected = await managed.selectGeneration(directory, isCurrent, verified);
      if (!selected) return false;
      if (isCurrent() === true) rememberSelection(verified, analysisId);
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
              !vscode.workspace.textDocuments.some(document => document.isDirty && document.uri.fsPath && normalized(document.uri.fsPath) === normalized(manifestTemporary!))) fs.unlinkSync(manifestTemporary);
        } catch { /* Preserve unverified staging data. */ }
        for (const handle of handles.reverse()) try { await handle.cleanup(); } catch { /* Preserve any changed artifact. */ }
        try { await cleanupStagedGeneration(managed, `${id}${STAGING_SUFFIX}`); } catch { /* Preserve incomplete or changed staging data for manual inspection. */ }
        if (markerIdentity && !fs.existsSync(directory)) try {
          verifyRoot();
          const current = fs.lstatSync(markerPath);
          const bytes = fs.readFileSync(markerPath);
          if (same(markerIdentity, current) && bytes.length <= markerBytes.length && markerBytes.subarray(0, bytes.length).equals(bytes) && !hasDirtyDocument(markerPath)) fs.unlinkSync(markerPath);
        } catch { /* Preserve unverified marker bytes. */ }
        if (stagingRecorded && !fs.existsSync(markerPath) && !fs.existsSync(directory)) {
          if (cleanupStagingTemporary(managed, id, digest(markerBytes))) {
            await managed.forgetStaging(directory, digest(markerBytes));
          }
        } else if (stagingRecorded && fs.existsSync(markerPath)) {
          try {
            const existing = readStagingMarker(markerPath, id);
            if (existing.fingerprint !== digest(markerBytes) && cleanupStagingTemporary(managed, id, digest(markerBytes))) {
              await managed.forgetStaging(directory, digest(markerBytes));
            }
          } catch {
            if (cleanupStagingTemporary(managed, id, digest(markerBytes))) {
              await managed.forgetStaging(directory, digest(markerBytes));
            }
          }
        }
      }
      throw error;
    }
  }

  function serialize<T>(operation: () => T | PromiseLike<T>): Promise<T> {
    const pending = publicationQueue.then(operation, operation);
    publicationQueue = pending.catch(() => {});
    return pending;
  }

  return {
    publish: (...args: Parameters<typeof publishNow>) => serialize(() => publishNow(...args)),
    recover: (...args: Parameters<typeof recoverNow>) => serialize(() => recoverNow(...args)),
    current: (managed: ManagedRoot) => currentGenerations(managed),
    verification,
  };
}

export = { createAnalysisGenerations, generationFiles, validOperationManifest, MAX_GENERATION_MANIFEST_BYTES };
