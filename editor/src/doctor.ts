import type { WorkspaceFolder } from "vscode";

type DoctorHost = Pick<typeof import("vscode"), "version" | "extensions" | "workspace">;
type CheckStatus = "ready" | "degraded" | "unavailable";
type Check = { name: string; status: CheckStatus; message: string; project: string | null };
type WorkerHealth = {
  checks: Check[];
  runtime: {
    executable: string;
    python_version: string;
    graphql_desk_version: string | null;
    mypy_version: string | null;
    pyright_version: string | null;
  };
  exit_code: 0 | 1;
};
type DoctorOptions = {
  folder?: WorkspaceFolder;
  workerHealth?: (folder: WorkspaceFolder) => unknown;
  verifyPublication?: (folder: WorkspaceFolder) => unknown;
  isCurrent?: () => boolean | PromiseLike<boolean>;
};

const LIVE_CHECKS = new Set(["generated-ownership", "active-editor", "active-python-extension", "active-checker", "live-bridge"]);
const VERSION = /^[0-9A-Za-z][0-9A-Za-z.+_-]{0,127}$/;
const check = (name: string, status: CheckStatus, message: string): Check => ({ name, status, message, project: null });
const trusted = (vscode: DoctorHost) => vscode.workspace.isTrusted === true;
const exactKeys = (value: unknown, keys: string[]): value is Record<string, unknown> =>
  !!value && Object.getPrototypeOf(value) === Object.prototype && Object.keys(value).sort().join(",") === [...keys].sort().join(",");
const text = (value: unknown): value is string => typeof value === "string" && value.length > 0 && value.length <= 4096 && !/[\x00-\x1f\x7f]/.test(value);

function validWorkerHealth(value: unknown) {
  return value && exactKeys(value, ["checks", "runtime", "exit_code"]) && (value.exit_code === 0 || value.exit_code === 1) &&
    Array.isArray(value.checks) && value.checks.length <= 8192 && value.checks.every((item: unknown) =>
      exactKeys(item, ["name", "status", "message", "project"]) && text(item.name) && text(item.message) &&
      (item.status === "ready" || item.status === "degraded" || item.status === "unavailable") && (item.project === null || text(item.project))) &&
    value.runtime && exactKeys(value.runtime, ["executable", "python_version", "graphql_desk_version", "mypy_version", "pyright_version"]) &&
    text(value.runtime.executable) && typeof value.runtime.python_version === "string" && VERSION.test(value.runtime.python_version) &&
    [value.runtime.graphql_desk_version, value.runtime.mypy_version, value.runtime.pyright_version]
      .every(version => version === null || typeof version === "string" && VERSION.test(version));
}

function isWorkerHealth(value: unknown): value is WorkerHealth {
  return validWorkerHealth(value) === true;
}

function extensionVersion(vscode: DoctorHost, id: string) {
  try {
    const extension = vscode.extensions?.getExtension?.(id);
    const version: unknown = extension?.packageJSON?.version;
    return extension?.isActive === true && typeof version === "string" && VERSION.test(version) ? version : null;
  } catch { return null; }
}

function liveVersions(vscode: DoctorHost) {
  return {
    vscode: typeof vscode.version === "string" && VERSION.test(vscode.version) ? vscode.version : null,
    graphql_desk: extensionVersion(vscode, "graphql-desk.graphql-desk-editor"),
    python: extensionVersion(vscode, "ms-python.python"),
    pylance: extensionVersion(vscode, "ms-python.vscode-pylance"),
  };
}

function versionChecks(versions: ReturnType<typeof liveVersions>) {
  return [
    check("active-editor", versions.vscode && versions.graphql_desk ? "ready" : "unavailable",
      versions.vscode && versions.graphql_desk ? `VS Code ${versions.vscode}; GraphQL Desk ${versions.graphql_desk}.` :
        "Run Doctor from the active GraphQL Desk extension in a supported VS Code host."),
    check("active-python-extension", versions.python ? "ready" : "unavailable", versions.python ?
      `Python extension ${versions.python}.` : "Activate the Python extension and select a Python environment."),
    check("active-checker", versions.pylance ? "ready" : "unavailable", versions.pylance ?
      `Pylance ${versions.pylance} is active; this does not acknowledge a specific typing generation.` :
      "Install or activate Pylance; an installed Pyright distribution does not establish an active editor checker."),
  ];
}

/** Callbacks are private live-session capabilities, never values from project files.
 * workerHealth uses the selected interpreter's existing read-only worker.
 * verifyPublication rechecks the selected owned artifacts against this session.
 * isCurrent must revalidate the same source/config/schema/interpreter observation.
 */
async function collectEditorDoctor(vscode: DoctorHost, { folder, workerHealth, verifyPublication, isCurrent }: DoctorOptions = {}) {
  const unavailable = (message: string) => ({ checks: [check("live-bridge", "unavailable", message)], runtime: null,
    versions: liveVersions(vscode), exitCode: 1 });
  if (!trusted(vscode)) return unavailable("Trust the workspace before running editor Doctor.");
  const present = () => folder?.uri?.scheme === "file" &&
    vscode.workspace.workspaceFolders?.some(item => item === folder) === true;
  if (!folder || !present()) return unavailable("Select an owning local workspace folder and run Doctor again.");
  if (typeof workerHealth !== "function" || typeof verifyPublication !== "function" || typeof isCurrent !== "function") {
    return unavailable("Live editor health is unavailable; refresh GraphQL Desk and run Doctor again.");
  }
  const current = async () => trusted(vscode) && present() && await isCurrent() === true && trusted(vscode) && present();
  try {
    if (!await current()) return unavailable("Editor inputs changed; wait for a fresh publication and run Doctor again.");
    const health = await workerHealth(folder);
    if (!await current()) return unavailable("Editor inputs changed during diagnosis; run Doctor again.");
    if (!isWorkerHealth(health)) return unavailable("The selected Python worker returned invalid health data; check its GraphQL Desk installation.");
    // An otherwise valid Python report cannot assert editor authority over the wire.
    const checks = health.checks.filter(item => !LIVE_CHECKS.has(item.name)).map(item => ({ ...item }));
    const runtime = { ...health.runtime };
    if (!["configuration", "project-ownership"].every(name => checks.some(item => item.name === name))) {
      checks.push(check("configuration", "unavailable", "The worker did not verify effective project configuration and ownership."));
    }
    const firstPublication = await verifyPublication(folder);
    if (!await current()) return unavailable("Editor inputs changed during artifact verification; run Doctor again.");
    const versions = liveVersions(vscode);
    if (!await current()) return unavailable("Editor state changed before the report completed; run Doctor again.");
    // Worker freshness checks await external I/O, so verify successful ownership again afterward.
    const publication = exactKeys(firstPublication, ["ownership", "current"]) &&
      firstPublication.ownership === true && typeof firstPublication.current === "boolean" ?
      await verifyPublication(folder) : firstPublication;
    if (!trusted(vscode) || !present() || JSON.stringify(liveVersions(vscode)) !== JSON.stringify(versions)) {
      return unavailable("Editor state or extensions changed during diagnosis; run Doctor again.");
    }
    const verified = exactKeys(publication, ["ownership", "current"]) &&
      typeof publication.ownership === "boolean" && typeof publication.current === "boolean";
    checks.push(check("generated-ownership", verified && publication.ownership ? "ready" : "unavailable",
      verified && publication.ownership ? "Selected generated artifacts and effective import ownership are verified." :
        "Generated artifacts or the effective stub path are unverified; resolve ownership or recovery before retrying."));
    checks.push(check("live-bridge", verified && publication.ownership && publication.current ? "ready" : "degraded",
      verified && publication.ownership && publication.current ?
        "This editor session selected a verified generation matching its current captured inputs." :
        "No verified current publication is selected; refresh GraphQL Desk and run Doctor again."));
    checks.push(...versionChecks(versions));
    return { checks, runtime, versions,
      exitCode: Number(checks.some(item => item.status !== "ready")) };
  } catch {
    return unavailable("Editor diagnosis failed; check the selected interpreter, project configuration and GraphQL Desk output, then retry.");
  }
}

export = { collectEditorDoctor, validWorkerHealth };
