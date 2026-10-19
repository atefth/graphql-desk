# Pyright live-operation type spike

## User story / goal

As a Python GraphQL developer, I want a query edit to update useful Python type feedback in the editor without stale results.

## Spike question

Can Pyright's Type Server Protocol (TSP) provide a live bridge from GraphQL operations to Python types, beyond the generated-stub workflow?

## Method

- Use a schema and operation with a required `ID!` variable, nullable query result and field, and custom `DateTime` scalar.
- Compare a checked-in generated `TypedDict` baseline with a direct TSP client.
- Ask TSP for a Python expression type before and after a document update; check snapshot refresh and stale-snapshot rejection.
- Measure each TSP request round trip, excluding server startup.

## Acceptance criteria and results

- [x] Generated Python types distinguish valid from invalid field access, required variables, wrong variable types, and nullability.
- [x] TSP reports a nullable type before an in-memory Python document change and a non-null type after it.
- [x] The server advances its snapshot after the document change and rejects an old-snapshot query as stale.
- [ ] A GraphQL operation edit itself produces or regenerates the matching Python types. This was not implemented: the spike has no GraphQL parser or generator.
- [ ] VS Code/Pylance diagnostics visibly update end to end. No VS Code UI test was run.

## Reproduction

Requires Node.js and npm. From the repository root:

```sh
npm install
npm run verify:pyright-tsp
npx pyright --project spikes/pyright-bridge/fixture/pyrightconfig.json spikes/pyright-bridge/fixture/valid.py
npx pyright --project spikes/pyright-bridge/fixture/pyrightconfig.json spikes/pyright-bridge/fixture/invalid.py
```

The invalid fixture should exit nonzero and report four errors. The valid fixture should report zero errors.

## Environment and observations

- Node.js `v22.17.1`; npm `11.6.0`.
- Pyright CLI and `pyright-typeserver` npm package `1.1.414`.
- Negotiated TSP version `0.4.1`.
- Both fixture Python checks behaved as expected: valid code had zero errors; invalid code reported missing required `id`, `int` where `str` is required, unknown `TypedDict` key, and nullable access without a guard.
- TSP returned `str | None` for `nickname` and `str` for `name`. Request round trips were 10.32 ms and 5.53 ms in this single local run; startup is excluded, and this is not a latency benchmark.
- Snapshot advanced from `5` to `7` after `textDocument/didChange`. Querying with the prior snapshot returned JSON-RPC error `-32802` (`ServerCancelled`), so the client must retry using a fresh snapshot.
- Pyright emitted a Windows “Python was not found” environment-discovery warning because this runner has no Python interpreter installed; type analysis still completed. The Node-based TSP smoke test ran successfully.

## Recommendation

**No-go for treating TSP as the feature that makes GraphQL queries infer Python types. Go for generated Python stubs/models as the source of static typing, with the normal Pyright/Pylance analyzer consuming those generated types.** TSP is a viable, real API for querying Pyright's view of Python expressions and may help a GraphQL editor align hover or completion with Python analysis, but it does not parse GraphQL, map a query to a Python call site, or generate query-derived types. That missing GraphQL-to-Python mapping is the actual bridge that needs separate design and prototype evidence.

This spike manually edited Python text to prove TSP invalidation; it did not claim that a GraphQL edit causes that Python edit. The 0.x protocol version also needs version negotiation and compatibility testing against each supported server release. The docs explicitly warn that minor version changes in 0.x can be breaking.

## Fragment masking

The TSP result does not decide fragment semantics. The masking design spike can proceed independently as a Python typing/API experiment using the generated-type baseline; it should not assume TSP supplies masking.

## Out of scope

- Production LSP, GraphQL parsing/validation, a VS Code extension, generated-code watching, and full GraphQL-to-Python type synchronization.
- A performance benchmark or compatibility matrix across Pyright versions and editors.

## Dependencies

- Related to issue #2 for GraphQL document diagnostics; this checker experiment remains separable.
- Source: [Pyright Type Server](https://github.com/microsoft/pyright/blob/main/docs/type-server.md), [Type Server Protocol](https://github.com/microsoft/pylance-release/blob/main/docs/tsp/type-server-protocol.md), [Pyright type stubs](https://github.com/microsoft/pyright/blob/main/docs/type-stubs.md).
