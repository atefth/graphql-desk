# Operation declaration renderer

`graphql_desk.operation_types.render_operation_types(schema, source, *, name, operation=None, scalars=(), enums=())` returns a complete Python declaration module containing the schema aliases, `<name>Variables` and `<name>Result`. It validates the document before rendering; select an operation explicitly when the document contains several.

The initial subset supports query/mutation/subscription roots, nested object selections, aliases, merged compatible repeated fields, built-in scalars, enum literals and literal `__typename`. List and null wrappers are preserved. Nullable variables and variables with defaults may be omitted; a non-null variable with a default still rejects an explicit null. Generated declaration names must be safe, normalized Python identifiers and must not collide with schema or other generated declarations.

Configured scalar mappings distinguish variable/input and result/output aliases; enum remappings apply in both directions. Mapping facts and limitations follow the [schema renderer](schema-types.md). Fragments, all directives, abstract output types, unmapped custom scalars and introspection object selections currently raise errors. Unsupported semantics never become `Any`. The schema renderer supplies closed oneOf input alternatives and preserves variable omission/default behavior. This function produces declarations only: it does not attach them to factories, execute requests, coerce responses, write files or refresh Pylance.

Tests execute generated modules and check both valid and invalid bindings with strict mypy/Pyright. This is a foundation for #22, not completion of TYPE-01 or the live-typing gate. Remaining semantics and integration are tracked separately in the [full inventory](../feature-tracking.md).
