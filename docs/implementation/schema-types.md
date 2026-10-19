# Schema declaration primitive

`from graphql_desk.schema_types import render_schema_types` exposes a pure `render_schema_types(schema: GraphQLSchema) -> str`. It returns deterministic, importable Python declarations; it never writes files or imports project modules. The caller supplies an already constructed schema.

The initial slice emits built-in scalar aliases, Literal enums and recursive TypedDict inputs. It preserves each list/null wrapper and distinguishes optional dictionary keys with NotRequired. Non-null input members with defaults retain the pinned upstream's nullable static compatibility behavior; GraphQL execution may still reject explicit null. This renderer does not validate response data.

Custom scalar mappings, enum remapping, oneOf inputs and unsafe/colliding declaration names currently raise explicit errors. They remain required future work; no Any fallback is produced. Keyword input field names are preserved with functional TypedDict declarations.

This is not an operation/result renderer or generated factory. Source ownership, configured output paths, runtime companions, operation Literal overloads, fragments, abstract selections and live generation remain unimplemented. No schema/API/type or live-typing product gate passes from this primitive alone. See the [API/type contract](../contracts/api-and-types.md) for the full required behavior.
