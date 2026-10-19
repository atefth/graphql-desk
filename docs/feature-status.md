# Feature status and remaining work

Status baseline: merged production PRs #14, #15, #19, #20 and #21. Five foundation slices are built; **0 of 12 broad parity gates have passed**. Counts below refer to the 12 requirements-ledger areas, not equally sized individual features or a percentage. Research proofs are evidence, not production features.

| Area | Production status | Built evidence | Required remaining work |
|---|---|---|---|
| API-01 public API/client interoperability | Partial | Generic AST/result/variables carrier, manually annotated | Parser/exact AST declarations; graphql/init factories, helpers, generated aliases/internal exports; real sync/async/subscription client examples and compatibility fixtures |
| SRC-01 embedded Python | Partial | Conservative one-module direct-import/static-literal extraction and character call/literal spans | Constants/aliases/reexports/generated factories and fragment imports, shared project graph, decoded-character maps, incomplete buffers and live indexing |
| TYPE-01 schema/live operation types | Partial | Built-in scalar, Literal enum and recursive input declarations; snapshot/ticket stale-result guard | Operation result/variables renderer, custom mappings/oneOf/abstract/directive/fragment cases, exact literal factory overloads/runtime companions, owned writes and reliable live Pylance lifecycle |
| EDIT-01 editor experience | Not started as a production editor | Earlier narrow diagnostics/completion/hover/definition research only | Actual embedded highlighting/validation/completion/rich hover/definitions; after live typing: references/rename, outline, safe fixes, extract-to-fragment and persisted-ID actions |
| FRAG-01 fragment semantics/helpers | Not started as a production feature | Earlier basic opaque-marker and runtime-identity research only | Project composition/index and typed references, masking/read/unmask/global preview, identity-only fixture helpers and complete nullable/list/abstract/conditional matrix |
| SCALAR-01 scalar/enum/input helper | Partial | Underlying schema aliases/input declarations | Configured input/output/enum mappings and schema-bound scalar identity overloads; no response serialization or validation |
| PERSIST-01 persisted operations | Not started | Specification only | Required immediately after live gate: exact bytes/SHA256, retained/omitted typed declarations, deterministic manifests/conflicts, hash actions and source-free build/wheel verification |
| SCHEMA-01 project/schema workflows | Not started as a workflow | graphql-core substrate is installed, not a GraphQL Desk loader/config implementation | Shared validated configuration, named schemas/isolation, SDL/JSON/authenticated URL loaders, secret-safe errors, deterministic schema output and refresh/watch |
| CLI-01 commands/callables | Not started | Specification only | check/init/doctor/generate-output/generate-schema/generate-persisted/scan, shared options/defaults/errors, severity/fail-on-warn and Actions annotations; preserve init/doctor WIP |
| SCAN-01 project analysis | Not started | Specification only | Experimental project/import/document graph; all eight pinned rules and exact terminal/JSON/graph contracts; separate bounded field-use heuristic |
| PERF-01 responsiveness | Not started as measured performance work | Stale-result guard is correctness groundwork, not a benchmark | Contract small/large workloads, cold/warm latency, memory/idle/leak, incremental invalidation and supported-platform measurements |
| PACK-01 release compatibility | Partial | Locked Python package, wheel/sdist clean installs, Python3.10–3.14/OS smoke, strict checkers, dependency/license CI and required-check enforcement | Installable production VSIX/server, complete supported-editor/platform/client matrix, release/reference/security/license/docs acceptance |

Thus, at this baseline: **5 areas partial, 7 not started as production capabilities, 0 complete**. This does not mean the partial areas are equally close to completion. The [complete issue inventory](feature-tracking.md) is the traceable backlog; its per-story status is separate from these broad acceptance areas.

## Active work and observed blocker

#22 tracks operation types. #23/#24 track the actual owned-buffer adapter and isolated host harness. In real VS Code1.141.0 and1.142.0 tests, publication and disk isolation work, but restoring original text leaves the stub dirty. A resource-unbound revert command cannot safely target only the owned document; manual Save All can also persist an overlay. The adapter is not merged as production and those issues remain blocked. The new harness did not run Pylance because no prepared isolated extension environment was provided. Earlier narrow Pylance research does not settle cleanup.

## Required post-live work versus optional extras

Persisted operations and independent editor conveniences are **required** and begin immediately after live typing passes, in parallel where independent. Fragment/remaining editor, schema/CLI/project, then performance/release acceptance follow. These are not optional backlog items.

| Extra | Status / classification |
|---|---|
| Standalone `.graphql` authoring/discovery | Optional extra; not started. Embedded static Python remains required |
| Persistent turbo cache and its CLI | Optional extra; not started. Correct live inference and incremental recomputation cannot depend on it |
| Saved client/model generation | Separate optional integration/evaluation; not implemented. Turms/Ariadne research does not select a backend. Isolate tools; Ariadne0.19 cannot share core3.3 environment |
| Additional editors/unclaimed platforms | Optional evaluation only; no support promise without tests. Browser-only constraints and remote-host verification remain explicit |

APQ/server registration and a new runtime transport are not GraphQL Desk parity deliverables. Existing clients remain external; required typed-document client interoperability is tracked under API-01. See the [canonical plan](parity-plan.md) and [contracts](contracts/api-and-types.md) for exact acceptance and maturity labels.
