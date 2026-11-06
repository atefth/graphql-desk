# Configuration and CLI

Configuration is data in `[tool.graphql-desk]` inside `pyproject.toml`. Discovery
does not import or execute application modules. Relative configuration paths are
relative to that file; a CLI `--output` is relative to the working directory.
`${configDir}` denotes the configuration directory.

An explicit `--config PATH` wins over nearest-project discovery. The editor uses
the nearest configured project within its workspace; malformed nearer settings
fail instead of falling back. `graphqlDesk.project` is a whole-object fallback
when no active project table exists, not a source of individual credentials to
merge with another configuration. Nested projects have independent ownership.

## Schema, types and source selection

```toml
[tool.graphql-desk]
schema = "schema.graphql"
generated_package = "api_generated"
schema_output = "api_generated/default.pyi"
persisted_output = "persisted-manifest.json"
source_roots = ["."]
include = ["app/**/*.py"]
exclude = ["app/vendor/**"]
```

`generated_package` defaults to `graphql_desk_generated`. Generated schema output
must be a `.pyi` inside that package. Source patterns are project-relative; source
roots stay inside the project unless an explicitly selected CLI configuration
permits external roots. Generated trees, build output, environments and configured
authoring/build paths have additional eligibility rules. See
[source selection](../implementation/source-selection.md) before changing roots or
entrypoints; a matching filename does not override nearest-project ownership.

For multiple schemas, use `schemas` records instead of the single-schema keys:

```toml
[tool.graphql-desk]
generated_package = "api_generated"

[[tool.graphql-desk.schemas]]
name = "catalog"
schema = "catalog.graphql"
schema_output = "api_generated/catalog.pyi"

[[tool.graphql-desk.schemas]]
name = "account"
schema = "account.graphql"
schema_output = "api_generated/account.pyi"
```

Author with `api_generated.catalog.graphql` or
`api_generated.account.graphql`. Use `--schema catalog` to select a named schema
for a command. There is no filename-based or ambiguous default-schema guess.
Projects and schemas cannot share conflicting generated destinations.

Custom types are qualified names, resolved statically during inspection:

```toml
[tool.graphql-desk.scalars]
DateTime = { input = "builtins.str", output = "datetime.datetime" }

[tool.graphql-desk.enums]
State = "app_types.State"
```

A single scalar string applies to input and output. Generation does not decode
values into those classes. Your application must arrange serialization/decoding
and runtime imports where required. Missing custom-scalar mappings are errors;
explicit `typing.Any` mappings remain visible in doctor.

Other implemented settings:

| Setting | Behavior |
| --- | --- |
| `disable_masking` | Defaults false; controls fragment type exposure. |
| `track_field_usage`, `reserved_keys` | Enable the same-file usage heuristic (default true) and additional exemptions. |
| `check_colocated_fragments` | Defaults true; diagnoses imported colocated fragments that are not supplied. |
| `client_directives` | Extra directive names without `@`; does not implement their server behavior. |
| `template` | Additional exact imported factory identity; no evaluation by name alone. |
| `template_fragment_composition` | Explicitly asserts that a custom template supports `source=`/`fragments=` composition for extraction edits; generated factories already satisfy it. |
| `entrypoints` | Project-relative Python entrypoints for project analysis. |
| `authoring_roots` | Dedicated selected source roots excluded from a source-free persisted build. |
| `schema_refresh_interval` | Zero by default; positive seconds enable trusted remote polling. |
| `disable_preprocessing` | Accepted generation metadata; current Python output types/bytes do not change. |
| `cache_enabled`, `cache_output` | Reserved for optional persistent acceleration; enabling the unsupported cache fails explicitly. |

Unknown keys and substitutions are rejected. Python uses call expressions, not
tagged templates; `templateIsCallExpression=false` is rejected. The
[configuration implementation](../implementation/configuration.md) and
[canonical contract](../contracts/config-cli-and-lifecycle.md) give the complete
validation and precedence rules.

## Remote schemas

```toml
[tool.graphql-desk]
schema = { url = "https://example.invalid/graphql", headers = { Authorization = "Bearer ${env:GRAPHQL_TOKEN}" } }
schema_output = "graphql_desk_generated/default.pyi"
```

Set the named variable in the invoking environment. Expansion is one pass; header
values and URL details are redacted from failures. Schema loading uses bounded
HTTP/TLS behavior. CLI generation and explicit terminal schema checks can fetch
the configured endpoint. Typing and the captured editor doctor do not fetch by
themselves. In the trusted editor use **GraphQL Desk: Refresh Schema**, or opt into
polling with a positive interval. An unavailable or stale remote schema is not
reported as current merely because a previous generation exists.

## Commands and outputs

All commands accept `--help`. `--config` and `--schema` are available globally or
on the applicable subcommand; `init` instead takes an existing directory.

| Command | Purpose and output |
| --- | --- |
| `init [directory]` | WIP interactive setup with preview/confirmation. Preserves unrelated TOML; no installs or generated package creation. |
| `generate-output --output api_generated/default.pyi` | Generates the configured schema/factory/result/variable/source companions. Explicit output must match the selected schema path. |
| `generate-schema INPUT [--output schema.graphql]` | Converts SDL/introspection JSON or downloads a URL to SDL. Repeat `--header 'Name: value'` when needed. |
| `generate-persisted` | Writes configured server manifests and ID-only typed bindings; see the release guide. |
| `check` | Source/schema diagnostics. `--level` filters display only; errors still fail. `--fail-on-warn` also fails warnings. |
| `scan` | Experimental project insights. Terminal defaults to text; a pipe/file defaults to JSON. `--graph` selects graph JSON. Findings alone do not fail; `--fail-on-warn` applies to discovery/parse warnings. |
| `doctor` | Saved setup diagnostics. Remains degraded/nonzero without authoritative live editor/ownership/bridge evidence. |

Without explicit `--output`, redirected `generate-output` writes one selected
schema stub to stdout rather than the whole package. Use an explicit configured
output path in automation. `generate-schema` also supports clean redirected SDL;
protect the source when converting JSON by choosing a different destination.

`check --format json` emits structured diagnostics on stdout. Text diagnostics go
to stderr; in GitHub Actions text mode also emits escaped workflow annotations on
stdout. `--annotations-output PATH` separates annotation output from JSON. Errors
exit 1; invalid arguments exit 2; interruption exits 130. Display filtering cannot
turn a validation failure into success. See [check details](../implementation/cli-check.md)
and [scan semantics](../implementation/scan-graph.md).

Generated output is owned by a manifest. Modified or foreign files cause a
conflict instead of being overwritten. Follow a reported recovery location or
**GraphQL Desk: Review Interrupted Config Write**; do not erase ownership state,
save over a dirty buffer, or treat an old folder as fresh typing. The terminal
doctor can report installed distributions and its interpreter but cannot establish
an active checker. The editor doctor uses its own current session and can still
be degraded when publication verification is unavailable. Read the current
[doctor contract](../implementation/doctor.md); activation is not a per-generation
Pylance acknowledgement.
