"use strict";

const LIVE_CHECKS = new Set(["generated-ownership", "active-editor", "active-checker", "live-bridge"]);
const VERSION = /^[0-9A-Za-z][0-9A-Za-z.+_-]{0,127}$/;
const check = (name, status, message) => ({ name, status, message, project: null });
const trusted = vscode => vscode.workspace.isTrusted === true;
const plainObject = value => value && Object.getPrototypeOf(value) === Object.prototype;
const exactKeys = (value, keys) => plainObject(value) && Object.keys(value).sort().join(",") === [...keys].sort().join(",");
const text = value => typeof value === "string" && value.length > 0 && value.length <= 4096 && !/[\x00-\x1f\x7f]/.test(value);

function validWorkerHealth(value) {
  return exactKeys(value, ["checks", "runtime", "exit_code"]) && [0, 1].includes(value.exit_code) &&
    Array.isArray(value.checks) && value.checks.length <= 8192 && value.checks.every(item =>
      exactKeys(item, ["name", "status", "message", "project"]) && text(item.name) && text(item.message) &&
      ["ready", "degraded", "unavailable"].includes(item.status) && (item.project === null || text(item.project))) &&
    exactKeys(value.runtime, ["executable", "python_version", "graphql_desk_version", "mypy_version", "pyright_version"]) &&
    text(value.runtime.executable) && typeof value.runtime.python_version === "string" && VERSION.test(value.runtime.python_version) &&
    [value.runtime.graphql_desk_version, value.runtime.mypy_version, value.runtime.pyright_version]
      .every(version => version === null || typeof version === "string" && VERSION.test(version));
}

function extensionVersion(vscode, id) {
  try {
    const extension = vscode.extensions?.getExtension?.(id);
    const version = extension?.packageJSON?.version;
    return extension?.isActive === true && typeof version === "string" && VERSION.test(version) ? version : null;
  } catch { return null; }
}

function liveVersions(vscode) {
  return {
    vscode: typeof vscode.version === "string" && VERSION.test(vscode.version) ? vscode.version : null,
    graphql_desk: extensionVersion(vscode, "graphql-desk.graphql-desk-editor"),
    python: extensionVersion(vscode, "ms-python.python"),
    pylance: extensionVersion(vscode, "ms-python.vscode-pylance"),
  };
}

function versionChecks(versions) {
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
async function collectEditorDoctor(vscode, { folder, workerHealth, verifyPublication, isCurrent } = {}) {
  const unavailable = message => ({ checks: [check("live-bridge", "unavailable", message)], runtime: null,
    versions: liveVersions(vscode), exitCode: 1 });
  if (!trusted(vscode)) return unavailable("Trust the workspace before running editor Doctor.");
  const present = () => folder?.uri?.scheme === "file" &&
    vscode.workspace.workspaceFolders?.some(item => item === folder) === true;
  if (!present()) return unavailable("Select an owning local workspace folder and run Doctor again.");
  if (![workerHealth, verifyPublication, isCurrent].every(value => typeof value === "function")) {
    return unavailable("Live editor health is unavailable; refresh GraphQL Desk and run Doctor again.");
  }
  const current = async () => trusted(vscode) && present() && await isCurrent() === true && trusted(vscode) && present();
  try {
    if (!await current()) return unavailable("Editor inputs changed; wait for a fresh publication and run Doctor again.");
    const health = await workerHealth(folder);
    if (!await current()) return unavailable("Editor inputs changed during diagnosis; run Doctor again.");
    if (!validWorkerHealth(health)) return unavailable("The selected Python worker returned invalid health data; check its GraphQL Desk installation.");
    // An otherwise valid Python report cannot assert editor authority over the wire.
    const checks = health.checks.filter(item => !LIVE_CHECKS.has(item.name)).map(item => ({ ...item }));
    const runtime = { ...health.runtime };
    if (!["configuration", "project-ownership"].every(name => checks.some(item => item.name === name))) {
      checks.push(check("configuration", "unavailable", "The worker did not verify effective project configuration and ownership."));
    }
    const publication = await verifyPublication(folder);
    if (!await current()) return unavailable("Editor inputs changed during artifact verification; run Doctor again.");
    const verified = exactKeys(publication, ["ownership", "current"]) &&
      typeof publication.ownership === "boolean" && typeof publication.current === "boolean";
    checks.push(check("generated-ownership", verified && publication.ownership ? "ready" : "unavailable",
      verified && publication.ownership ? "Selected generated artifacts and effective import ownership are verified." :
        "Generated artifacts or the effective stub path are unverified; resolve ownership or recovery before retrying."));
    checks.push(check("live-bridge", verified && publication.ownership && publication.current ? "ready" : "degraded",
      verified && publication.ownership && publication.current ?
        "This editor session selected a verified generation matching its current captured inputs." :
        "No verified current publication is selected; refresh GraphQL Desk and run Doctor again."));
    const versions = liveVersions(vscode);
    checks.push(...versionChecks(versions));
    // The authority callbacks may await; no cached success crosses the final check.
    if (!await current()) return unavailable("Editor state changed before the report completed; run Doctor again.");
    if (JSON.stringify(liveVersions(vscode)) !== JSON.stringify(versions)) {
      return unavailable("Editor extensions changed during diagnosis; run Doctor again.");
    }
    return { checks, runtime, versions,
      exitCode: Number(checks.some(item => item.status !== "ready")) };
  } catch {
    return unavailable("Editor diagnosis failed; check the selected interpreter, project configuration and GraphQL Desk output, then retry.");
  }
}

module.exports = { collectEditorDoctor, validWorkerHealth };
