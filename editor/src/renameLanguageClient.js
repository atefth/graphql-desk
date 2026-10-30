"use strict";

const path = require("node:path");
const { PREPARE_REQUEST, PLAN_REQUEST, SYNC_REQUEST, createRenameBridge } = require("./renameBridge");

async function stopLanguageClient(client, outputChannel) {
  const server = client.serverProcess;
  try {
    await client.dispose();
  } catch {
    if (server && server.exitCode === null && server.signalCode === null) {
      await new Promise(resolve => {
        server.once("exit", resolve);
        try { if (!server.kill()) resolve(); } catch { resolve(); }
      });
    }
  } finally {
    outputChannel.dispose();
  }
}

function createRenameLanguageClient(vscode, language, { documentSelector = [{ language: "python", scheme: "file" }],
  middleware, ...bridgeOptions } = {}) {
  const { LanguageClient, TransportKind } = require("vscode-languageclient/node");
  const server = {
    run: { module: path.join(__dirname, "renameServer.js"), transport: TransportKind.ipc },
    debug: { module: path.join(__dirname, "renameServer.js"), transport: TransportKind.ipc,
      options: { execArgv: ["--nolazy", "--inspect=6009"] } },
  };
  const bridge = createRenameBridge(vscode, language, bridgeOptions);
  const outputChannel = vscode.window.createOutputChannel("GraphQL Desk Rename", { log: true });
  const client = new LanguageClient("graphqlDeskFragmentRename", "GraphQL Desk Fragment Rename", server, {
    documentSelector,
    middleware,
    synchronize: {},
    outputChannel,
  });
  client.onRequest(PREPARE_REQUEST, (params, token) => bridge.prepare(params, token));
  client.onRequest(PLAN_REQUEST, async (params, token) => {
    const plan = await bridge.plan(params, token);
    if (!plan || token?.isCancellationRequested) return null;
    try {
      const synchronized = await client.sendRequest(SYNC_REQUEST, plan.targets, token);
      return synchronized === true && !token?.isCancellationRequested ? plan : null;
    } catch { return null; }
  });
  let startPromise;
  let stopPromise;
  return {
    client,
    start() {
      if (!startPromise) startPromise = client.start();
      return startPromise;
    },
    async stop() {
      if (!stopPromise) stopPromise = stopLanguageClient(client, outputChannel);
      await stopPromise;
    },
  };
}

module.exports = { createRenameLanguageClient, stopLanguageClient };
