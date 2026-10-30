"use strict";

const assert = require("node:assert/strict");
const { EventEmitter } = require("node:events");
const test = require("node:test");
const { stopLanguageClient } = require("../src/renameLanguageClient");

test("failed startup shuts down its child and disposes the output channel", async () => {
  const server = new EventEmitter();
  server.exitCode = null;
  server.signalCode = null;
  server.kill = () => {
    queueMicrotask(() => {
      server.exitCode = 1;
      server.emit("exit", 1, null);
    });
    return true;
  };
  let outputDisposed = false;
  let currentServer = server;

  await stopLanguageClient({
    get serverProcess() { return currentServer; },
    async dispose() {
      currentServer = undefined;
      throw new Error("Client is not running");
    },
  }, { dispose() { outputDisposed = true; } });

  assert.equal(server.exitCode, 1);
  assert.equal(outputDisposed, true);
});

test("successful shutdown still disposes the owned output channel", async () => {
  let clientDisposed = false;
  let outputDisposed = false;

  await stopLanguageClient({ async dispose() { clientDisposed = true; } },
    { dispose() { outputDisposed = true; } });

  assert.equal(clientDisposed, true);
  assert.equal(outputDisposed, true);
});
