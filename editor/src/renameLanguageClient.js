"use strict";

const path = require("node:path");
const { LanguageClient, TransportKind } = require("vscode-languageclient/node");
const { PREPARE_REQUEST, PLAN_REQUEST, SYNC_REQUEST, createRenameBridge } = require("./renameBridge");

function createRenameLanguageClient(vscode, language, { documentSelector = [{ language: "python", scheme: "file" }],
  middleware, ...bridgeOptions } = {}) {
  const server = {
    run: { module: path.join(__dirname, "renameServer.js"), transport: TransportKind.ipc },
    debug: { module: path.join(__dirname, "renameServer.js"), transport: TransportKind.ipc,
      options: { execArgv: ["--nolazy", "--inspect=6009"] } },
  };
  const bridge = createRenameBridge(vscode, language, bridgeOptions);
  const client = new LanguageClient("graphqlDeskFragmentRename", "GraphQL Desk Fragment Rename", server, {
    documentSelector,
    middleware,
    synchronize: {},
    outputChannel: vscode.window.createOutputChannel("GraphQL Desk Rename", { log: true }),
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
      if (!stopPromise) stopPromise = client.dispose();
      await stopPromise;
    },
  };
}

module.exports = { createRenameLanguageClient };
