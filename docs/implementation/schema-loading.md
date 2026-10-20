# Schema loading

`graphql_desk.schema_loading` exposes three pure backend entrypoints:

- `load_from_sdl(source)` builds and validates SDL with graphql-core.
- `load_from_introspection(data)` accepts an introspection object or an error-free GraphQL `data` envelope and validates the resulting schema. Error-bearing, malformed, ambiguous or incomplete responses fail with sanitized errors.
- `load_local_schema(path)` rereads a caller-selected `.graphql`, `.gql`, `.sdl` or `.json` file using UTF-8 (optional BOM), then delegates to the corresponding parser.

Invalid content and unreadable files raise `ValueError`. Introspection construction errors suppress upstream exception chains that can contain the raw response. Local SDL diagnostics retain parser context. This backend refuses URLs, does not fetch data, execute project code, write files or cache old schemas. Its caller must enforce project/workspace path boundaries; the explicit-path loader can read any caller-selected local file.

Configuration/routing (#27), authenticated remote loading (#59), watch/refresh integration and advanced preprocessing/output utilities (#76) remain separate work. Eight focused tests cover successful round trips, malformed inputs, sanitized tracebacks, local errors and rereading changed files. This is a local-loader foundation, not completion of SCHEMA-01.

`graphql_desk.schema_url.load_from_url(origin, environment=..., timeout=10.0,
max_response_bytes=16777216)` explicitly loads one configured URL origin without
caching or project discovery. It resolves captured header values from the supplied
environment, probes introspection feature support and requests a compatible
schema. Normal configured headers override defaults. Authentication failures do
not trigger weaker introspection retries.

The loader uses verified TLS and no ambient proxy, cookies or credentials. It
follows at most five redirects, strips all configured headers after a foreign
origin hop and rejects HTTPS downgrades. It bounds response size, checks framing
and accepts UTF-8 JSON only. Transport framing headers and response encoding remain
loader-owned. Errors suppress response bodies, credentials and upstream exception
chains. The socket timeout is not a total elapsed-time or cancellation guarantee.

Nine HTTP-server tests cover modern/legacy feature support, configured header
precedence, credential isolation, redirects, authentication failure, malformed
responses, encoding, framing, size limits and timeout behavior. Real TLS certificate
fixtures, refresh/watch integration, caching, CLI dispatch and the full authenticated
schema workflow remain open; this slice does not close #59 or a broad gate.
