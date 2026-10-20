"use strict";

const { spawn: spawnProcess } = require("node:child_process");
const os = require("node:os");
const path = require("node:path");
const { TextDecoder } = require("node:util");

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
    return keys(reply, ["ok", "id", "local_schemas", "remote_schemas", "required_buffers"]) && string(reply.id) &&
      Array.isArray(reply.remote_schemas) && reply.remote_schemas.every(item =>
        keys(item, ["project", "schema_name"]) && Object.values(item).every(string)) &&
      Array.isArray(reply.required_buffers) && reply.required_buffers.every(string) &&
      Array.isArray(reply.local_schemas) && reply.local_schemas.every(item =>
        keys(item, ["project", "schema_name", "path", "canonical_path"]) && Object.values(item).every(string));
  }
  if (request.op === "refresh") return keys(reply, ["ok", "id", "project", "schema_name", "status", "last_good", "error"]) &&
    string(reply.id) && reply.id !== request.id && reply.project === request.project && reply.schema_name === request.schema_name &&
    ["ready", "stale", "unavailable"].includes(reply.status) && reply.last_good === (reply.status === "stale") &&
    reply.error === (reply.status === "ready" ? null : "schema-refresh-failed");
  if (request.op === "current") return keys(reply, ["ok", "current"]) && typeof reply.current === "boolean";
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
      try { previous.kill(); } catch { /* State is already invalidated. */ }
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
    let bytes = 0;
    const failed = () => { if (child === process) stop(); };
    process.on("error", failed);
    process.on("exit", failed);
    process.stdin.on("error", failed);
    process.stdout.on("error", failed);
    process.stderr.on("error", failed);
    process.stderr.on("data", failed);
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
        if (!object(message) || !["prepare", "analyze", "current", "refresh"].includes(message.op) ||
            message.workspace !== workspace) throw failure();
        encoded = Buffer.from(JSON.stringify(message) + "\n", "utf8");
        if (encoded.length > MAX_FRAME_BYTES) throw failure();
      } catch { stop(); throw failure(); }
      let process;
      try { process = child ?? start(); } catch { stop(); throw failure(); }
      const ticket = epoch;
      try {
        const result = await new Promise((resolve, reject) => {
          const abort = () => stop();
          const timer = setTimeout(abort, timeoutMs);
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
