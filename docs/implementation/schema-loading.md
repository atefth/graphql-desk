# Local schema loading

`graphql_desk.schema_loading` exposes three pure backend entrypoints:

- `load_from_sdl(source)` builds and validates SDL with graphql-core.
- `load_from_introspection(data)` accepts an introspection object or an error-free GraphQL `data` envelope and validates the resulting schema. Error-bearing, malformed, ambiguous or incomplete responses fail with sanitized errors.
- `load_local_schema(path)` rereads a caller-selected `.graphql`, `.gql`, `.sdl` or `.json` file using UTF-8 (optional BOM), then delegates to the corresponding parser.

Invalid content and unreadable files raise `ValueError`. Introspection construction errors suppress upstream exception chains that can contain the raw response. Local SDL diagnostics retain parser context. This backend refuses URLs, does not fetch data, execute project code, write files or cache old schemas. Its caller must enforce project/workspace path boundaries; the explicit-path loader can read any caller-selected local file.

Configuration/routing (#27), authenticated remote loading (#59), watch/refresh integration and advanced preprocessing/output utilities (#76) remain separate work. Eight focused tests cover successful round trips, malformed inputs, sanitized tracebacks, local errors and rereading changed files. This is a local-loader foundation, not completion of SCHEMA-01.
