# Experimental scan graph foundation

`graphql_desk.scan_graph.build_scan_graph` combines validated indexed GraphQL
definitions with a static import graph built from the already selected Python
snapshots. It does not enumerate files, execute imports, or discover standalone
`.graphql` files. Callers provide each module's project-relative path and the
project root, so source selection remains the shared `source_selection` layer's
responsibility.

Each operation and fragment retains its project/schema identity, source module,
mapped authored locations, variables/defaults, direct fragment spreads, and a
SHA-256 hash of its printed definition truncated to 16 lowercase hex characters.
Definition IDs include project, schema, kind, and name; repeated names receive
stable numeric suffixes. Anonymous operations use the same collision-safe
sequence. Documents rejected by fragment indexing are omitted from definitions
and their diagnostics remain attached to the graph; a stale call/index mismatch
is diagnosed and omitted. Conflicting fragment names
within one project/schema receive diagnostics; a spread is retained by name and
is never bound arbitrarily to one conflicting definition. Fragment spreads
produce explicit edges from the referring definition ID to the fragment ID
selected by that call's validated composition facts. This preserves explicit
fragment imports when an unrelated module has a same-named fragment; unresolved
or ambiguous targets produce no edge.

The module graph includes every selected Python module, including isolated
modules. Absolute and relative imports only create edges to selected project
modules. Inferred entrypoints have no incoming import edges; configured
entrypoint paths are relative to the selected source root, matching source
selection, and replace that set while rejecting unknown paths. Breadth-first
distances follow importer-to-imported edges. Closed import cycles remain
unreachable unless an entrypoint reaches them. Areas are the module's
project-relative parent directory, with `(root)` for the project root.

This implements graph data only. The eight scan rules, terminal/JSON/graph
renderers, CLI integration, upstream conformance corpus, and product-scale
performance acceptance remain in their separate stories; SCAN-01 and overall
gql.tada parity remain open.
