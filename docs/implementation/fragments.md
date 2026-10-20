# Fragment snapshot index

`FragmentIndex.unavailable` retains frozen `RejectedDocument` records for failed
indexed owners. Each record holds the original resolved call and its local and
transitive dependency diagnostics, independently of where those diagnostics are
displayed. A reference error's range therefore never substitutes for the failed
owner identity. Unrelated valid documents remain in `documents`.

Resolver failures that never produced a resolved call have no invented rejected
owner. These facts support future invalid-document recovery; the artifact
generator still rejects an error-bearing index. They do not establish generated
error types, schema-unavailable publication or editor recovery.

`graphql_desk.fragments.index_fragments(sources, *, schemas)` consumes actual `ProjectSources` records from the static module resolver. Schema contexts are explicitly keyed by `(project, schema_name)` and contain the resolved schema name, `GraphQLSchema` and caller-owned generation token. An unbound `None` name also requires an explicit routing context; the index never guesses a default. Callers keep schemas stable during the synchronous call.

The frozen result contains indexed documents, selected operation and primary-fragment facts, ordered printed definitions, exact source identities, context tokens and transitive URI/version dependencies. AST objects remain internal. Each source is parsed once. Local definitions precede each explicit fragment document's complete closure in caller order, matching runtime composition. Identical imported definitions coalesce; local duplicates and conflicting imports fail. Primary identity comes from the first definition of a fragment-only document. Multiple operations require an explicit selector.

Missing/stale references, mixed source versions, project/schema/token mismatches, operation-as-fragment references and structural cycles produce diagnostics. GraphQL checks cover known types/fragments, composite fragment conditions, possible spreads and fragment cycles. Standalone fragments are usable without `NoUnusedFragments`; this index does not replace full operation validation in the renderer. Syntax/recursion failures omit affected entries and their dependents while preserving unrelated valid documents and existing source diagnostics.

The [indexed operation renderer](operation-types.md) consumes these facts for explicitly unmasked operations. Masked references/helpers, editor navigation, persisted output and scan consumers are not integrated yet; neither FRAG-01 nor the live-typing gate is complete. Tests use real imported/reexported/unsaved source records and compare definition order with runtime construction.
