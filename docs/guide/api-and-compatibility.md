# API, compatibility and migration

This is development software targeting full gql.tada 1.11.4 plus the pinned
GraphQLSP feature set. Public-looking names are documented here; this is not a
stable-release or full-parity guarantee. The
[canonical contract](../contracts/api-and-types.md) is normative, and
[current status](../feature-status.md) distinguishes evidence from remaining work.

## Runtime and generated imports

| Import or member | Responsibility |
| --- | --- |
| `graphql_desk.TypedDocument[R, V]` | Frozen carrier of a normal graphql-core AST and selected operation; does not validate caller-supplied generic annotations or freeze mutable AST internals. |
| `graphql`, `gql` | Identical untyped runtime constructors; parse/compose executable documents and select operations. |
| `init_graphql(name)` | Runtime schema-name binding; does not load a schema or replace generated overloads. |
| `graphql.scalar(name, value)` | Identity scalar helper; generated schema bindings add input-type overloads. |
| `graphql.persisted(id, document)` | Retained typed document with persisted metadata and unchanged AST. |
| `PersistedDocument[R, V]`, `persisted_id(id)` | Source-free ID carrier and constructor; not an AST executor or APQ client. |
| `parse` | graphql-core parser; generated companions/overlays add supported exact literal views. |
| `read_fragment`, `read_fragment_passthrough` | Exact known-reference reading versus an explicit wider passthrough view; both preserve identity. |
| `mask_fragments` | Identity fixture helper with generated fixed-tuple registry typing. |
| `unsafe_read_result` | Root export and `graphql_desk.testing` export are the same identity helper; masked data is deliberately unchecked. |
| `graphql_desk.testing.read_result` | Identity helper with generated call-site registry/closure typing. |
| `<package>.<schema>.graphql` | Precise generated factory import; source/operation/fragment-specific overloads. |
| `<package>.sources.<module>` | Result, variable, fragment-reference, fixture and bound reader aliases. |
| `<package>.parsers.<module>.parse` | Captured module's opt-in exact parser companion. |
| `<package>.persisted.<schema>` | ID constants and type-only persisted result/variable declarations. |

Discovery and generation do not import project Python. Importing generated runtime
modules is ordinary application execution and can import configured mapping types.
An arbitrary client method does not infer these generics automatically; use an
explicit typed adapter such as the [worked bindings](../../scripts/client_bindings.py).

The callable CLI surface in `graphql_desk.cli` includes `check`, `generate_output`,
`generate_persisted`, `generate_schema`, their structured error types, and
`main(argv) -> int`. `check` returns diagnostics; generators return `None` or raise
sanitized structured errors. `graphql_desk.scan.scan_project` returns an
experimental project report. Imports do not dispatch commands. Optional turbo
commands are not implemented just because the target contract names them.

## Advanced internal utilities

`graphql_desk.internal` is an advanced surface without a stability promise. Use
the complete [internal export correspondence and examples](../implementation/internal-api.md)
and the actual [`__all__` declaration](../../src/graphql_desk/internal.py) rather
than copying TypeScript compiler objects into Python. Its groups include:

- Loaders/references: `load_from_sdl`, `load_from_url`, `load`, `load_ref`,
  `SchemaLoader`, `SchemaLoaderResult`, `SchemaRef`, load/input records and typed
  update/error callbacks.
- Configuration: `parse_config`, `load_config`, `load_configs`, `find_config_file`,
  `read_config_file`, `resolve_project_root`, schema-name selection and unique
  output ownership validation; these reuse the existing Python configuration model.
- Introspection: `minify_introspection`, `preprocess_introspection`, `SchemaMap`,
  `output_introspection_file`, and `extract_introspection_header`.
- Marker/error types: `FragmentIdentity`, `FragmentRef`, `FragmentDocument`,
  `GraphQLDeskError`, `CheckerDiagnostic`, `CheckerIntegrationError`, plus the
  standard parser/document and schema/scalar records.

Loader construction/import is inert. Explicit loads can read or fetch; explicit
subscriptions start background update work and must be unsubscribed. Failed
reloads do not present a stale schema as current. Output utilities return text;
callers own writes, trust, lifetime and freshness. Internal `load_from_sdl(path)`
creates a loader; the lower-level schema-loading function of that name parses
text. Keep that distinction when migrating callers.

## Migrate from untyped Python or gql.tada

1. Keep your existing execution client and error policy. Install GraphQL Desk in
   the selected environment and add a valid project schema/configuration.
2. Change static authoring calls to the generated per-schema `graphql` import;
   regenerate saved output or enable the trusted development editor bridge.
3. Replace handwritten result/variable annotations with generated source aliases.
   Construct/annotate variables at generic client boundaries; preserve the
   response's optional data and native errors.
4. Read masked fields through the exact fragment or bound reader. Use fixed tuples
   for precise fixture registries. Choose the wider passthrough helper explicitly
   when an unrelated reference must survive.
5. For source-free release, move source into dedicated authoring roots, import
   generated IDs in the app, build the omitted projection and inspect the wheel.

Python has no tagged templates, declaration merging or JavaScript `undefined`.
Use calls, declarative configuration, generated aliases and omitted dictionary
keys. Do not insert an undefined sentinel, wrap JSON to implement masking, or
assume Python's runtime can verify a phantom reference. Default enum strings and
separate scalar input/output mappings do not coerce response values. Named
multi-operation documents use explicit `operation="Name"`; a fragment-only
document's first definition is its primary fragment.

Do not switch to ID-only imports while still using an AST-only transport. Do not
make `unsafe_read_result` a substitute for response validation. Do not expect
mypy to read Pylance's managed `stubPath`; its SDK-overlay path remains explicit.
The [fragment notes](../implementation/fragment-refs.md) describe conservative
abstract/nullable/absence cases and raw structural-input limits.

## Maturity and platform boundaries

Package metadata requires Python >=3.10; the planned initial test matrix is
CPython 3.10–3.14. Pinned editor acceptance targets VS Code desktop 1.141.0 and
1.142.0 with the specified Python/Pylance pair. The planned OS matrix is Windows
11 x64, Ubuntu 24.04 x64 and macOS 15 Arm64. CI Windows Server smoke coverage is
not Windows 11 editor acceptance. A local example passing is not proof of every
minor, host platform, wheel install or Marketplace distribution.

Remote SSH/WSL/container hosts need their own path/URI and interpreter evidence;
other editors and browser-only VS Code are not initial support promises. Keep
Pylance user-installed under its normal terms. See the exact pins and budgets in
the [support contract](../contracts/support-performance-and-evidence.md).

All broad parity gates remain open. A scoped live-typing milestone passed, but
later lifecycle/recovery and reader-retention work remains required. Doctor can
accurately report degraded health even with an active Pylance extension; no
active-extension version or surviving generation folder acknowledges fresh type
consumption. Performance and full pinned upstream conformance are still release
requirements, not results established by these guides.

Standalone `.graphql` discovery, persistent turbo caching, extra editors and saved
model/client generation are optional or separate work, not available features
promised here. The Ariadne/Turms evaluation does not install or adopt either as a
core backend. The owner selected the [MIT license](../../LICENSE) on 2026-11-06;
third-party dependencies keep their own license terms.
