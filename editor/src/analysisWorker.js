"use strict";

const { spawn: spawnProcess } = require("node:child_process");
const os = require("node:os");
const path = require("node:path");
const { TextDecoder } = require("node:util");
const { validOperationManifest } = require("./analysisGenerations");

const MAX_FRAME_BYTES = 8 * 1024 * 1024;
const failure = () => new Error("GraphQL Desk worker request failed");
const object = value => value !== null && typeof value === "object" && !Array.isArray(value);
const keys = (value, expected) => object(value) && Object.keys(value).sort().join(",") === [...expected].sort().join(",");
const string = value => typeof value === "string" && value.length > 0;

function parseFrame(buffer) {
  const text = new TextDecoder("utf-8", { fatal: true, ignoreBOM: true }).decode(buffer);
  const result = JSON.parse(text, (_key, value) => {
    if (typeof value === "number" && !Number.isFinite(value)) throw failure();
    return value;
  });
  // JSON.parse otherwise silently replaces duplicate object keys.
  const stack = [];
  for (const match of text.matchAll(/"(?:[^"\\]|\\.)*"|[{}\[\]]/g)) {
    const token = match[0];
    if (token === "{") stack.push(new Set());
    else if (token === "[") stack.push(null);
    else if (token === "}" || token === "]") stack.pop();
    else {
      let end = match.index + token.length;
      while (/\s/.test(text[end] ?? "") && end < text.length) end += 1;
      if (text[end] === ":") {
        const seen = stack.at(-1);
        const key = JSON.parse(token);
        if (!seen || seen.has(key)) throw failure();
        seen.add(key);
      }
    }
  }
  return result;
}

function validReply(reply, request) {
  if (!object(reply) || reply.ok !== true) return false;
  if (request.op === "prepare") {
    const extended = Object.hasOwn(request, "remote_revisions");
    const expected = ["ok", "id", "local_schemas", "remote_schemas", "required_buffers", "configuration_diagnostics"];
    if (extended) expected.push("project_preferences", "remote_revisions");
    const names = value => Array.isArray(value) && value.every(string) && new Set(value).size === value.length;
    const position = value => keys(value, ["line", "character"]) && Number.isSafeInteger(value.line) && value.line >= 0 &&
      Number.isSafeInteger(value.character) && value.character >= 0;
    const configurationDiagnostic = value => keys(value, ["uri", "version", "code", "message", "range"]) &&
      string(value.uri) && value.uri.startsWith("file:") && (value.version === null || (Number.isSafeInteger(value.version) && value.version >= 0)) &&
      value.code === "graphql-desk.configuration" && string(value.message) &&
      keys(value.range, ["start", "end"]) && position(value.range.start) && position(value.range.end) &&
      (value.range.start.line < value.range.end.line || (value.range.start.line === value.range.end.line && value.range.start.character <= value.range.end.character));
    return keys(reply, expected) && string(reply.id) && Array.isArray(reply.configuration_diagnostics) &&
      reply.configuration_diagnostics.every(configurationDiagnostic) &&
      (!extended || (Array.isArray(reply.project_preferences) && reply.project_preferences.every(item =>
        keys(item, ["project", "client_directives"]) && string(item.project) && names(item.client_directives)) &&
        Array.isArray(reply.remote_revisions) && reply.remote_revisions.every(item =>
          keys(item, ["project", "schema_name", "revision"]) && Object.values(item).every(string)))) &&
      Array.isArray(reply.remote_schemas) && reply.remote_schemas.every(item =>
        keys(item, extended ? ["project", "schema_name", "environment_names", "refresh_interval"] : ["project", "schema_name"]) &&
        string(item.project) && string(item.schema_name) && (!extended || (names(item.environment_names) &&
          typeof item.refresh_interval === "number" && Number.isFinite(item.refresh_interval) && item.refresh_interval >= 0))) &&
      names(reply.required_buffers) &&
      Array.isArray(reply.local_schemas) && reply.local_schemas.every(item =>
        keys(item, ["project", "schema_name", "path", "canonical_path"]) && Object.values(item).every(string));
  }
  if (request.op === "refresh") return keys(reply, ["ok", "id", "project", "schema_name", "status", "last_good", "error"]) &&
    string(reply.id) && reply.id !== request.id && reply.project === request.project && reply.schema_name === request.schema_name &&
    ["ready", "stale", "unavailable"].includes(reply.status) && reply.last_good === (reply.status === "stale") &&
    reply.error === (reply.status === "ready" ? null : "schema-refresh-failed");
  if (request.op === "quarantine") {
    if (!keys(reply, ["ok", "id", "projects"]) || reply.id !== request.id || !Array.isArray(reply.projects) ||
        reply.projects.length !== request.projects.length) return false;
    const byProject = new Map(request.projects.map(project => [project.project, project]));
    const seenProjects = new Set();
    return reply.projects.every(project => {
      if (!keys(project, ["project", "generated_package", "files"]) || !string(project.project) ||
          !Array.isArray(project.files) || !byProject.has(project.project) || seenProjects.has(project.project)) return false;
      seenProjects.add(project.project);
      const source = byProject.get(project.project);
      return project.generated_package === source.generated_package && project.files.length === source.files.length &&
        new Set(project.files.map(file => file?.relative_path)).size === project.files.length &&
        project.files.every(file => keys(file, ["relative_path", "text"]) &&
          source.files.some(original => original.relative_path === file.relative_path) &&
          typeof file.text === "string" && Buffer.byteLength(file.text, "utf8") <= 1024 * 1024);
    });
  }
  if (request.op === "current") return keys(reply, ["ok", "current"]) && typeof reply.current === "boolean";
  if (request.op === "artifacts") {
    if (keys(reply, ["ok", "id", "current"])) return string(reply.id) && reply.id === request.id && reply.current === false;
    const identifier = value => typeof value === "string" && /^[\p{ID_Start}_][\p{ID_Continue}_]*$/u.test(value);
    const span = value => keys(value, ["start", "end"]) && Number.isSafeInteger(value.start) && value.start >= 0 && Number.isSafeInteger(value.end) && value.end >= value.start;
    const module = value => keys(value, ["project", "module", "uri", "version"]) && [value.project, value.module, value.uri].every(string) && Number.isSafeInteger(value.version) && value.version >= 0;
    const provenanceIdentity = value => keys(value, ["module", "codepoint", "utf16"]) && module(value.module) && span(value.codepoint) && span(value.utf16);
    const schemaDependency = item => keys(item, ["coordinate", "fingerprint"]) && string(item.coordinate) && item.coordinate.length <= 2048 && /^[a-f0-9]{64}$/.test(item.fingerprint);
    const configDependency = item => keys(item, ["kind", "name", "value"]) &&
      string(item.name) && item.name.length <= 512 && ((["scalar-input", "scalar-output", "enum-mapping"].includes(item.kind) &&
        (item.value === null || typeof item.value === "string" && item.value.length <= 4096)) ||
        (["disable-masking", "client-directive"].includes(item.kind) && typeof item.value === "boolean"));
    const provenance = value => keys(value, ["owner", "selector", "operation_name", "operation_kind", "schema_identity", "context_token", "binding", "status", "declaration_path", "declaration_symbol", "source_exports", "dependencies", "direct_fragments", "definition_owners", "canonical_owner", "selector_known", "source_dependencies_known", "mapping_dependencies", "schema_dependencies", "config_dependencies"]) &&
      provenanceIdentity(value.owner) && (value.selector === null || string(value.selector)) && (value.operation_name === null || string(value.operation_name)) &&
      (value.operation_kind === null || ["query", "mutation", "subscription", "fragment"].includes(value.operation_kind)) &&
      (value.schema_identity === null || (Array.isArray(value.schema_identity) && value.schema_identity.length === 2 && value.schema_identity.every(string))) &&
      (value.context_token === null || string(value.context_token)) && (value.binding === null || string(value.binding)) &&
      ["precise", "unavailable", "rejected"].includes(value.status) &&
      (value.declaration_path === null || (string(value.declaration_path) && !value.declaration_path.includes("\\") && !path.posix.isAbsolute(value.declaration_path) && value.declaration_path.split("/").every(part => part && part !== "." && part !== ".."))) &&
      (value.declaration_symbol === null || identifier(value.declaration_symbol)) &&
      Array.isArray(value.source_exports) && value.source_exports.every(item => keys(item, ["relative_path", "symbol", "alias"]) && string(item.relative_path) && !item.relative_path.includes("\\") && !path.posix.isAbsolute(item.relative_path) && item.relative_path.split("/").every(part => part && part !== "." && part !== "..") && identifier(item.symbol) && identifier(item.alias)) &&
      Array.isArray(value.dependencies) && value.dependencies.every(module) && new Set(value.dependencies.map(item => `${item.project}\0${item.module}\0${item.uri}\0${item.version}`)).size === value.dependencies.length &&
      (value.direct_fragments === null || (Array.isArray(value.direct_fragments) && value.direct_fragments.every(provenanceIdentity))) &&
      (value.definition_owners === null || (Array.isArray(value.definition_owners) && value.definition_owners.every(provenanceIdentity))) &&
      (value.canonical_owner === null || provenanceIdentity(value.canonical_owner)) &&
      typeof value.selector_known === "boolean" && typeof value.source_dependencies_known === "boolean" &&
      (value.mapping_dependencies === "unknown" || Array.isArray(value.mapping_dependencies) && value.mapping_dependencies.length <= 16384 && value.mapping_dependencies.every(module)) &&
      (value.schema_dependencies === "unknown" || Array.isArray(value.schema_dependencies) && value.schema_dependencies.length <= 16384 && value.schema_dependencies.every(schemaDependency) &&
        new Set(value.schema_dependencies.map(item => item.coordinate)).size === value.schema_dependencies.length &&
        value.schema_dependencies.every((item, index) => index === 0 || value.schema_dependencies[index - 1].coordinate < item.coordinate)) &&
      (value.config_dependencies === "unknown" || Array.isArray(value.config_dependencies) && value.config_dependencies.length <= 4096 && value.config_dependencies.every(configDependency) &&
        new Set(value.config_dependencies.map(item => `${item.kind}\0${item.name}`)).size === value.config_dependencies.length &&
        value.config_dependencies.every((item, index) => {
          if (index === 0) return true;
          const previous = value.config_dependencies[index - 1];
          return previous.kind < item.kind || previous.kind === item.kind && previous.name < item.name;
        }));
    const validProvenance = project => {
      if (!Array.isArray(project.provenance) || !project.provenance.every(provenance)) return false;
      const prefix = `${project.generated_package.replaceAll(".", "/")}/`;
      const files = new Set(project.files.map(file => file.relative_path));
      const owners = new Set(project.provenance.map(value => JSON.stringify(value.owner)));
      return new Set(project.provenance.map(value => JSON.stringify(value.owner))).size === project.provenance.length &&
        project.provenance.every(value => {
          const references = [...(value.direct_fragments ?? []), ...(value.definition_owners ?? []), ...(value.canonical_owner ? [value.canonical_owner] : [])];
          return value.owner.module.project === project.project &&
            value.dependencies.every(item => item.project === project.project) &&
            (value.mapping_dependencies === "unknown" || value.mapping_dependencies.every(item => item.project === project.project)) &&
            references.every(item => item.module.project === project.project && owners.has(JSON.stringify(item))) &&
            (value.declaration_path === null || (value.declaration_path.startsWith(prefix) && value.declaration_path.endsWith(".pyi") && files.has(value.declaration_path))) &&
            value.source_exports.every(item => item.relative_path.startsWith(`${prefix}sources/`) && files.has(item.relative_path));
        });
    };
    const diagnostic = value => keys(value, ["module", "code", "message", "range"]) && module(value.module) && string(value.code) && string(value.message) &&
      keys(value.range, ["codepoint", "utf16"]) && span(value.range.codepoint) && span(value.range.utf16);
    const file = value => keys(value, ["relative_path", "text"]) && typeof value.text === "string" && string(value.relative_path) &&
      !value.relative_path.includes("\\") && !value.relative_path.includes("\0") && !path.posix.isAbsolute(value.relative_path) && !path.win32.isAbsolute(value.relative_path) &&
      /\.pyi?$/.test(value.relative_path) && value.relative_path.replace(/\.pyi?$/, "").split("/").every(identifier);
    const parserOverlay = value => Array.isArray(value) && value.length === 2 &&
      value[0]?.relative_path === "graphql_desk-stubs/__init__.pyi" && typeof value[0]?.text === "string" &&
      value[1]?.relative_path === "graphql_desk-stubs/py.typed" && value[1]?.text === "partial\n";
    return keys(reply, ["ok", "id", "input_fingerprint", "saved_source_fingerprint", "projects", "parser_overlay", "unavailable_schemas"]) && string(reply.id) && reply.id === request.id &&
      typeof reply.input_fingerprint === "string" && /^[a-f0-9]{64}$/.test(reply.input_fingerprint) &&
      typeof reply.saved_source_fingerprint === "string" && /^[a-f0-9]{64}$/.test(reply.saved_source_fingerprint) && Array.isArray(reply.projects) &&
      parserOverlay(reply.parser_overlay) &&
      Array.isArray(reply.unavailable_schemas) && reply.unavailable_schemas.every(value => Array.isArray(value) && value.length === 2 && value.every(string) && reply.projects.some(project => project?.project === value[0])) &&
      new Set(reply.unavailable_schemas.map(value => JSON.stringify(value))).size === reply.unavailable_schemas.length &&
      new Set(reply.projects.map(project => project?.project)).size === reply.projects.length && reply.projects.every(project =>
        keys(project, ["project", "generated_package", "files", "diagnostics", "provenance"]) && string(project.project) && string(project.generated_package) && project.generated_package.split(".").every(identifier) &&
        Array.isArray(project.files) && project.files.every(value => file(value) && value.relative_path.startsWith(project.generated_package.replaceAll(".", "/") + "/")) && new Set(project.files.map(file => file.relative_path.toLowerCase())).size === project.files.length &&
        Array.isArray(project.diagnostics) && project.diagnostics.every(value => diagnostic(value) && value.module.project === project.project) &&
        validProvenance(project));
  }
  return keys(reply, ["ok", "id", "request", "response", "unavailable_schemas", "remote_schemas"]) && reply.id === request.id &&
    Array.isArray(reply.remote_schemas) && reply.remote_schemas.every(item =>
      keys(item, ["project", "schema_name", "status"]) && string(item.project) && string(item.schema_name) &&
      ["ready", "stale", "unavailable"].includes(item.status)) &&
    keys(reply.request, ["request_id", "modules", "bindings", "schemas"]) &&
    keys(reply.response, ["request_id", "modules", "schemas", "calls", "diagnostics"]) &&
    string(reply.request.request_id) && reply.request.request_id === reply.response.request_id &&
    [reply.request.modules, reply.request.bindings, reply.request.schemas, reply.response.modules,
      reply.response.schemas, reply.response.calls, reply.response.diagnostics, reply.unavailable_schemas].every(Array.isArray);
}

/** One request at a time; concurrent calls reject immediately, without a queue.
 * The caller owns complete captures, latest-job publication guards and trust events.
 * invalidate() terminates busy work and permits a fresh process; dispose() is final.
 */
function createAnalysisWorker({ python, workspace, isTrusted, timeoutMs = 10000, spawn = spawnProcess }) {
  if (!string(python) || !path.isAbsolute(python) || !string(workspace) || !path.isAbsolute(workspace) ||
      typeof isTrusted !== "function" || !Number.isSafeInteger(timeoutMs) || timeoutMs <= 0 || timeoutMs > 2147483647) throw failure();
  const outside = candidate => {
    const relative = path.relative(path.resolve(workspace), candidate);
    return relative && (relative.startsWith(`..${path.sep}`) || relative === ".." || path.isAbsolute(relative));
  };
  const cwd = [os.tmpdir(), path.parse(path.resolve(workspace)).root].find(outside);
  if (!cwd) throw failure();
  let child = null;
  let pending = null;
  let epoch = 0;
  let disposed = false;
  const trusted = () => { try { return isTrusted() === true; } catch { return false; } };
  function stop() {
    const previous = child;
    child = null;
    epoch += 1;
    if (pending) {
      const active = pending;
      pending = null;
      active.cleanup();
      active.reject(failure());
    }
    if (previous) {
      const systemRoot = process.env.SystemRoot;
      const taskkill = process.platform === "win32" && Number.isInteger(previous.pid) &&
        typeof systemRoot === "string" && path.isAbsolute(systemRoot)
        ? path.join(systemRoot, "System32", "taskkill.exe") : null;
      if (taskkill) {
        try {
          const killer = spawn(taskkill, ["/PID", String(previous.pid), "/T", "/F"], {
            windowsHide: true, stdio: ["ignore", "pipe", "pipe"],
          });
          killer.stdout.on("data", () => {});
          killer.stderr.on("data", () => {});
          const closeStreams = () => {
            previous.stdin.destroy();
            previous.stdout.destroy();
            previous.stderr.destroy();
          };
          const fallback = () => {
            try { previous.kill(); } catch { /* State is already invalidated. */ }
            closeStreams();
          };
          killer.once("error", fallback);
          killer.once("exit", code => { if (code !== 0) fallback(); else closeStreams(); });
          return;
        } catch { try { previous.kill(); } catch { /* State is already invalidated. */ } }
      } else {
        try { previous.kill(); } catch { /* State is already invalidated. */ }
      }
      previous.stdin.destroy();
      previous.stdout.destroy();
      previous.stderr.destroy();
    }
  }
  function start() {
    const process = spawn(python, ["-I", "-X", "utf8", "-m", "graphql_desk.editor_worker"], {
      cwd, shell: false, windowsHide: true, stdio: ["pipe", "pipe", "pipe"],
    });
    child = process;
    const parts = [];
    let stderrTail = Buffer.alloc(0);
    let bytes = 0;
    const failed = () => { if (child === process) stop(); };
    process.on("error", failed);
    process.on("exit", failed);
    process.stdin.on("error", failed);
    process.stdout.on("error", failed);
    process.stderr.on("error", failed);
    process.stderr.on("data", chunk => {
      if (child !== process) return;
      if (globalThis.process.env.GRAPHQL_DESK_CAPTURE_WORKER_STDERR === "1" && Buffer.isBuffer(chunk)) {
        stderrTail = Buffer.concat([stderrTail, chunk]).subarray(-4096);
      }
      if (stderrTail.length) console.error("GraphQL Desk worker stderr:", stderrTail.toString("utf8"));
      failed();
    });
    process.stdout.on("data", chunk => {
      if (child !== process) return;
      try {
        if (!Buffer.isBuffer(chunk)) throw failure();
        let offset = 0;
        while (offset < chunk.length) {
          if (!pending) throw failure();
          const newline = chunk.indexOf(10, offset);
          const end = newline === -1 ? chunk.length : newline + 1;
          const piece = chunk.subarray(offset, end);
          bytes += piece.length;
          if (bytes > MAX_FRAME_BYTES) throw failure();
          parts.push(piece);
          offset = end;
          if (newline === -1) break;
          const active = pending;
          if (!active || !trusted()) throw failure();
          const reply = parseFrame(Buffer.concat(parts, bytes));
          if (!validReply(reply, active.request)) throw failure();
          parts.length = 0;
          bytes = 0;
          pending = null;
          active.cleanup();
          active.resolve(reply);
        }
      } catch { failed(); }
    });
    return process;
  }
  return {
    async request(message, { signal } = {}) {
      if (pending) throw failure();
      if (disposed || !trusted() || signal?.aborted) { stop(); throw failure(); }
      let encoded;
      try {
        if (!object(message) || !["prepare", "analyze", "current", "refresh", "artifacts", "quarantine"].includes(message.op) ||
            message.workspace !== workspace) throw failure();
        if (message.op === "quarantine") {
          const projects = message.projects;
          const operations = Array.isArray(projects) ? projects.map(project => ({
            project: project?.project, generated_package: project?.generated_package, operations: project?.provenance,
          })) : [];
          const validOwners = new Set(operations.flatMap(group => Array.isArray(group.operations) ? group.operations.map(item => JSON.stringify(item?.owner)) : []));
          if (!keys(message, ["op", "id", "workspace", "projects", "invalidated_owners"]) || !string(message.id) ||
              !Array.isArray(projects) || projects.length > 4096 || !validOperationManifest(operations) ||
              !Array.isArray(message.invalidated_owners) || message.invalidated_owners.length > 8192 ||
              !message.invalidated_owners.every(owner => object(owner) && validOwners.has(JSON.stringify(owner)))) throw failure();
          let total = 0;
          for (const project of projects) {
            if (!keys(project, ["project", "generated_package", "files", "provenance"]) || !Array.isArray(project.files) || project.files.length > 4096) throw failure();
            const prefix = `${project.generated_package.replaceAll(".", "/")}/`;
            for (const file of project.files) {
              if (!keys(file, ["relative_path", "text"]) || typeof file.relative_path !== "string" || !file.relative_path.startsWith(prefix) ||
                  !file.relative_path.endsWith(".pyi") || file.relative_path.includes("\\") || file.relative_path.includes("\0") ||
                  file.relative_path.split("/").some(part => !part || part === "." || part === "..") || typeof file.text !== "string") throw failure();
              total += Buffer.byteLength(file.text, "utf8");
              if (total > 4 * 1024 * 1024) throw failure();
            }
          }
        }
        encoded = Buffer.from(JSON.stringify(message) + "\n", "utf8");
        if (encoded.length > MAX_FRAME_BYTES) throw failure();
      } catch { stop(); throw failure(); }
      let process;
      try { process = child ?? start(); } catch { stop(); throw failure(); }
      const ticket = epoch;
      try {
        const result = await new Promise((resolve, reject) => {
          const abort = () => stop();
          const timer = setTimeout(() => {
            if (globalThis.process.env.GRAPHQL_DESK_CAPTURE_WORKER_STDERR === "1") {
              console.error(`GraphQL Desk worker timed out after ${timeoutMs} ms`);
            }
            abort();
          }, timeoutMs);
          const cleanup = () => { clearTimeout(timer); signal?.removeEventListener("abort", abort); };
          pending = { resolve, reject, cleanup, request: message };
          signal?.addEventListener("abort", abort, { once: true });
          if (signal?.aborted) { stop(); return; }
          process.stdin.write(encoded, error => { if (error && child === process) stop(); });
        });
        if (disposed || signal?.aborted || !trusted() || child !== process || epoch !== ticket) {
          stop(); throw failure();
        }
        return result;
      } catch { if (child === process) stop(); throw failure(); }
    },
    invalidate: stop,
    close() { disposed = true; stop(); },
    dispose() { disposed = true; stop(); },
  };
}

module.exports = { createAnalysisWorker, MAX_FRAME_BYTES };
