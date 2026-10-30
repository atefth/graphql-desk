# Embedded editor providers and code actions

`editor/src/languageFeatures.js` exposes `createLanguageFeatures({analyze, isCurrent, isTrusted, clientDirectives?})`. Its `complete`, `hover`, `definition`, `diagnostics` and `codeActions` methods accept a plain `{uri, version, text}` Python document snapshot; cursor offsets and returned spans use UTF-16. The injected `analyze(snapshot, cancellationToken)` returns `{request, response}` from the [snapshot analysis boundary](editor-analysis.md), retaining the original schema text. No process, configuration discovery or production analysis adapter is started here.

`isCurrent(bundle, snapshot, cancellationToken)` is mandatory. The caller must compare all contributing source/config versions, fragment dependencies and schema identities/tokens with current state. Providers also check the local document snapshot and response schema facts, and recheck trust, cancellation and freshness before returning. A failed, stale, cancelled or ambiguous analysis returns no precise result. Failures do not replace the native Python service. The injected callback is an internal trusted analysis boundary; this module does not independently validate arbitrary malformed response JSON.

The pinned lower-level `graphql-language-service==5.7.0` and `graphql==16.12.0` supply online completion/hover and validation primitives. Validation uses separately parsed raw source ASTs with their original locations, rather than concatenating printed fragments or compensating for guessed range endpoints. Diagnostics retain separate syntax, validation, deprecation and source/index codes, with identical results deduplicated. Configured client directive names may be supplied through `clientDirectives(bundle, call)`; undeclared pinned client names are suppressed without bypassing validation of schema-declared directives.

Incomplete GraphQL inside valid Python still has real maps and can complete. Completion inserts a plain GraphQL name/value label where one linear current-file map span supplies an unambiguous replacement, or inserts at a uniquely mapped zero-width boundary with no matching linear occurrence. The latter preserves escaped whitespace and physical CRLF bytes while allowing the existing language-service suggestions. Atomic escape interiors, ranges crossing literal gaps and repeated/shared origins remain rejected, including repeated empty constants whose identical origins deduplicate. It does not rewrite Python escaping, insert snippets, plan refactors or claim project-wide edit safety. Native hover signatures/descriptions/deprecation are retained; input-member qualification is corrected from actual input type metadata, and actual argument/input defaults are shown. Hover Markdown is untrusted and HTML-disabled.

Definitions cover schema fields, arguments, directives, named types, enum/input members, local variables and explicit indexed fragments. They use original SDL/node locations and raw call maps; printed definition offsets are never treated as authored positions. Fragment spreads require one matching authored target: duplicate local declarations or conflicting imported definitions cannot select an arbitrary first target. Their diagnostics and unrelated schema navigation remain available, and repairing the conflict restores fragment navigation. Introspection schemas lack authored SDL definitions, so no schema location is invented. Definition traversal requires parseable GraphQL. Missing or unresolved fragment facts do not become trusted navigation targets.

Persisted declarations are checked with the pinned editor hash after resolving their operation and transitive fragments. Only `sha256:` IDs receive stale-hash diagnostics. A direct string-literal ID gets a code action with an exact replacement range and Python-escaped string; a statically resolved alias is diagnosed but never rewritten. An omitted ID gets an insertion action for an explicit `document=` keyword or a sole positional argument that statically resolves to a GraphQL Desk document. The positional fix prepends the ID only after proving the argument's document identity; unresolved or malformed calls remain diagnostic-only.

Validation quick fixes offer one replacement per plain GraphQL `Did you mean` candidate and skip inline-fragment suggestions. Deprecated fields are replaced only when their reason explicitly names a simple field and that field exists on the current parent type without being deprecated itself. Both actions require a matching current diagnostic and one exact, linear Python source-map range; escaped, stale or ambiguous ranges get no edit. VS Code applies edits only after the user selects a quick fix.

`registerLanguageFeatures(vscode, options)` adds supplemental Python completion/hover/definition/document-symbol providers, validation/deprecation/persisted-ID code actions, and a separate diagnostic collection. Document symbols expose named operations and fragments with exact embedded source ranges; when a trailing GraphQL definition is incomplete, complete earlier definitions remain available. The returned `refresh(document, token)`, `close(uri)` and `dispose()` functions are lifecycle hooks for the activation owner. Per-document generations prevent an older diagnostic refresh from overwriting a newer one. Analysis does not edit or save source/generated buffers; VS Code applies an edit only when the user chooses the code action.

`editor/src/highlighting.js` exposes `createHighlighting(options)` and `registerHighlighting(vscode, options)`. Resolved GraphQL tokens project through actual maps into literal spans. Existing theme colors are applied as confined decorations, without replacing Python semantic-token providers. In incomplete Python, or an untrusted workspace, a separate lexical candidate mode recognizes a limited set of direct imported constructors, including top-level parenthesized `from` imports. It decodes simple, octal, hex, fixed-width Unicode escapes and escaped line continuations, mapping each token back over its exact Python source span. Unsupported, named, malformed and invalid-code-point escapes are skipped; interpolated and byte strings are also excluded. The fallback never supplies diagnostics, navigation, types or trusted source facts, and untrusted mode never calls analysis. It rejects constructor-name suffixes inside longer Unicode identifiers or spaced attribute calls and is not complete Python name resolution. The scoped #40 grammar, escape/Unicode, conservative incomplete-buffer and recovery acceptance is complete; broader editor parity remains open. Trusted incomplete-Python captures also recognize exact configured generated/template factory imports and qualified aliases from the uniquely matching snapshot project. Stale captures, foreign/ambiguous owners, recognized conflicting imports and assignments to callable namespace prefixes cannot supply configured candidates. Untrusted mode retains only the existing root constructors and never uses project bindings.

Native Node tests run the real Python snapshot analyzer with isolated UTF-8 JSON transport (`-I -X utf8`), covering exact Unicode inside GraphQL string values, CRLF and Python-escape ranges, incomplete source recovery, imported fragments, rich hover/navigation, trust/cancellation, stale results and read-only registration. Disposal during the final freshness await cannot publish a late result.

The lexer regression matrix also covers query, mutation and subscription definitions; variable and nested input types; negative exponent numbers; object/list defaults; block-string values; directives, fragment spreads and incomplete fragment headers. Separate fixtures cover Unicode/enum scopes and decoded Python escape ranges.

Field hover includes schema argument names, complete types and GraphQL-printed defaults, preserving the language service's descriptions and deprecation text. Focused analyzer fixtures cover aliased and incomplete fields, SDL and introspection schemas, introspection fields, and null/boolean/numeric/string/list/input-object defaults. No-argument fields keep their existing signature. Custom scalar defaults that GraphQL cannot print retain the original language-service hover; custom scalar serialization is not supplied by this adapter.

The isolated host suite passed on VS Code 1.141.0 and 1.142.0 with official Python
2026.10.0 and Pylance 2026.4.1. It exercised actual completion, hover and definition
commands, diagnostics, decorations, native Python hover coexistence, trust
revocation and disposal while preserving fixture disk bytes and clean buffers.
Both pinned hosts also assert that lexical decorations for an escaped, incomplete
Python string cover the exact VS Code ranges for its GraphQL keyword and string token.
They also exercise one dirty-buffer recovery cycle: incomplete Python receives only
lexical highlights, repaired Python exposes mapped GraphQL validation/highlighting,
and a corrected field clears validation without changing the source file on disk.
The feature host calls the registered providers directly for exact decoration
ranges. A separate real activated-extension host observes the diagnostic transition
through its document-change path; it does not inspect decorations. The edited test
buffer intentionally remains dirty.
The feature suites also verify an exact empty insertion range on a blank CRLF
GraphQL line with preceding Unicode through the registered completion provider.
They also reject arbitrary navigation for duplicate local and conflicting imported
fragment names while retaining unique fragment targets, schema navigation and
conflict diagnostics.
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

Remote schemas load through explicit refresh by default; a positive configured
interval enables trusted background polling. Unavailable schemas
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

Local-schema watchers belong to their worker state. Interpreter replacement,
workspace removal and disposal release those watchers and listeners. Successful
preparation reconciles the required schema paths, including missing paths needed
for repair; failed preparation keeps the previous observation set. Real-worker
coverage checks external dirty-schema authority, configuration path replacement,
missing-file repair and inert retired callbacks. This is bounded resource-lifecycle
coverage, with complete lifecycle acceptance still open.

Remote schemas remain explicit-refresh-only by default. A positive validated
`schema_refresh_interval` enables authenticated background polling after trusted
preparation. One state-owned timer preserves per-target deadlines across source-only
captures, waits for interactive work, and reuses the existing bounded worker queue.
Long intervals use safe platform timer chunks. Fresh preparation rechecks the target
and interval before HTTP; credentials resolve from the current environment, and all
capture/credential/trust/cancellation guards still apply before publication. Explicit
refresh takes priority, including while its schema picker is open. Successful polls
refresh providers without repeated command notifications; failed schemas retain the
existing unavailable-state behavior. Invalidations abort polling; interpreter changes,
workspace removal and disposal retire state-owned timers. This bounded polling and
lifecycle coverage does not pass complete schema/editor or live-typing acceptance.
