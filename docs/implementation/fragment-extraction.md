# Fragment extraction

Extraction replaces a complete selected sibling field set with a fragment spread, preserves any selected external fragment dependencies, and validates the transformed document before returning versioned edits.

At module scope, the fragment stays a named Python binding immediately before the query. Inside a direct ordinary or async function body, the fragment `gql(...)` call is placed inline in the operation's `fragments` list. The Python source resolver intentionally rejects function-local fragment-variable references; inline construction keeps both documents in the same function execution and lets the existing resolver validate and index them without widening source-scope inference.

Function-scope support covers direct `Assign` and `AnnAssign` statements whose target is a simple name and whose factory is a trusted `graphql_desk.gql`/`graphql` import or a known module-level `init_graphql` result. Nested functions, class bodies, branches, local factory aliases, global/nonlocal declarations, and `locals()`/`vars()` inspection remain unsupported. The provider preserves suite indentation and the source newline style and rejects stale or cancelled edit plans.

This is a bounded #51 slice. Generated/custom factories, module-qualified factory mutation, function-local nested selection, and inline-only nested-fragment dependency cases remain open; it does not pass a broad parity gate.
