# Schema loading

`graphql_desk.schema_loading` exposes three pure backend entrypoints:

- `load_from_sdl(source)` builds and validates SDL with graphql-core.
- `load_from_introspection(data)` accepts an introspection object or an error-free GraphQL `data` envelope and validates the resulting schema. Error-bearing, malformed, ambiguous or incomplete responses fail with sanitized errors.
- `load_local_schema(path)` rereads a caller-selected `.graphql`, `.gql`, `.sdl` or `.json` file using UTF-8 (optional BOM), then delegates to the corresponding parser.

Invalid content and unreadable files raise `ValueError`. Introspection construction errors suppress upstream exception chains that can contain the raw response. Local SDL diagnostics retain parser context. This backend refuses URLs, does not fetch data, execute project code, write files or cache old schemas. Its caller must enforce project/workspace path boundaries; the explicit-path loader can read any caller-selected local file.

Focused local-loader tests cover successful round trips, malformed inputs, sanitized tracebacks, local errors and rereading changed files. Advanced preprocessing/output utilities and the consolidated `graphql_desk.internal` export surface remain tracked by #76. This module alone does not complete SCHEMA-01.

## Authenticated URL loading and refresh

`graphql_desk.schema_url.get_url_config(origin)` accepts a `SchemaOrigin` or `None`
and returns the same origin for a valid HTTP(S) URL. Local paths, mixed local/URL
origins, unsupported schemes and unsafe URLs return `None`. Header templates stay
unresolved; this helper does not inspect the environment or contact the network.
The existing origin record hides URLs and headers from its `repr`.
This independently callable discriminator supplies the `getURLConfig` capability
from the pinned upstream advanced API. Current editor discovery already returns
typed origins, so it passes those directly to the loader; both entrypoints share
the same URL validation. The future combined loader/export surface is tracked by #76.

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

The production editor refresh path captures only the configured header environment
variables. It binds a fetched schema to its project, configuration and opaque
credential revision. Source edits can retain a current fetched schema without
another HTTP request. A failed refresh exposes an unavailable/stale state; retained
last-good data is never supplied as current analysis or substituted into another
schema. Changed credentials or configuration invalidate the prior context. Explicit
retry can recover it. Polling requires a trusted workspace and an explicitly positive
`schema_refresh_interval`; its cancellation and retirement abort pending worker
requests and reject late responses.

Issue #59 acceptance uses these existing and extended fixtures:

| Behavior | Acceptance fixture |
| --- | --- |
| Pure URL configuration, modern/legacy negotiation, header precedence, redaction, HTTP/GraphQL/partial-introspection errors, framing, size and socket limits | `tests/test_schema_url.py` |
| Real verified TLS with a trusted test CA; rejection of untrusted, expired and wrong-host certificates; HTTPS downgrade rejection before credentials reach HTTP | `tests/test_schema_url.py`, `tests/fixtures/tls/` |
| Captured remote facts, stale rejection, unchanged-config retention, secret-safe metadata and last-good isolation across credentials/projects/configuration | `tests/test_editor_snapshot.py`, `tests/test_editor_worker.py` |
| Trusted opt-in polling, explicit refresh, disabled polling, current credential capture, cancelled/retired requests and rejection of late replies | `editor/test/analysisController.test.cjs`, `editor/test/analysisWorker.test.cjs` |
| Installed extension authenticated refresh, failure and recovery, credential rotation, opt-in polling and preservation of dirty buffers/disk files | `editor/test/activation.host.cjs` on both pinned VS Code hosts |

The socket loader intentionally has no ambient cache or asynchronous cancellation
API; the editor owns retention, scheduling and worker cancellation. Completing this
story requires the listed focused tests and both host checks to pass. CLI dispatch,
the remaining advanced utility exports and broad SCHEMA-01 acceptance are separate
stories; a URL-loader or editor-refresh pass does not close those gates.
