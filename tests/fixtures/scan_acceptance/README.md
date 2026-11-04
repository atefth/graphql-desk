# Scan acceptance fixture

This fixture is a stable Python project used to exercise all eight scan rules,
the project/import/fragment graph, multiple schemas, warnings, and each report
format. Python source inputs are stored as `.py.txt` and materialized in a temporary
project by the test, so intentionally malformed inputs and generated imports are
data rather than files checked as repository code.

`expected-report.json`, `expected-graph.json`, and
`expected-terminal.txt` are GraphQL Desk contract snapshots. They are not
claimed to be byte-for-byte gql.tada output: Python adds schema identity,
project-relative paths, coverage, and discovery warnings as specified in
`docs/contracts/persisted-and-project-analysis.md`.

## Pinned upstream reference

Reference source: gql.tada commit
`cfe64f433bd29ff6f79e462596389b81c6e01aec`.

The pinned tests confirm these source-level expectations used by the matching
assertions in `tests/test_scan_acceptance.py`:

| Acceptance behavior | Pinned reference |
| --- | --- |
| Field selections count once per definition; fragment fields reach operations; unused schema fields are absent; parse failures warn | `packages/cli-utils/src/commands/scan/__tests__/analyze.test.ts` |
| Input fields and enum values, directives, deprecated usage, orphan fragments, and complexity footprint | `packages/cli-utils/src/commands/scan/__tests__/rules.test.ts` |
| Import distance, reverse dependents, cross-feature fragments, and field reach | `packages/cli-utils/src/commands/scan/__tests__/dependency.test.ts` |
| JSON corpus shape and operation variables/locations | `packages/cli-utils/src/commands/scan/__tests__/json.test.ts` |
| Graph topology and inclusion of non-document import modules | `packages/cli-utils/src/commands/scan/__tests__/graph.test.ts` |
| Terminal width and unbounded output | `packages/cli-utils/src/commands/scan/__tests__/terminal.test.ts` |

These are assertions read from the pinned reference tests; the upstream suite
was not executed for this fixture. The full expected snapshots below are local
contract expectations, with only the listed facts asserted as upstream
correspondence. Deliberate Python differences must remain explicit.
