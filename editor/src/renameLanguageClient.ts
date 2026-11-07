import path = require("node:path");
import type { Disposable, LogOutputChannel } from "vscode";
import type { LanguageClient, LanguageClientOptions, Middleware, ServerOptions } from "vscode-languageclient/node";
import type { CancellationToken, WorkspaceEdit } from "vscode-languageserver/node";
import {
  PREPARE_REQUEST, PLAN_REQUEST, SYNC_REQUEST, EXTRACT_REQUEST, EXTRACT_PLAN_REQUEST,
  createRenameBridge,
  type ExtractParams,
  type RenameBridgeOptions,
  type RenameLanguage,
  type RenamePlan,
} from "./renameBridge";

type RenameClientOptions = RenameBridgeOptions & {
  documentSelector?: LanguageClientOptions["documentSelector"];
  middleware?: Middleware;
};

export async function stopLanguageClient(
  client: Pick<LanguageClient, "serverProcess" | "dispose">,
  outputChannel: Pick<LogOutputChannel, "dispose" | "error">,
) {
  const server = client.serverProcess;
  const exited = () => server?.exitCode != null || server?.signalCode != null;
  const alreadyClosed = server && exited() && [server.stdout, server.stderr].every(stream => !stream || stream.closed === true);
  const closed = server && !alreadyClosed ? new Promise<void>(resolve => server.once("close", () => resolve())) : null;
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
          void closed?.then(disposeChannel).catch(() => {});
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

export function createRenameLanguageClient(
  vscode: Pick<typeof import("vscode"), "workspace" | "window" | "Uri" | "Position">,
  language: RenameLanguage,
  { documentSelector = [{ language: "python", scheme: "file" }],
    middleware, ...bridgeOptions }: RenameClientOptions = {},
) {
  // Load the VS Code client only inside the host-facing factory.
  const { LanguageClient, TransportKind }: typeof import("vscode-languageclient/node") = require("vscode-languageclient/node");
  let started = false;
  let stopped = false;
  let stopPromise: Promise<void> | undefined;
  let trustGranted: Disposable | undefined;
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
    override validateWorkspaceEdit(edit: WorkspaceEdit) { return allowed() && super.validateWorkspaceEdit(edit); }
    override async start() {
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
  const server: ServerOptions = {
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
        const send: typeof next = (...args) => {
          if (!allowed()) return Promise.reject(new Error("A trusted workspace is required for GraphQL fragment edits"));
          return next(...args);
        };
        return middleware?.sendRequest ? middleware.sendRequest(type, params, token, send) : send(type, params, token);
      },
      workspace: { ...middleware?.workspace,
        handleApplyEdit(params, next) {
          if (!allowed()) return { applied: false };
          // v10.1.2 supplies a unary continuation despite its request-handler type.
          const applyEdit = next as (value: typeof params) => ReturnType<typeof next>;
          const apply = (value: typeof params) => allowed() ? applyEdit(value) : { applied: false };
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
  async function synchronizedPlan(plan: RenamePlan | null, token?: CancellationToken) {
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
    async extract(params: ExtractParams) {
      if (!allowed()) return false;
      await client.start();
      if (!allowed()) return false;
      return client.sendRequest(EXTRACT_REQUEST, params);
    },
    stop,
  };
}
