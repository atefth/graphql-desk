"use strict";

const path = require("node:path");
const { PREPARE_REQUEST, PLAN_REQUEST, SYNC_REQUEST, EXTRACT_REQUEST, EXTRACT_PLAN_REQUEST,
  createRenameBridge } = require("./renameBridge");

async function stopLanguageClient(client, outputChannel) {
  const server = client.serverProcess;
  const exited = () => server.exitCode != null || server.signalCode != null;
  const alreadyClosed = server && exited() && [server.stdout, server.stderr].every(stream => !stream || stream.closed === true);
  const closed = server && !alreadyClosed ? new Promise(resolve => server.once("close", resolve)) : null;
  let channelDisposed = false;
  const disposeChannel = () => {
    if (!channelDisposed) { channelDisposed = true; outputChannel.dispose(); }
  };
  let cleanupPending = false;
  try {
    try { await client.dispose(); }
    catch {
      if (server && !exited()) {
        let killed = false;
        try { killed = server.kill(); } catch { /* Report failure without dropping the still-live child. */ }
        if (!killed && !exited()) {
          cleanupPending = true;
          void closed.then(disposeChannel).catch(() => {});
          const message = "GraphQL fragment edit process could not be stopped; cleanup is pending";
          try { outputChannel.error(message); } catch { /* Preserve the shutdown failure if logging also fails. */ }
          throw new Error(message);
        }
      }
    }
    // The library can log child exit after its protocol shutdown has resolved.
    if (closed) await closed;
  } finally {
    if (!cleanupPending) disposeChannel();
  }
}

function createRenameLanguageClient(vscode, language, { documentSelector = [{ language: "python", scheme: "file" }],
  middleware, ...bridgeOptions } = {}) {
  const { LanguageClient, TransportKind } = require("vscode-languageclient/node");
  let started = false;
  let stopped = false;
  let stopPromise;
  let trustGranted;
  const trusted = () => {
    try { return !stopped && vscode.workspace.isTrusted === true &&
      (bridgeOptions.isTrusted === undefined || bridgeOptions.isTrusted() === true); }
    catch { return false; }
  };
  const allowed = () => {
    if (trusted()) return true;
    if (started) void stop().catch(() => {});
    return false;
  };
  class TrustedLanguageClient extends LanguageClient {
    validateWorkspaceEdit(edit) { return allowed() && super.validateWorkspaceEdit(edit); }
    async start() {
      if (!allowed()) throw new Error("A trusted workspace is required for GraphQL fragment edits");
      started = true;
      await super.start();
      if (!allowed()) throw new Error("GraphQL fragment edit trust changed during startup");
    }
  }
  function trustedModule() {
    // v10.1.2 re-reads module immediately before fork, after cwd lookup; recheck on upgrades.
    if (!allowed()) throw new Error("A trusted workspace is required for GraphQL fragment edits");
    return path.join(__dirname, "renameServer.js");
  }
  const server = {
    run: { get module() { return trustedModule(); }, transport: TransportKind.ipc },
    debug: { get module() { return trustedModule(); }, transport: TransportKind.ipc,
      options: { execArgv: ["--nolazy", "--inspect=6009"] } },
  };
  const bridge = createRenameBridge(vscode, language, { ...bridgeOptions, isTrusted: allowed });
  const outputChannel = vscode.window.createOutputChannel("GraphQL Desk Rename", { log: true });
  const client = new TrustedLanguageClient("graphqlDeskFragmentRename", "GraphQL Desk Fragment Rename", server, {
    documentSelector,
    middleware: { ...middleware,
      sendRequest(type, params, token, next) {
        if (!allowed()) return Promise.reject(new Error("A trusted workspace is required for GraphQL fragment edits"));
        const send = (...args) => {
          if (!allowed()) return Promise.reject(new Error("A trusted workspace is required for GraphQL fragment edits"));
          return next(...args);
        };
        return middleware?.sendRequest ? middleware.sendRequest(type, params, token, send) : send(type, params, token);
      },
      workspace: { ...middleware?.workspace,
        handleApplyEdit(params, next) {
          if (!allowed()) return { applied: false };
          const apply = value => allowed() ? next(value) : { applied: false };
          return middleware?.workspace?.handleApplyEdit ? middleware.workspace.handleApplyEdit(params, apply) : apply(params);
        },
      },
    },
    synchronize: {},
    outputChannel,
  });
  client.onRequest(PREPARE_REQUEST, async (params, token) => {
    const result = await bridge.prepare(params, token);
    return allowed() && !token?.isCancellationRequested ? result : null;
  });
  async function synchronizedPlan(plan, token) {
    if (!allowed() || !plan || token?.isCancellationRequested) return null;
    try {
      const synchronized = await client.sendRequest(SYNC_REQUEST, plan.targets, token);
      return allowed() && synchronized === true && !token?.isCancellationRequested ? plan : null;
    } catch { return null; }
  }
  client.onRequest(PLAN_REQUEST, async (params, token) => synchronizedPlan(await bridge.plan(params, token), token));
  client.onRequest(EXTRACT_PLAN_REQUEST, async (params, token) => {
    return synchronizedPlan(await bridge.extract(params, token), token);
  });
  function start() { return allowed() ? client.start() : Promise.resolve(false); }
  function stop() {
    stopped = true;
    trustGranted?.dispose();
    if (!stopPromise) stopPromise = stopLanguageClient(client, outputChannel);
    return stopPromise;
  }
  // The public trust event reports grants; disposal and request guards handle loss.
  trustGranted = vscode.workspace.onDidGrantWorkspaceTrust(() => start().catch(() => {
    if (!stopped) outputChannel.appendLine("GraphQL fragment edit language server could not start");
  }));
  return {
    client,
    start,
    async extract(params) {
      if (!allowed()) return false;
      await client.start();
      if (!allowed()) return false;
      return client.sendRequest(EXTRACT_REQUEST, params);
    },
    stop,
  };
}

module.exports = { createRenameLanguageClient, stopLanguageClient };
