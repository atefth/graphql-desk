# VS Code GraphQL language-service spike

## Spike question

Can an existing GraphQL language service provide useful schema-aware editing for one local schema and `.graphql` operation in VS Code?

## User story / goal

As a Python GraphQL developer, I want schema errors and schema-aware completion in my editor while editing a standalone GraphQL document, without requiring generated Python types.

## Context

This exploratory fixture uses GraphiQL's [`graphql-language-service-cli`](https://github.com/graphql/graphiql/tree/main/packages/graphql-language-service-cli) and its server package. It is intentionally separate from product code. The selected dependency is TypeScript/Node based: VS Code's [language-server guide](https://code.visualstudio.com/api/language-extensions/language-server-extension-guide) uses a TypeScript language client, and the service package already speaks LSP, so a Python server would add a bridge without helping this spike answer its question.

## Method and setup

Requirements: Node.js 20.9+ (the current server docs specify `^18.18.0 || >=20.9.0`) and npm.

From this directory:

```sh
npm install
npm run check
```

The check launches the actual language server over Content-Length framed stdio, initializes it as an LSP client, opens and changes the `.graphql` document, and requests completion and hover. It uses `.graphqlrc.yml` for schema discovery. This exercises the LSP server protocol without shipping a VS Code extension client.

The service documents diagnostics, completion, definitions, symbols, and outline; its server also handles LSP hover requests. This fixture does not ship a GraphQL Desk VS Code client.

## Results

- Diagnostics: the LSP publishes an unknown-field diagnostic starting at the field name; its end position extends one character past the identifier in this fixture. This verifies protocol ranges, not the exact VS Code underline rendering.
- Completion: the LSP completion request suggests the schema field `name` after a document change to the prefix `na`.
- Hover: the LSP hover request on `name` returns its schema type, `String!`; hover is implemented by the server even though it is omitted from its README's feature list.
- Navigation: an LSP definition request on the `name` field returns its location in the configured `schema.graphql`.
- Schema discovery: the server loads the local SDL schema through `.graphqlrc.yml` when started with the fixture directory as `configDir`.
- Refresh: `textDocument/didChange` triggers updated diagnostics and completion reflects the changed document. External schema-file watcher refresh remains unverified.
- Generated Python files: not needed for GraphQL diagnostics or completion; Python call-site typing is outside this spike.

## Acceptance evidence

`npm run check` asserts the LSP diagnostic range, completion suggestion, hover type, and definition target after initialize, didOpen, didChange, completion, hover, and definition JSON-RPC messages over stdio. This is protocol-level evidence, not an end-to-end VS Code UI test.

## Decision recommendation

Proceed with the existing GraphQL Language Service as the first GraphQL analysis backend for a VS Code client prototype. The protocol smoke test confirms config discovery, diagnostics, completion, hover, and a definition jump without generated Python files. Manually verify underline rendering, schema-file watcher refresh, and editor integration before making an extension-level claim.

## Out of scope

- Shipping a GraphQL Desk VS Code extension or manually testing the VS Code UI.
- Python call-site typing, fragment masking, and generator integration.

## Limitations and risks

- No VS Code extension UI was built or manually exercised. A TypeScript language client is still needed for a shippable extension; this test speaks the standard LSP stdio transport directly.
- The current server README describes partial LSP support and requires GraphQL config; it also documents limitations around multi-root workspaces.
- Precise diagnostic underlines in VS Code and live schema-file refresh remain unverified.
- This does not demonstrate live Python type inference, fragment masking, or Ariadne/Turms integration.
- This package and lockfile are quarantined under `spikes/` as a development-only feasibility fixture, not a production dependency. `npm audit` reports 13 high-severity vulnerable package entries: `@graphql-tools/code-file-loader`, `@graphql-tools/graphql-file-loader`, `@graphql-tools/graphql-tag-pluck`, `@graphql-tools/json-file-loader`, `@graphql-tools/utils`, `braces`, `fast-glob`, `globby`, `graphql-config`, `graphql-language-service-cli`, `graphql-language-service-server`, `micromatch`, and `source-map-js`. They trace to three advisories: [GHSA-7mx3-vvmw-hjmv](https://github.com/advisories/GHSA-7mx3-vvmw-hjmv) affects `@graphql-tools/utils` versions through 12.0.0; [GHSA-vfj7-8cjw-p6xm](https://github.com/advisories/GHSA-vfj7-8cjw-p6xm) affects `braces` through `micromatch`, `fast-glob`, and `globby`; [GHSA-68fv-2mgg-jv7q](https://github.com/advisories/GHSA-68fv-2mgg-jv7q) affects `source-map-js` versions 1.0.0–1.2.1. `npm audit` suggests a forced downgrade to `graphql-language-service-cli@3.3.32` as a breaking change; `npm audit fix --dry-run --force` still reports the same 13 entries, so no verified clean compatible update is available. The smaller `graphql-language-service-server` package is also in the reported vulnerable dependency path. `npm audit --omit=dev` reports zero vulnerabilities because the CLI is a dev dependency in this isolated fixture. The lockfile stays to preserve the exact tested tree. Do not promote this dependency to product runtime without a separately reviewed upgrade or replacement.

## Dependencies

Independent of the Pyright and codegen spikes. The final parity checklist in issue #1 may change which features become acceptance requirements.
