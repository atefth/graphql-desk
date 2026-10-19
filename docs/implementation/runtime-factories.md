# Runtime document factories

`graphql_desk.graphql(source, fragments=(), *, operation=None)` and its identical `gql` alias construct `TypedDocument[Any, Any]`. `init_graphql("SchemaName")` returns a frozen callable with the same construction behavior and schema-name metadata. Names follow GraphQL identifier syntax; this does not resolve project configuration or load a schema. The root `parse` export is graphql-core's ordinary parser; exact AST declarations remain #25.

Factories parse executable documents, select a single operation (or require an explicit name for several), and compose supplied fragment-only documents. Identical definitions are deduplicated, conflicting definitions, unknown spreads and cycles raise errors. Supplied fragment nodes retain their identity. For fragment-only source, its first definition remains the primary fragment.

The optional carrier `.schema_name` is keyword-only, preserving existing positional `(ast, operation_name)` construction. Known schema bindings propagate through unbound composition; different known bindings cannot be combined. An unbound fragment has no independently verified schema identity. Schema field validation and project routing remain separate work.

Construction removes the internal `@_unmask` directive from the first definition
when that definition is a fragment, matching the [pinned upstream runtime](https://github.com/0no-co/gql.tada/blob/cfe64f433bd29ff6f79e462596389b81c6e01aec/src/api.ts#L329-L336).
Other directives retain their order. Later definitions and supplied fragment nodes
are preserved; this is not a general directive-stripping pass. The original source
location body remains unchanged. A normally constructed unmasked fragment can
therefore be composed and validated against a server schema without exposing that
internal marker. Type generation reads the original source separately.

Construction does not execute requests, coerce responses, or infer precise result/variable types. Frozen carrier/factory fields do not freeze mutable graphql-core AST internals. Tests cover ordinary graphql-core execution, node identity, composition errors and strict checker boundaries. Full helper/client and live-typing acceptance remain open.
