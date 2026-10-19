# Configuration parsing foundation

`graphql_desk.config.load_project_config(path)` reads one explicitly selected UTF-8 TOML file. `parse_project_config(text, *, config_path)` accepts its contents directly. Both return immutable `ProjectConfig` facts, or `None` when `[tool.graphql-desk]` is absent. Invalid configuration raises `ValueError`; they do not search for projects or execute project code.

The parser validates documented single/named-schema keys, types and defaults, schema origins, mapping symbol syntax, generated-package layout, and output collisions using canonical paths. Named schema bindings follow the runtime factory's ASCII GraphQL-name syntax. Source-root/authoring-root containment and exact entrypoint/glob syntax are checked without scanning files. Existing symlinks are resolved for identity; companion and package initializer paths cannot escape their owning generated directories. This is a planning check, not a race-safe writer or permission grant.

HTTP(S) schema origins and headers are retained as facts only. Header environment references remain unresolved; unknown substitutions fail. Header values and URLs are hidden from object representations, and configuration diagnostics avoid echoing supplied values. Scalar/enum/template qualified symbols are validated syntactically without importing them. Requesting the optional persistent cache currently raises an explicit unsupported error.

Python 3.10 uses pinned MIT-licensed `tomli==2.5.0`; Python 3.11+ uses standard-library `tomllib`. The runtime license inventory follows that version marker.

Issue #27 remains open: nearest-project/workspace discovery, cross-project ownership, document routing, environment/credential resolution, symbol resolution, source matching and writer integration still require implementation and acceptance. Local explicit-path schema loading is an independent backend. No full SCHEMA-01 or live-typing gate is claimed.
