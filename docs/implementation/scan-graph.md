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
stable numeric suffixes, with the originating call's source range breaking ties
for otherwise identical definitions. Anonymous operations use the same
collision-safe sequence. Documents rejected by fragment indexing are omitted from definitions
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
entrypoint paths are project-root-relative and replace that set while rejecting
unknown paths. Breadth-first
distances follow importer-to-imported edges. Closed import cycles remain
unreachable unless an entrypoint reaches them. Areas are the module's
project-relative parent directory, with `(root)` for the project root.

The bounded `field-usage` rule is implemented in
`graphql_desk.scan_rules.field_usage_rule`. It counts each schema coordinate
once per operation or fragment, expands resolved fragment edges to reaching
operations, and unions reverse-import reach, areas, and entrypoints. Results
are schema-qualified and omit unused and introspection fields. When a project
has no entrypoints, weight falls back to the number of reached modules.

`graphql_desk.scan_input_rules.input_usage_rule` counts schema-typed input
object fields and enum literals in operations, fragments, directives, and
variable defaults. Runtime variables and values without a schema-known input
type do not produce rows; each row's weight is its syntactic occurrence count.

`graphql_desk.scan_deprecation_rules.deprecated_usage_rule` reuses field-use
reach and reports deprecated coordinates with distinct reaching operation IDs,
their deprecation reason when present, and a zero count for deprecated fields
used only by orphan fragments.

`graphql_desk.scan_fragment_rules.orphan_fragment_rule` reports fragments that
are never the target of a resolved direct spread; a fragment used from another
orphan fragment is not orphaned, and a spread cycle is not orphaned. Its paired
`cross_feature_fragment_rule` counts direct spreads by consumer area, includes
the defining area and `(root)` in consumer lists, emits only cross-area uses,
and weights each row by distinct external areas.

`graphql_desk.scan_complexity_rules.operation_complexity_rule` counts distinct
reachable non-introspection coordinates and list fields, computes cycle-guarded
fragment depth, and ranks with `fieldCount + depth² + 3 × listFields`. This is a
static ranking signal, not a server execution-cost estimate.
`graphql_desk.scan_depth_rules.fetch_depth_rule` emits query operations only,
ranked by shortest import distance from inferred/configured entrypoints; closed
unreachable module cycles have null depth and sort last.
`graphql_desk.scan_directive_rules.directive_usage_rule` counts directive
applications in original GraphQL source before client directive stripping and
ranks by descending count, then directive name. Input and directive findings
retain their referenced definition IDs so terminal results can point back to
the Python location containing each finding.

`graphql_desk.scan.scan_project()` now builds one read-only report from the
configured project and invokes all eight rules. The `graphql-desk scan` command
offers an experimental ranked terminal report, two-space version-1 JSON, and
pure graph JSON through `--graph`; graph output takes precedence over
`--format`. Pipe and output-file use default to JSON; an interactive terminal
defaults to the human report. An explicit `--format` overrides that default.
Machine reports include schema-qualified coverage, operation and
fragment records, rule data, and discovery/parse warnings. Coverage rounds
integer percentages to nearest with half values rounded up, and an empty schema
reports 100%. Named schema coordinates stay distinct.

Configured entrypoints use project-root-relative Python paths, matching the
configuration contract. For an explicitly selected external source root, report
paths use a stable `external-source-root-N/` prefix so terminal and machine
output do not reveal absolute local paths. A requested output file is staged
atomically and cannot replace project inputs, configured schema/generated
artifacts, or paths reached through symlinks. `--fail-on-warn` changes the exit
code only for source discovery/parse warnings; findings alone do not fail.
Python files honor their declared source encoding; undecodable input produces a
sanitized command error instead of a traceback.
Machine stdout contains only JSON, warnings are included in that JSON, and a
specified output file leaves stdout free.

The terminal/JSON/graph and CLI paths now have focused fixtures for output
equivalence, exact 2-of-3 coverage, schema isolation, graph edges, destination
safety, and warning policy. Pinned upstream output conformance, the full
source/import corpus, broader CLI acceptance, and product-scale performance
remain open. SCAN-01 and overall gql.tada parity remain open.
