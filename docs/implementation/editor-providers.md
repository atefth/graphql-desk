# Read-only embedded editor providers

`editor/src/languageFeatures.js` exposes `createLanguageFeatures({analyze, isCurrent, isTrusted, clientDirectives?})`. Its `complete`, `hover`, `definition` and `diagnostics` methods accept a plain `{uri, version, text}` Python document snapshot; cursor offsets and returned spans use UTF-16. The injected `analyze(snapshot, cancellationToken)` returns `{request, response}` from the [snapshot analysis boundary](editor-analysis.md), retaining the original schema text. No process, configuration discovery or production analysis adapter is started here.

`isCurrent(bundle, snapshot, cancellationToken)` is mandatory. The caller must compare all contributing source/config versions, fragment dependencies and schema identities/tokens with current state. Providers also check the local document snapshot and response schema facts, and recheck trust, cancellation and freshness before returning. A failed, stale, cancelled or ambiguous analysis returns no precise result. Failures do not replace the native Python service. The injected callback is an internal trusted analysis boundary; this module does not independently validate arbitrary malformed response JSON.

The pinned lower-level `graphql-language-service==5.7.0` and `graphql==16.12.0` supply online completion/hover and validation primitives. Validation uses separately parsed raw source ASTs with their original locations, rather than concatenating printed fragments or compensating for guessed range endpoints. Diagnostics retain separate syntax, validation, deprecation and source/index codes, with identical results deduplicated. Configured client directive names may be supplied through `clientDirectives(bundle, call)`; undeclared pinned client names are suppressed without bypassing validation of schema-declared directives.

Incomplete GraphQL inside valid Python still has real maps and can complete. Completion inserts a plain GraphQL name/value label only where one linear current-file map span supplies an unambiguous replacement. It rejects atomic escape interiors, ranges crossing literal gaps and repeated/shared origins. It does not rewrite Python escaping, insert snippets, plan refactors or claim project-wide edit safety. Native hover signatures/descriptions/deprecation are retained; input-member qualification is corrected from actual input type metadata, and actual argument/input defaults are shown. Hover Markdown is untrusted and HTML-disabled.

Definitions cover schema fields, arguments, directives, named types, enum/input members, local variables and explicit indexed fragments. They use original SDL/node locations and raw call maps; printed definition offsets are never treated as authored positions. Introspection schemas lack authored SDL definitions, so no schema location is invented. Definition traversal requires parseable GraphQL. Missing or unresolved fragment facts do not become trusted navigation targets.

`registerLanguageFeatures(vscode, options)` adds supplemental Python completion/hover/definition providers and a separate diagnostic collection. The returned `refresh(document, token)`, `close(uri)` and `dispose()` functions are lifecycle hooks for the activation owner. Per-document generations prevent an older diagnostic refresh from overwriting a newer one. No source or generated buffer is edited, saved or reverted.

`editor/src/highlighting.js` exposes `createHighlighting(options)` and `registerHighlighting(vscode, options)`. Resolved GraphQL tokens project through actual maps into literal spans. Existing theme colors are applied as confined decorations, without replacing Python semantic-token providers. In incomplete Python, or an untrusted workspace, a separate lexical candidate mode recognizes a limited set of direct imported constructors. It never supplies diagnostics, navigation, types or trusted source facts, and untrusted mode never calls analysis. This fallback skips interpolated/byte strings and escaped ordinary literals requiring semantic decoding; it is not complete Python name resolution or the full highlighting grammar acceptance matrix.

Native Node tests run the real Python snapshot analyzer with isolated UTF-8 JSON transport (`-I -X utf8`), covering exact Unicode/CRLF/escape ranges, incomplete source recovery, imported fragments, rich hover/navigation, trust/cancellation, stale results and read-only registration. Disposal during the final freshness await cannot publish a late result.

The isolated host suite passed on VS Code 1.141.0 and 1.142.0 with official Python
2026.10.0 and Pylance 2026.4.1. It exercised actual completion, hover and definition
commands, diagnostics, decorations, native Python hover coexistence, trust
revocation and disposal while preserving fixture disk bytes and clean buffers.
Observations are under `editor/test/observations/`; reproduce with
`npm run test:host -- 1.141.0` or `1.142.0` after the Python development environment
is installed. The runner installs pinned official extensions into an isolated
cache and uses fresh temporary profiles/workspaces. It does not alter the user's
profile. Hosted CI runs the native feature tests and dependency audit on Linux
and Windows; these manual host observations do not establish other host platforms.

## Read-only extension activation

The extension activates for Python documents and the **GraphQL Desk: Refresh
Schema** command. Install GraphQL Desk in the Python extension's selected
environment. A blank `graphqlDesk.pythonPath` uses that environment's resolved
executable for the owning workspace; a nonempty setting is an authoritative
absolute interpreter override. Invalid nonempty overrides fail closed rather than
silently selecting another interpreter. `graphqlDesk.project` supplies the existing validated
fallback configuration when no active pyproject table exists. Semantic analysis
requires workspace trust; untrusted workspaces retain lexical highlighting only.

The controller owns one worker per local workspace, enumerates Python candidates,
unions associated untitled/open buffers and captures source/configuration/schema
buffers without saving them. Watchers request early invalidation; complete capture
and worker freshness checks guard publication even when a watcher misses a change.
Only one wire job and one latest pending job are retained during edit storms.
Provider cancellation suppresses that caller; explicit refresh cancellation,
interpreter changes, trust loss and disposal terminate the relevant worker.

Remote schemas load only through the explicit refresh command. Unavailable schemas
show a status item linked to that command. Credential fingerprints stay private;
only random opaque revisions cross the protocol. Changed configuration/credentials
remove trusted schema precision, while unrelated source edits can retain explicitly
fetched schemas without HTTP. URI mapping preserves associated untitled documents
at the provider boundary. Missing buffers inherit existing parent path aliases;
the deepest canonical workspace owns analysis, and ambiguous duplicate owners
produce no semantic results.

Run `npm run test:host -- 1.141.0 activation` or `1.142.0 activation` for the actual
activation suite. Checked-in observations cover the two pinned hosts, dirty source,
configuration, fragment and schema buffers, native Pylance coexistence and explicit
remote refresh with no HTTP during typing. Source files remain unchanged on disk.
The earlier feature fixture still directly registers providers.

The controller activates and reads the official Python environment API only in a
trusted workspace. It resolves an executable rather than treating an environment
ID/directory as Python, and never guesses from PATH or default-interpreter settings.
Selected-environment changes immediately dispose affected workers/caches; explicit
overrides remain independent. Late resolution rechecks trust, selection, settings
and disposal. Both pinned activation hosts exercise a blank GraphQL override and
assert the officially selected executable before the semantic feature checks.

Full supported-platform/performance
matrices, complete discovery/lifecycle acceptance, VSIX release and live generated
typing remain open under #39/#40 and the broad gates.

Broad local-folder file watchers follow workspace-folder additions and removals.
Unchanged folders retain their watcher; removed folders release the watcher and
its listeners. Real-worker regression coverage verifies that closed source and
configuration edits in an added folder trigger refreshed analysis, and removed
watcher callbacks stop invalidating it. This bounded coverage does not establish
complete watcher discovery, supported-platform or lifecycle acceptance.
