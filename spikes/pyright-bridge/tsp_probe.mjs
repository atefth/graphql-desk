import { spawn } from "node:child_process";
import { fileURLToPath, pathToFileURL } from "node:url";
import { dirname, resolve } from "node:path";
import process from "node:process";

const here = dirname(fileURLToPath(import.meta.url));
const root = resolve(here, "fixture");
const file = resolve(root, "tsp_live.py");
const uri = pathToFileURL(file).href;
const text = `from generated import GetUserResult\n\nresult: GetUserResult = {"user": {"id": "42", "name": "Atef", "nickname": None, "createdAt": "now"}}\nif result["user"] is not None:\n    value = result["user"]["nickname"]\n`;
const child = spawn(process.execPath, [resolve(here, "../../node_modules/pyright-typeserver/pyright-typeserver.js"), "--stdio"], { stdio: ["pipe", "pipe", "inherit"] });
let buffer = Buffer.alloc(0);
let nextId = 1;
const pending = new Map();

child.stdout.on("data", (chunk) => {
  buffer = Buffer.concat([buffer, chunk]);
  while (true) {
    const boundary = buffer.indexOf("\r\n\r\n");
    if (boundary < 0) return;
    const header = buffer.subarray(0, boundary).toString("ascii");
    const length = Number(header.match(/Content-Length: (\d+)/i)?.[1]);
    if (!Number.isFinite(length) || buffer.length < boundary + 4 + length) return;
    const message = JSON.parse(buffer.subarray(boundary + 4, boundary + 4 + length).toString("utf8"));
    buffer = buffer.subarray(boundary + 4 + length);
    if (message.id !== undefined && pending.has(message.id)) {
      const { resolve, reject } = pending.get(message.id);
      pending.delete(message.id);
      message.error ? reject(new Error(JSON.stringify(message.error))) : resolve(message.result);
    }
  }
});

function send(message) {
  const body = Buffer.from(JSON.stringify(message));
  child.stdin.write(`Content-Length: ${body.length}\r\n\r\n`);
  child.stdin.write(body);
}

function request(method, params) {
  const id = nextId++;
  return new Promise((resolve, reject) => {
    pending.set(id, { resolve, reject });
    send({ jsonrpc: "2.0", id, method, ...(params === undefined ? {} : { params }) });
  });
}

function notify(method, params) {
  send({ jsonrpc: "2.0", method, params });
}

function summarizeType(type) {
  if (type.kind === 4) return type.subTypes.map(summarizeType).sort().join(" | ");
  const name = type.name ?? type.declaration?.name ?? "unknown";
  return name === "NoneType" ? "None" : name;
}

async function computedType(snapshot, source) {
  const line = source.split("\n").findIndex((item) => item.includes("value ="));
  const expression = source.split("\n")[line].trim().slice("value = ".length);
  const start = source.split("\n")[line].indexOf(expression);
  const started = performance.now();
  const type = await request("typeServer/getComputedType", {
    arg: { uri, range: { start: { line, character: start }, end: { line, character: start + expression.length } } },
    snapshot,
  });
  return { type: type && summarizeType(type), elapsedMs: +(performance.now() - started).toFixed(2) };
}

try {
  const rootUri = pathToFileURL(root).href;
  await request("initialize", {
    processId: process.pid,
    rootUri,
    workspaceFolders: [{ uri: rootUri, name: "pyright-bridge-fixture" }],
    capabilities: {},
  });
  notify("initialized", {});
  const protocolVersion = await request("typeServer/getSupportedProtocolVersion");
  const beforeOpen = await request("typeServer/getSnapshot");
  notify("textDocument/didOpen", { textDocument: { uri, languageId: "python", version: 1, text } });
  const openedSnapshot = await request("typeServer/getSnapshot");
  const nullable = await computedType(openedSnapshot, text);
  const edited = text.replace('result["user"]["nickname"]', 'result["user"]["name"]');
  notify("textDocument/didChange", { textDocument: { uri, version: 2 }, contentChanges: [{ text: edited }] });
  const editedSnapshot = await request("typeServer/getSnapshot");
  const nonNullable = await computedType(editedSnapshot, edited);
  let staleSnapshotError;
  try { await computedType(openedSnapshot, edited); } catch (error) { staleSnapshotError = error.message; }
  console.log(JSON.stringify({ protocolVersion, beforeOpen, openedSnapshot, editedSnapshot, nullable, nonNullable, staleSnapshotError }, null, 2));
  if (editedSnapshot <= openedSnapshot || nullable.type !== "None | str" || nonNullable.type !== "str" || !staleSnapshotError?.includes("-32802")) process.exitCode = 1;
} finally {
  try { await request("shutdown"); } catch {}
  notify("exit", {});
  child.kill();
}
