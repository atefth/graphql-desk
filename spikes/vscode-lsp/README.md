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

The check exercises the service's CLI against an invalid operation and a completion prefix using the local SDL schema. `.graphqlrc.yml` records the schema/document association expected by the LSP server; the CLI check passes the schema path directly, so server-side config discovery is not exercised.

The service speaks LSP and documents GraphQL diagnostics, completion, definitions, symbols, and outline; this fixture does not ship a GraphQL Desk VS Code client.

## Results

- Diagnostics: supported by the service; the check confirms an unknown field is surfaced at line 0, characters 24–37. This verifies the service range, not the exact VS Code underline rendering.
- Completion: supported; the check confirms schema field completion for the prefix `na`.
- Hover: not listed as a supported feature in the service's current official README; not available as a demonstrated capability here.
- Navigation: service documentation lists links to fragment and named type definitions. The fixture contains no fragment or separate SDL declaration, so navigation remains unverified.
- Schema discovery: the local SDL works through the CLI's explicit schema-path option. A GraphQL config file is required by the server, but this spike does not exercise server-side config discovery.
- Refresh: the server relies on an LSP client file watcher and document change notifications. This fixture does not test VS Code refresh behavior.
- Generated Python files: not needed for GraphQL diagnostics or completion; Python call-site typing is outside this spike.

## Acceptance evidence

`npm run check` asserts that an invalid field appears in diagnostics and that the schema field `name` is suggested by completion. This is service-level evidence, not an end-to-end VS Code UI test.

## Decision recommendation

Proceed with the existing GraphQL Language Service as the first GraphQL analysis backend for the LSP prototype. Keep the first slice to schema configuration, diagnostics, completion, definition navigation, and document symbols. Do not promise hover parity: it is not among the service's documented features, and a custom hover implementation would need a separate scoped spike. Add a thin VS Code client only after the backend choice is accepted.

## Out of scope

- Shipping a GraphQL Desk VS Code extension or manually testing the VS Code UI.
- Python call-site typing, fragment masking, and generator integration.

## Limitations and risks

- No VS Code extension UI was built or manually exercised. A TypeScript language client is still needed for a shippable extension.
- The current server README describes partial LSP support and requires GraphQL config; it also documents limitations around multi-root workspaces.
- Hover, precise diagnostic underlines in VS Code, definition jumps, and live schema refresh remain unverified.
- This does not demonstrate live Python type inference, fragment masking, or Ariadne/Turms integration.
- `npm install` reports 13 high-severity transitive dependency advisories in this exploratory CLI dependency tree; review/update the selected service version before shipping it as a product dependency.

## Dependencies

Independent of the Pyright and codegen spikes. The final parity checklist in issue #1 may change which features become acceptance requirements.
