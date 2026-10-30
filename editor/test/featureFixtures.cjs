"use strict";

const assert = require("node:assert/strict");
const path = require("node:path");
const { spawnSync } = require("node:child_process");

const root = path.resolve(__dirname, "../..");
const python = process.env.GRAPHQL_DESK_PYTHON ?? path.join(root, ".venv", process.platform === "win32" ? "Scripts/python.exe" : "bin/python");
const schema = `
"Available colors" enum Color { RED BLUE }
"User filter" input Filter { "Filter color" color: Color = RED }
"Tag a query" directive @tag("Tag argument" value: String) on QUERY
"A user" type User { id: ID! "Display name" name: String! old: String @deprecated(reason:"Use name") }
type Query { "Current user" user(id: ID = "1", color: Color, filter: Filter): User }
`;

function bundle(source, extra = {}) {
  const request = {
    request_id: "features-1",
    modules: [{ project: "p", module: "main", uri: extra.uri ?? "file:///workspace/main.py", version: extra.version ?? 1, source, is_package: false }, ...(extra.modules ?? [])],
    bindings: [{ project: "p", schemas: ["default"], default_schema: "default", factories: [], type_mappings: [] }],
    schemas: [{ project: "p", schema_name: "default", uri: extra.schemaUri ?? "file:///workspace/schema.graphql", version: extra.schemaVersion ?? 2,
      token: extra.schemaToken ?? "schema-2", format: extra.schemaFormat ?? "sdl", text: extra.schema ?? schema }],
  };
  const result = spawnSync(python, ["-I", "-X", "utf8", "-c",
    "import sys; sys.path.insert(0, sys.argv[1]); from graphql_desk.editor_analysis import analyze_editor_json; sys.stdout.write(analyze_editor_json(sys.stdin.read()))",
    path.join(root, "src")],
    { input: JSON.stringify(request), encoding: "utf8", cwd: root });
  assert.equal(result.status, 0, result.stderr);
  return { request, response: JSON.parse(result.stdout) };
}

function document(value) {
  const item = value.request.modules[0];
  return { uri: item.uri, version: item.version, text: item.source };
}

function options(value, extra = {}) {
  return { analyze: async () => value, isCurrent: async () => true, isTrusted: () => true, ...extra };
}

module.exports = { bundle, document, options, schema };
