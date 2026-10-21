# Operation declaration renderer

`graphql_desk.operation_types.render_operation_types(schema, source, *, name, operation=None, scalars=(), enums=(), client_directives=(), disable_masking=False)` returns a complete Python declaration module containing the schema aliases, `<name>Variables` and `<name>Result`. It validates the document before rendering; select an operation explicitly when the document contains several.

The initial subset supports query/mutation/subscription roots, nested object selections, aliases, merged compatible repeated fields, built-in scalars, enum literals and literal `__typename`. Query-root `__schema` and `__type` selections use graphql-core's field definitions, including nested introspection objects and finite type-kind/directive-location enums. Null/list wrappers, aliases and configured enum mappings follow the ordinary selection rules; invalid meta-field locations or arguments remain GraphQL errors. List and null wrappers are preserved. Nullable variables and variables with defaults may be omitted; a non-null variable with a default still rejects an explicit null. Generated declaration names must be safe, normalized Python identifiers and must not collide with schema or other generated declarations.

Union and interface selections produce concrete result alternatives, including inline fragment conditions. Fields retain the types declared at their authored scope, including interface nullability. Explicit or aliased `__typename` selections are required literal discriminators. Abstract alternatives also expose an optional canonical `__typename` aid unless an authored response key already owns that name; an alias such as `__typename: id` keeps its actual ID type. The aid does not add a runtime response field. Abstract types with no possible implementations use a private `Never` alias while preserving list/null wrappers.

Field `skip`, `include` and schema-legal `defer` directives make keys optional even with literal conditions. An unconditional repeated scalar selection guarantees key presence. `required`/`_required` remove outer nullability; `optional`/`_optional` add it, with required taking precedence and list-item wrappers unchanged. Typename remains a literal under nullability overrides, matching the pinned source. `stream` has no special result-type effect. The renderer suppresses unknown-directive errors only for GraphQLSP's pinned built-in client names and explicit `client_directives` names. Schema-declared directive arguments, locations and variables remain validated; no directive declarations are injected or execution behavior invented.

Conditional inline fragments and repeated conditional composite selections produce absent/present alternatives, retaining independent selected fields. Affected alternatives and their descendants use closed dictionary shapes so both checkers reject partial combinations, including wider pretyped values. Syntax occurrences vary independently even when conditions use the same variable or literal; this is conservative presence inference, not Boolean evaluation. A pinned TypeScript probe and real GraphQL execution showed upstream intersections can require omitted child fields; these alternatives preserve actual omission instead.

Equivalent alternatives coalesce incrementally, comparing effective outer nullability so redundant overrides and their spelling do not consume the conditional budget. More than 64 alternatives at one conditional selection or 256 conditional alternatives across one render raises an explicit error without returning partial output. Ordinary conditional fields retain `NotRequired` behavior. Repeated simultaneously active fields intersect their effective outer nullability: a non-null occurrence wins. Conditional occurrences with different effective nullability produce alternatives only when they can change the result; an unconditional non-null occurrence avoids redundant scalar alternatives. Inner list wrappers and conditional key presence stay independent. Masking and configuration/consumer integration keep #33 open.

Raw named spreads require explicit `disable_masking=True`; masked rendering needs
the indexed ownership facts described below. Unmasked expansion preserves each
fragment's authored type condition and conditional spread/definition gates, using
a local fragment/concrete-type memo for diamonds. Validation omits only
`NoUnusedFragmentsRule`, matching the pinned GraphQLSP language-service policy so
an explicit registry may contain unused definitions. Unknown/cyclic/impossible
spreads, invalid fields and non-executable definitions still reject.

`render_indexed_operation_types(context, document, *, name, scalars=(), enums=(), client_directives=(), disable_masking=False, fragment_index=None)` consumes the [fragment index](fragments.md), checks supplied schema/token and selected-operation identity, and delegates the joined semantic definitions to the same renderer. It derives the indexed operation selector; callers cannot override that binding. Index facts are trusted inputs. This adapter does not acquire current snapshots or prove freshness: the lifecycle caller must recheck source/config/schema versions.

For masked spreads, supply the full `fragment_index`. Each definition resolves to
its owning document with matching project/schema/context and valid local
membership. Missing or duplicate owners reject. Shared [fragment identities](fragment-refs.md)
use that owner's transitive dependencies, so different consumers agree on the
same fragment capability. Direct fields remain visible; a masked spread supplies
its required read-only callable phantom capability instead of exposing its fields.
Co-present capabilities combine, while conditional and abstract branches retain
their actual presence alternatives and list/null wrappers. No response keys are
inserted or inspected at runtime.

The same module includes `<name>Fragment_<fragment name>` payload declarations
for its indexed fragment definitions, with each fragment's own selected fields
visible and nested spreads still masked. Definition-level `@_unmask` expands that
fragment without adding its own capability; nested fragments keep their policy.
Global `disable_masking=True` expands all spreads without reference metadata.
Schema aliases are emitted once. This entry point still selects an operation.

`render_indexed_fragment_types` accepts the same keyword options and uses the
indexed `primary_fragment` instead. It renders `<name>Result` from the fragment's
actual type condition, without inventing an operation or schema field.
`<name>Variables` is empty because fragments declare no operation variables.
Nested spreads use the same masking policy and owner registry. Abstract
alternatives and list/null wrappers follow the shared renderer, and incompatible
primary/schema/context facts reject. Factory attachment and reader helpers remain
separate work.

Configured scalar mappings distinguish variable/input and result/output aliases; enum remappings apply in both directions. Mapping facts and limitations follow the [schema renderer](schema-types.md). Raw masked spreads without ownership and unmapped custom scalars currently raise errors. Unsupported semantics never become `Any`. The schema renderer supplies closed oneOf input alternatives and preserves variable omission/default behavior. This function produces declarations only: it does not attach them to factories, execute requests, coerce responses, write files or refresh Pylance.

Tests execute generated modules, compare real GraphQL response aliases, and check both valid and invalid bindings with strict mypy/Pyright, including the actual resolver/index/renderer pipeline. The abstract selection rules were checked against pinned gql.tada selection source. These are foundations for #22, #32 and #35, not completion of TYPE-01 or the live-typing gate. Reader helpers and generated/live consumers keep the advanced stories open. Remaining semantics and integration are tracked separately in the [full inventory](../feature-tracking.md).
