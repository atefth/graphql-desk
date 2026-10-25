# Feature status and remaining work

Implementation baseline: **92 foundation slices** are merged, with 447 Python tests and 175 Node tests. **0 of 12 broad parity gates have passed**. Counts below refer to the 12 requirements-ledger areas, not equally sized individual features or a percentage. Research proofs are evidence, not production features.

Merged #134–#137 preserve original-source validation and rejected resolver/index
ownership, and type introspection selections. #138 assembles captured multi-schema
artifact trees. #139 isolates semantically invalid indexed documents and their
fragment dependents while retaining independent valid declarations. Syntax/source
recovery, owned publication and full invalid-buffer lifecycle acceptance remain
open. Merged #140 supplies explicit bounded URL introspection loading. #141 adds bounded
root and generated fragment readers. #143 adds explicit trusted-worker refresh;
#144 captured-local mapping-symbol preflight; #145 bounded unsafe fixture inputs.
#146 supplies installed read-only doctor diagnostics with degraded health reporting.
#248 adds incomplete-buffer escape-aware highlighting and pinned-host source-range
checks. #251 adds provider-level lexical-to-resolved decoration recovery on both
pinned hosts and an actual activated-extension diagnostic recovery path. The scoped
#40 grammar, escape/Unicode, conservative incomplete-buffer and recovery acceptance
is complete; broader editor and live-typing requirements remain open.
#147/#148 retain explicitly refreshed schemas through safe new captures and activate
the read-only editor controller. Complete workflows, reader precision and live
generated inference remain open.

| Area | Production status | Built evidence | Required remaining work |
|---|---|---|---|
| API-01 public API/client interoperability | Partial | Generic AST/result/variables carrier; runtime parse and untyped factories; exact executable literal AST declarations and editor-generated per-source parser companions with ordinary fallback; identity fragment references; pure retained-source factory artifact trees with nominal references, exact AST views and companion aliases; real sync/async/subscription client fixtures | Automatic exact parser typing for normal imports (#253), complete factory inference/source diagnostics, generated CLI publication/internal exports, complete helper/overload integration and supported-client matrix |
| SRC-01 embedded Python | Partial | Conservative one-module extractor; explicit-snapshot module graph with factory/schema metadata, ordered fragment references and versions; exact decoded source maps; finite candidate source selection; read-only finite snapshot acquisition and JSON analysis bridge; incremental parsing/unchanged-project reuse and stable disk-source acquisition identities; explicit analyzer reuse in a bounded read-only worker | Complete candidate enumeration/worker invalidation acceptance, full scope integration, safe edit consumers, incomplete-buffer coverage and live indexing |
| TYPE-01 schema/live operation types | Partial | Built-in scalar, Literal enum and recursive input declarations; operation renderer with abstract/conditional alternatives, directives, unmasked and masked indexed fragments, mapped aliases and closed oneOf inputs; pure literal factory artifacts with source companions/nominal references/exact AST views; snapshot/ticket stale-result guard; managed Pylance publication passes selected VS Code 1.141/1.142 host flows for results, variables, nullability and cross-file fragments; Pylance fixtures verify interface/union narrowing with canonical and aliased typename, optional type aids, conditional/nullability regeneration after unsaved edits; PR #259 verifies external scalar/enum mappings in the selected interpreter and Pylance | Broad Python type and source diagnostics matrix, cancellation/interpreter changes, crash recovery, performance and the full live Pylance acceptance matrix |
| EDIT-01 editor experience | Partial | Pure snapshot bridge and read-only diagnostics/completion/hover/definition/highlighting providers; isolated real VS Code1.141.0/1.142.0 provider/discovery host fixtures; local schema-path preflight and bounded Python worker/Node connection; trusted production activation/controller with captured dirty buffers, explicit remote refresh and final inventory/freshness checks; official selected-environment resolution with scoped worker invalidation | Complete acquisition and incremental freshness acceptance; after live typing: references/rename, outline, safe fixes, extract-to-fragment and persisted-ID actions |
| FRAG-01 fragment semantics/helpers | Partial | Runtime explicit/transitive composition; finite versioned index with ordered closure and schema/context validation; unmasked/masked indexed operation rendering; identity-safe nominal references/document views/keys; fragment declaration rendering; readonly reader inputs and bound/root identity readers; bounded unsafe fixture inputs and identity helpers; separate identity-preserving passthrough helper; Pylance verifies cross-file conditional fragment absence/presence and key-guarded reader narrowing | Complete reader precision, mask_fragments/read_result helpers, complete generated/editor/configuration/persisted/scan integration, fixture helpers and full nullable/list/abstract/conditional acceptance matrix |
| SCALAR-01 scalar/enum/input helper | Partial | Underlying schema aliases/input declarations, directional scalar/enum mappings including built-in overrides, root/runtime scalar identity members and generated schema-bound input overloads; captured-local symbol preflight including bounded explicit named/container/union/Literal/Annotated TypeAlias and NewType exports; doctor resolution of local `.py`/`.pyi` mapping names through reachable static imports within configured roots; PR #259 verifies external scalar/enum declarations and selected-environment editor typing | Broader Python type compatibility, checker-backed matrix beyond covered aliases, complete editor/CLI/project integration and full helper matrix; no response serialization or validation |
| PERSIST-01 persisted operations | Not started | Specification only | Required immediately after live gate: exact bytes/SHA256, retained/omitted typed declarations, deterministic manifests/conflicts, hash actions and source-free build/wheel verification |
| SCHEMA-01 project/schema workflows | Partial | Validated explicit SDL/JSON loaders and immutable single/named-schema facts; nearest-project search, schema routing, output collision checks, effective fallback/header resolution, finite source eligibility, explicit CLI external-root scope backend, finite editor snapshot acquisition and local schema-path preflight; explicit bounded URL introspection loader | Complete source enumeration/incremental freshness acceptance and CLI dispatch, complete named-schema isolation, authenticated-loader/symbol integration, complete URL refresh and credential freshness acceptance, deterministic schema output and refresh/watch |
| CLI-01 commands/callables | Partial | Installed read-only doctor and module entrypoint; shared config/schema loaders, sanitized errors, source-root-limited static local mapping-name reports and explicit degraded status for unverified external compatibility/editor/bridge/ownership | check/init/complete doctor/generate-output/generate-schema/generate-persisted/scan, shared options/defaults/errors, severity/fail-on-warn and Actions annotations; preserve init/doctor WIP |
| SCAN-01 project analysis | Not started | Specification only | Experimental project/import/document graph; all eight pinned rules and exact terminal/JSON/graph contracts; separate bounded field-use heuristic |
| PERF-01 responsiveness | Not started as measured performance work | Stale-result guards and incremental source-cache correctness are groundwork, not benchmarks | Contract small/large workloads, cold/warm latency, memory/idle/leak, incremental invalidation and supported-platform measurements |
| PACK-01 release compatibility | Partial | Locked Python package, wheel/sdist clean installs, Python3.10–3.14/OS smoke, strict checkers, dependency/license CI, required-check enforcement and two-host read-only feature fixtures | Installable production VSIX/server, complete supported-editor/platform/client matrix, release/reference/security/license/docs acceptance |

Thus, at this baseline: **9 areas partial, 3 not started as production capabilities, 0 complete**. This does not mean the partial areas are equally close to completion. The [complete issue inventory](feature-tracking.md) is the traceable backlog; its per-story status is separate from these broad acceptance areas.

## Active work and observed blocker

#27 is closed on verified mapped acceptance of the shared configuration/schema-routing foundation, source eligibility and finite read-only snapshot acquisition. Complete CLI dispatch/overrides remain under #60; production enumeration/incremental freshness acceptance, authenticated refresh integration and symbol integration also remain open. #28 is closed on verified bounded source-graph acceptance; it supplies stable module/factory/schema facts and versioned fragment references. #29 exact maps and #34 fragment indexing now have read-only provider consumers. They do not establish safe refactoring or complete project discovery. #30 mapping declarations, #31 closed oneOf inputs and #133 scalar members/overloads have focused fixtures; broader integration remains separate.

Merged #111 client fixtures preserve native sync/async/subscription behavior. #112 identity references, #113 runtime primary-fragment `@_unmask` stripping, #115 masked rendering, #116 fragment declarations and #119 exact literal AST declarations extend the static foundations. #120 adds lazy generated imports; #121 hardens captured configuration text inputs. #123 adds pure retained-source factory artifact trees, nominal references, composed AST views and companion aliases. Merged #130 shares the executable validation policy; #131 replaces the unsafe Any fallback with read-only AST metadata. Incorrect known constructor calls can still select that opaque view, so complete inference and source diagnostics remain open. #132 intersects active repeated-field nullability and #133 adds scalar identity members, generated input-type overloads and configured built-in overrides. Mask/result helper functions, complete reader precision, dynamic parse overloads, complete generated configuration and live inference remain unimplemented or incomplete. Pinned TypeScript comparison corrected stronger reader-probe expectations: structurally compatible raw fields are accepted, unmatched references pass through and mutable object aliasing remains an upstream limitation. Return types and fragment-only access, including empty/conditional/abstract/nullable/list cases, remain the acceptance target; bounded production readers are merged in #141, and #189 adds the separate identity-preserving `read_fragment_passthrough` helper while exact `read_fragment` inference remains intact. `read_fragment(document, value)` narrows mapped abstract nullable-item lists; the generated one-argument convenience helper conservatively returns the full abstract result for that shape because branch-specific nullable overloads overlap in both Pyright and mypy. That helper precision and the broader nullable/list reader matrix remain open under #52.

Merged #114/#117/#118 provide the pure analysis bridge, finite read-only acquisition and GLS providers. The provider host suite passed in VS Code1.141.0 and1.142.0 with pinned Python/Pylance extensions, including native Python coexistence and unchanged source files. It directly registers providers; the bounded Python worker/Node connection is separate from production extension activation, a discovery controller and automatic analysis, now supplied by #148; complete lifecycle/discovery acceptance remains open. #122 records successful two-host discovery fixtures, including explicit unsaved-buffer union and symlink checks; it does not prove watcher completeness or full enumeration.

Merged #124 adds incremental parsing and reuse of unchanged project semantic results; #125 retains exact unchanged disk-source versions across acquisition generations. Counter and cold-result comparisons establish bounded cache correctness, not measured PERF-01 acceptance. Merged #126 adds local schema-path preflight, #127 explicit analyzer reuse, #128 a read-only Python worker and #129 its bounded Node connection. The connection itself provides no activation; #148 supplies read-only activation and automatic finite capture/analysis. The managed artifact adapter now connects activation to the worker, but VSIX release, broad lifecycle acceptance and performance evidence remain open. Schemas and fragment indexes still rebuild.

Merged #143/#147 add explicit remote refresh and safe retention across source captures;
#148 activates the trusted read-only controller with one active/one pending job,
private credential fingerprints and explicit interpreter selection. Final two-host
activation observations cover dirty source/configuration/fragment/schema buffers,
associated untitled documents, native Pylance coexistence, explicit refresh and zero
HTTP during typing. #144 preflights captured-local mapping symbols without imports;
external and richer type facts remain unverified. #145 adds bounded unsafe fixture
input typing and identity helpers. #146 adds read-only doctor entrypoints that
report degraded health rather than invent editor/bridge/ownership evidence. #249
adds import-free resolution of configured local mapping names from `.py` and `.pyi`
files reachable under configured source roots, and reports the results from doctor.
It does not scan unrelated files or impose a numeric file cap; unusually large
reachable import closures can make doctor slow. External installed-package symbol
verification and checker-level type compatibility remain open.
One checker fixture now verifies mapped input/output/enum compatibility against an
external stub-only package with both mypy and Pyright. This is bounded evidence;
doctor still reports external symbols as unverified and runtime exports are not
validated.
These foundations do not complete the dependency-gated later-stage stories.

Merged #150 adds simple captured PEP613 mapping aliases. #151 generates opt-in
parser companions with exact default-mode executable literals and ordinary fallback
for dynamic strings, Source objects, SDL and nondefault modes. Captured parser discovery and owner-specific opt-in companions now reuse static
source facts; automatic import attachment remains open under #253. The controller now resolves
the Python extension's selected executable when its override is blank; selection
changes invalidate affected workers while explicit overrides remain independent.
Both pinned hosts verify the selected executable with a blank GraphQL override.
No PATH/default-setting guess or new dependency is introduced.

#23/#24 are closed on verified bounded acceptance: pinned VS Code 1.141/1.142 host tests verify Pylance result/variable inference, schema nullability refresh, workspace stubPath migration, and owned-package cleanup on host shutdown while preserving unrelated settings/files. #38 and acceptance gate #42 remain open for broader live typing and lifecycle acceptance. The user has authorized managed workspace-local generated stubs. The production adapter publishes real multi-file worker output. Earlier in-memory overlay restoration left the stub dirty and Save All could persist it; that route is not used. The earlier unchanged-disk policy is superseded for owned generated analysis files. Preserve every pre-existing file and dirty user buffer. These bounded results do not pass the broad live gate.

## Required post-live work versus optional extras

Persisted operations and independent editor conveniences are **required** and begin immediately after live typing passes, in parallel where independent. Fragment/remaining editor, schema/CLI/project, then performance/release acceptance follow. These are not optional backlog items.

| Extra | Status / classification |
|---|---|
| Standalone `.graphql` authoring/discovery | Optional extra; not started. Embedded static Python remains required |
| Persistent turbo cache and its CLI | Optional extra; not started. Correct live inference and incremental recomputation cannot depend on it |
| Saved client/model generation | Separate optional integration/evaluation; not implemented. Turms/Ariadne research does not select a backend. Isolate tools; Ariadne0.19 cannot share core3.3 environment |
| Additional editors/unclaimed platforms | Optional evaluation only; no support promise without tests. Browser-only constraints and remote-host verification remain explicit |

APQ/server registration and a new runtime transport are not GraphQL Desk parity deliverables. Existing clients remain external; required typed-document client interoperability is tracked under API-01. See the [canonical plan](parity-plan.md) and [contracts](contracts/api-and-types.md) for exact acceptance and maturity labels.

The doctor mapping-report foundation checks loaded schema declarations through the
existing renderer and names explicit Any scalar input/output and enum mappings.
Missing or incompatible mappings produce a redacted unavailable report. Imported
symbol compatibility, generated ownership and active editor/bridge health remain
unverified, so doctor still exits degraded. This does not start or complete the
dependency-gated full doctor/CLI acceptance stories.

Workspace-folder watcher registration now follows local folder additions and
removals. A real-worker regression verifies added-folder closed source/configuration
edits refresh diagnostics, retained folders avoid duplicate watchers, and removed
listeners stop invalidating analysis. This does not complete discovery/lifecycle or
supported-platform acceptance.

Captured mapping preflight now preserves inherited TypedDict provenance through
aliases and reexports, allowing proven inherited total keywords. Mixed ordinary
bases and unsupported inherited-only closed semantics remain unresolved. Existing
actual-source factory fixtures cover both strict checkers and runtime behavior;
full Python type compatibility and external-symbol verification remain open.

Local schema watchers now belong to each worker state and retire with it. Successful
preparation reconciles changed schema paths; missing paths remain repair targets,
and failed preparation retains previous observation. External dirty-schema and
retired-callback regressions add bounded lifecycle evidence, leaving complete
discovery/lifecycle and live generated-typing acceptance open.

Captured parser syntax diagnostics now reuse exact versioned contribution maps,
including imported constants, escaped literals and legitimate EOF origins. Each
call keeps its own source locations despite deduplicated parsing; unverifiable
provenance and resource failures retain honest call-level fallbacks. Automatic
import attachment and automatic typing for ordinary `graphql_desk.parse` imports remain
open under #253.

Static authoring now admits direct ordinary/async function-body calls using literals
or proven stable module globals. Whole-function local binding checks prevent
shadowed inputs from being replaced with module globals; unsupported scopes and functions with inline type
parameters remain conservative. Actual-source generated factory/parser fixtures cover
both strict checkers and runtime identity. Complete static-source, automatic
attachment and live typing acceptance remain open.

Managed output is now wired into extension activation and the generated-artifact
worker. The manager publishes guarded multi-file package updates under the active
workspace-local stubPath and removes its owned package at extension shutdown.
Both pinned Windows hosts verify result/variable inference, schema-nullability
refresh, runtime stubPath migration and cleanup with unrelated settings/files
preserved. #23/#24 are closed on this bounded acceptance; restart recovery, the
full lifecycle matrix and broad typing acceptance remain unfinished in #38/#42
and the broader gates. See
[owned analysis stubs](implementation/owned-analysis-stubs.md).
