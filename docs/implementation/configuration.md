# Configuration parsing foundation

`graphql_desk.config.load_project_config(path)` reads one explicitly selected UTF-8 TOML file. `parse_project_config(text, *, config_path)` accepts its contents directly. Both return immutable `ProjectConfig` facts, or `None` when `[tool.graphql-desk]` is absent. Invalid configuration raises `ValueError`; they do not search for projects or execute project code.

The parser validates documented single/named-schema keys, types and defaults, schema origins, mapping symbol syntax, generated-package layout, and output collisions using canonical paths. Named schema bindings follow the runtime factory's ASCII GraphQL-name syntax. Source-root/authoring-root containment and exact entrypoint/glob syntax are checked without scanning files. Existing symlinks are resolved for identity; companion and package initializer paths cannot escape their owning generated directories. This is a planning check, not a race-safe writer or permission grant.

`track_field_usage` defaults to `true`; `reserved_keys` accepts additional GraphQL
field names for the [same-file consumption heuristic](field-usage.md). The worker
passes these captured facts into analysis and includes them in its fingerprint.

`template_fragment_composition` defaults to `false`. Set it to `true` only when
the configured `template` callable accepts GraphQL Desk's `source=` and
`fragments=` keyword API, preserves document identity, and composes the supplied
fragments. It is rejected without `template`. Generated per-schema
`graphql_desk_generated.<schema>.graphql` factories are known to satisfy this
contract and are marked automatically; this option only opts a custom template
into fragment-extraction edits.

HTTP(S) schema origins and headers are retained as facts only. Configuration facts retain unresolved header environment references; unknown substitutions fail. `resolve_schema_headers(origin, environment=...)` resolves explicit references once using only the supplied environment mapping and returns a new redacted origin. It rejects missing variables and CR/LF/NUL expansions without printing values, never reads process environment implicitly, and does not fetch schemas. Header values and URLs are hidden from object representations, and configuration diagnostics avoid echoing supplied values. Scalar/enum/template qualified symbols are validated syntactically without importing them. Requesting the optional persistent cache currently raises an explicit unsupported error.

Python 3.10 uses pinned MIT-licensed `tomli==2.5.0`; Python 3.11+ uses standard-library `tomllib`. The runtime license inventory follows that version marker.

`graphql_desk.project.find_project(directory, workspace=None, selected_config=None)` finds the nearest configured ancestor, skipping valid inactive pyprojects. Editor callers supply a workspace boundary; searches never read configuration outside it. Invalid nearer configuration fails rather than falling back. Omitting a workspace permits CLI-style upward search.

`parse_project_settings(settings, config_path=...)` applies the same strict validation to editor fallback mappings. `effective_project(directory, explicit_config=None, workspace=None, editor_fallback=None)` selects an entire object: explicit CLI config first, then the nearest configured project, then validated editor fallback rooted at the workspace. It never merges credentials or validates an unused fallback. Invalid/missing explicit configuration and invalid nearer configuration fail closed. An explicit user-selected CLI config may be outside the nearest-search workspace; fallback's virtual filename does not redirect paths through an inactive config symlink.

Parsing/loading also accepts a non-TOML `source_scope` context, stored as a keyword-only `ProjectConfig` fact. Ordinary `project` scope keeps source roots bounded. Only `explicit_cli` permits external roots; `effective_project` supplies it for an explicitly selected configuration. Reloads preserve that context only when their canonical path matches `selected_config.path`; foreign and nearest/editor configurations stay ordinary. Generated output boundaries are unchanged. [Finite source selection](source-selection.md) consumes this fact and still respects foreign nearest-project ownership.

`find_project`, `effective_project` and `select_project_sources` also accept
`config_texts`, a closed mapping of configuration paths to captured text or `None`
for captured absence. Canonical aliases must agree; missing entries reject rather
than reread disk. This lets one captured set, including unsaved or new configs,
govern both discovery and source ownership through the existing parser. Omitting
the mapping keeps ordinary disk discovery. [Editor acquisition](editor-snapshots.md)
captures this map and owns the file checks and later freshness comparison.

`select_schema(project, binding=None)` accepts a resolved schema name; only a single `default` schema permits omission. It does not infer bindings from filenames or source text. `validate_project_ownership(projects)` checks supplied project facts for shared generated trees, explicit/implicit output paths and input clobbering, including canonical symlink identities. Equal aliases deduplicate; conflicting snapshots fail. No registry, writes or source enumeration is introduced, and callers must supply every relevant project and recheck before writes.

`project_bindings(config, project_id=...)` describes exact configured generated `graphql` factory identities and an optional template to the source resolver, without importing or creating those modules. Each `FactoryBinding` carries a separate fragment-composition capability: generated factories are enabled by definition, custom templates only when `template_fragment_composition = true`. This declaration is a user assertion, not runtime signature inspection. `schema_for_call(config, call, project_id=...)` routes actual `ResolvedCall.schema_name` metadata to a configured schema. Both use the same explicit caller-owned project identity as module snapshots; Python assignment names never select a schema. Routing preserves call URI/version/dependency/contribution facts. Generated `gql` exports are not invented, and no source-file eligibility or freshness proof is implied.

The installed `graphql-desk doctor` command (also `python -m graphql_desk doctor`)
selects the nearest configured project, or an explicit `--config PATH`. Optional
`--schema NAME` limits inspection to one configured schema. Doctor reads local
schemas or explicitly fetches configured HTTP(S) schemas using the current process
environment for header references. It does not import project Python or write files.

Doctor reports interpreter/checker distribution versions and schema availability on
stderr. For each loaded schema it checks declaration compatibility through the
existing schema renderer, including missing custom-scalar mappings, without writing
its output. Explicit `typing.Any` scalar input/output and enum mappings are named
separately. Declaration readiness does not verify imported type compatibility;
failed schema loads do not produce a declaration-readiness claim. Configured mapping
names are also checked against local `.py` and `.pyi` files reached through static
imports from those mapping modules within configured source roots; stubs take
precedence over same-module Python files. It does not scan unrelated files or import
or execute project code. There is no numeric file or size cap, so an unusually large
reachable import closure can make doctor slow. Matching external `.pyi`/`.py`
declarations are read from the selected interpreter without importing them,
including external class bases used by captured local mapping classes. External
source capture is bounded; missing, ambiguous, oversized or binary-only symbols
remain unverified. Static resolution does not establish runtime identity or checker
compatibility. Its status remains degraded
(exit 1) because full mapping compatibility, generated ownership, active editor and
live bridge health do not yet have authoritative CLI inputs.
An isolated checker fixture verifies a generated schema/factory against a
stub-only external package with mypy and Pyright. It does not establish runtime
exports or complete external type compatibility.
`--help` and `--version` exit 0; invalid arguments exit 2 without echoing supplied
values. This command is a diagnostic foundation, not full doctor acceptance.

Issue #27 remains open: command-specific overrides, source enumeration/freshness and worker integration, complete command integration, symbol resolution and writer integration still require implementation and acceptance. Local explicit-path schema loading is an independent backend. No full SCHEMA-01 or live-typing gate is claimed.
