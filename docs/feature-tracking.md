# Complete feature and issue tracking

This inventory maps every capability in the canonical contracts to issue-linked stories. Full parity is still **0 of 12 acceptance gates passed**; a closed foundation or research issue does not close a broad gate. The [feature status](feature-status.md) table separates delivered primitives from remaining production capabilities.

The private project uses matching **Stage**, **Scope** and **Status** fields and seven undated milestones (five delivery stages, optional extras and cross-stage full-parity acceptance). Todo is planned, In Progress is active, Blocked has an observed acceptance blocker, and Done closes only the stated story. The epic stays open. Live issue states and board fields are authoritative; this inventory avoids duplicating per-story status that would become stale on each merge. Optional extras and separate integrations never block required parity.

## Inventory

| Issue | Capability / story | Stage | Classification | Ledger |
|---|---|---|---|---|
| [#1](https://github.com/atefth/graphql-desk/issues/1) | Define the complete gql.tada parity contract for Python | Cross-stage tracking | Required parity | All 12 gates |
| [#2](https://github.com/atefth/graphql-desk/issues/2) | Spike: VS Code LSP schema-aware editing (basic slice passed) | 1 Live typing | Planning and evidence | Research/planning only |
| [#3](https://github.com/atefth/graphql-desk/issues/3) | Spike: live Pyright/Pylance update bridge (narrow case passed) | 1 Live typing | Planning and evidence | Research/planning only |
| [#4](https://github.com/atefth/graphql-desk/issues/4) | Spike: compare Ariadne Codegen and Turms (comparison complete) | Optional extras | Planning and evidence | Research/planning only |
| [#5](https://github.com/atefth/graphql-desk/issues/5) | Spike: verify type-only Python fragment masking (minimal proof passed) | 1 Live typing | Planning and evidence | Research/planning only |
| [#6](https://github.com/atefth/graphql-desk/issues/6) | Define the full-parity support matrix and production review workflow | 5 Performance and release | Planning and evidence | Research/planning only |
| [#11](https://github.com/atefth/graphql-desk/issues/11) | Live typing: establish typed-document package and first production CI | 1 Live typing | Required parity | API-01 / PACK-01 |
| [#12](https://github.com/atefth/graphql-desk/issues/12) | Capture omitted local research constraints and contributor safeguards | 1 Live typing | Planning and evidence | Research/planning only |
| [#16](https://github.com/atefth/graphql-desk/issues/16) | Render schema input, scalar and enum declarations | 1 Live typing | Required parity | TYPE-01 / SCALAR-01 |
| [#17](https://github.com/atefth/graphql-desk/issues/17) | Extract statically embedded Python GraphQL call candidates | 1 Live typing | Required parity | SRC-01 |
| [#18](https://github.com/atefth/graphql-desk/issues/18) | Guard live-generation publication against stale snapshots | 1 Live typing | Required parity | TYPE-01 |
| [#22](https://github.com/atefth/graphql-desk/issues/22) | Render operation result and variable declarations | 1 Live typing | Required parity | TYPE-01 |
| [#23](https://github.com/atefth/graphql-desk/issues/23) | Adapt owned generated stubs to live VS Code buffers safely | 1 Live typing | Required parity | TYPE-01 |
| [#24](https://github.com/atefth/graphql-desk/issues/24) | Test live stub refresh and cleanup in isolated editor hosts | 1 Live typing | Required parity | TYPE-01 |
| [#25](https://github.com/atefth/graphql-desk/issues/25) | Exact parser AST declarations and runtime parse export | 1 Live typing | Required parity | API-01 |
| [#26](https://github.com/atefth/graphql-desk/issues/26) | Runtime graphql and schema-bound factory construction | 1 Live typing | Required parity | API-01 |
| [#27](https://github.com/atefth/graphql-desk/issues/27) | Shared project configuration and named-schema routing | 1 Live typing | Required parity | SCHEMA-01 |
| [#28](https://github.com/atefth/graphql-desk/issues/28) | Resolve Python constants imports reexports and fragment bindings | 1 Live typing | Required parity | SRC-01 |
| [#29](https://github.com/atefth/graphql-desk/issues/29) | Map decoded GraphQL characters back to Python source spans | 1 Live typing | Required parity | SRC-01 |
| [#30](https://github.com/atefth/graphql-desk/issues/30) | Render configured scalar and enum input/output mappings | 1 Live typing | Required parity | SCALAR-01 |
| [#31](https://github.com/atefth/graphql-desk/issues/31) | Render oneOf input alternatives with source validation | 1 Live typing | Required parity | TYPE-01 |
| [#32](https://github.com/atefth/graphql-desk/issues/32) | Render union interface and typename operation results | 1 Live typing | Required parity | TYPE-01 |
| [#33](https://github.com/atefth/graphql-desk/issues/33) | Render conditional defer and nullability directive semantics | 1 Live typing | Required parity | TYPE-01 |
| [#34](https://github.com/atefth/graphql-desk/issues/34) | Index and compose transitive operation fragment graphs | 1 Live typing | Required parity | FRAG-01 |
| [#35](https://github.com/atefth/graphql-desk/issues/35) | Render fragment result aliases and opaque masked references | 1 Live typing | Required parity | TYPE-01 |
| [#36](https://github.com/atefth/graphql-desk/issues/36) | Generate literal factory overloads and source companion aliases | 1 Live typing | Required parity | TYPE-01 |
| [#37](https://github.com/atefth/graphql-desk/issues/37) | Write owned generated pairs atomically with recovery manifest | 1 Live typing | Required parity | TYPE-01 |
| [#38](https://github.com/atefth/graphql-desk/issues/38) | Connect versioned source generation worker to editor adapter | 1 Live typing | Required parity | TYPE-01 |
| [#39](https://github.com/atefth/graphql-desk/issues/39) | Serve embedded GraphQL diagnostics completion hover and definitions | 1 Live typing | Required parity | EDIT-01 |
| [#40](https://github.com/atefth/graphql-desk/issues/40) | Highlight embedded GraphQL and conservatively handle incomplete Python | 1 Live typing | Required parity | EDIT-01 |
| [#41](https://github.com/atefth/graphql-desk/issues/41) | Prove generic AST client query mutation and subscription interop | 1 Live typing | Required parity | API-01 |
| [#42](https://github.com/atefth/graphql-desk/issues/42) | Verify the complete live-typing milestone acceptance matrix | 1 Live typing | Required parity | TYPE-01 |
| [#43](https://github.com/atefth/graphql-desk/issues/43) | Canonicalize persisted document bytes and SHA256 identities | 2 Persisted and conveniences | Required parity | PERSIST-01 |
| [#44](https://github.com/atefth/graphql-desk/issues/44) | Implement typed persisted declarations and generated ID-only bindings | 2 Persisted and conveniences | Required parity | PERSIST-01 |
| [#45](https://github.com/atefth/graphql-desk/issues/45) | Generate deterministic persisted manifests and conflict reports | 2 Persisted and conveniences | Required parity | PERSIST-01 |
| [#46](https://github.com/atefth/graphql-desk/issues/46) | Build source-free persisted release projections and inspect wheels | 2 Persisted and conveniences | Required parity | PERSIST-01 |
| [#47](https://github.com/atefth/graphql-desk/issues/47) | Diagnose stale persisted IDs and offer insert/hash refactors | 2 Persisted and conveniences | Required parity | EDIT-01 |
| [#48](https://github.com/atefth/graphql-desk/issues/48) | Find and rename fragment symbols across Python project files | 2 Persisted and conveniences | Required parity | EDIT-01 |
| [#49](https://github.com/atefth/graphql-desk/issues/49) | Provide operation and fragment document outline symbols | 2 Persisted and conveniences | Required parity | EDIT-01 |
| [#50](https://github.com/atefth/graphql-desk/issues/50) | Offer safe validation suggestion and deprecation quick fixes | 2 Persisted and conveniences | Required parity | EDIT-01 |
| [#51](https://github.com/atefth/graphql-desk/issues/51) | Extract complete sibling selections into colocated fragments | 2 Persisted and conveniences | Required parity | EDIT-01 |
| [#52](https://github.com/atefth/graphql-desk/issues/52) | Complete read_fragment scalar nullable and list identity helpers | 3 Fragments and remaining editor | Required parity | FRAG-01 |
| [#53](https://github.com/atefth/graphql-desk/issues/53) | Implement per-fragment unmask and global disable_masking | 3 Fragments and remaining editor | Required parity | FRAG-01 |
| [#54](https://github.com/atefth/graphql-desk/issues/54) | Implement mask_fragments read_result and unsafe_read_result helpers | 3 Fragments and remaining editor | Required parity | FRAG-01 |
| [#55](https://github.com/atefth/graphql-desk/issues/55) | Warn on imported colocated fragments unused in a module | 3 Fragments and remaining editor | Required parity | EDIT-01 |
| [#56](https://github.com/atefth/graphql-desk/issues/56) | Track bounded same-file Python result field consumption | 3 Fragments and remaining editor | Required parity | EDIT-01 |
| [#57](https://github.com/atefth/graphql-desk/issues/57) | Safely remove selections flagged by field-use diagnostics | 3 Fragments and remaining editor | Required parity | EDIT-01 |
| [#58](https://github.com/atefth/graphql-desk/issues/58) | Load SDL and introspection schemas through shared internal APIs | 1 Live typing | Required parity | SCHEMA-01 |
| [#59](https://github.com/atefth/graphql-desk/issues/59) | Fetch authenticated schemas with secret-safe refresh and errors | 4 Schema CLI and project | Required parity | SCHEMA-01 |
| [#60](https://github.com/atefth/graphql-desk/issues/60) | Expose CLI and callable check with severity and exit policies | 4 Schema CLI and project | Required parity | CLI-01 |
| [#61](https://github.com/atefth/graphql-desk/issues/61) | Emit escaped deduplicated GitHub Actions diagnostics | 4 Schema CLI and project | Required parity | CLI-01 |
| [#62](https://github.com/atefth/graphql-desk/issues/62) | Expose schema factory output generation in CLI and callable API | 4 Schema CLI and project | Required parity | CLI-01 |
| [#63](https://github.com/atefth/graphql-desk/issues/63) | Expose deterministic schema download and conversion command | 4 Schema CLI and project | Required parity | CLI-01 |
| [#64](https://github.com/atefth/graphql-desk/issues/64) | Implement conflict-safe project init workflow | 4 Schema CLI and project | Required parity | CLI-01 |
| [#65](https://github.com/atefth/graphql-desk/issues/65) | Diagnose project schema interpreter checker and bridge health | 4 Schema CLI and project | Required parity | CLI-01 |
| [#66](https://github.com/atefth/graphql-desk/issues/66) | Build schema-qualified project scan document and module graph | 4 Schema CLI and project | Required parity | SCAN-01 |
| [#67](https://github.com/atefth/graphql-desk/issues/67) | Implement scan field-usage and schema reach coverage rule | 4 Schema CLI and project | Required parity | SCAN-01 |
| [#68](https://github.com/atefth/graphql-desk/issues/68) | Implement scan input-field and enum-value usage rule | 4 Schema CLI and project | Required parity | SCAN-01 |
| [#69](https://github.com/atefth/graphql-desk/issues/69) | Implement scan deprecated-usage operation reach rule | 4 Schema CLI and project | Required parity | SCAN-01 |
| [#70](https://github.com/atefth/graphql-desk/issues/70) | Implement scan orphan-fragment direct-spread rule | 4 Schema CLI and project | Required parity | SCAN-01 |
| [#71](https://github.com/atefth/graphql-desk/issues/71) | Implement scan cross-feature fragment usage rule | 4 Schema CLI and project | Required parity | SCAN-01 |
| [#72](https://github.com/atefth/graphql-desk/issues/72) | Implement scan operation complexity and effective depth rule | 4 Schema CLI and project | Required parity | SCAN-01 |
| [#73](https://github.com/atefth/graphql-desk/issues/73) | Implement scan query fetch-depth import distance rule | 4 Schema CLI and project | Required parity | SCAN-01 |
| [#74](https://github.com/atefth/graphql-desk/issues/74) | Implement scan syntactic directive usage rule | 4 Schema CLI and project | Required parity | SCAN-01 |
| [#75](https://github.com/atefth/graphql-desk/issues/75) | Emit experimental scan terminal JSON and graph reports | 4 Schema CLI and project | Required parity | SCAN-01 |
| [#76](https://github.com/atefth/graphql-desk/issues/76) | Publish and verify all advanced/internal package utilities | 4 Schema CLI and project | Required parity | API-01 |
| [#77](https://github.com/atefth/graphql-desk/issues/77) | Measure and meet project-scale incremental latency budgets | 5 Performance and release | Required parity | PERF-01 |
| [#78](https://github.com/atefth/graphql-desk/issues/78) | Package installable VSIX server and Python release artifacts | 5 Performance and release | Required parity | PACK-01 |
| [#79](https://github.com/atefth/graphql-desk/issues/79) | Verify release dependency integrity licenses and workspace trust | 5 Performance and release | Required parity | PACK-01 |
| [#80](https://github.com/atefth/graphql-desk/issues/80) | Run pinned gql.tada and GraphQLSP conformance acceptance corpus | 5 Performance and release | Required parity | PACK-01 |
| [#81](https://github.com/atefth/graphql-desk/issues/81) | Publish worked examples and complete user/API compatibility docs | 5 Performance and release | Required parity | PACK-01 |
| [#82](https://github.com/atefth/graphql-desk/issues/82) | Optional standalone .graphql authoring and discovery | Optional extras | Optional extra | Optional |
| [#83](https://github.com/atefth/graphql-desk/issues/83) | Optional persistent turbo type cache and invalidation | Optional extras | Optional extra | Optional |
| [#84](https://github.com/atefth/graphql-desk/issues/84) | Optional turbo CLI and cache doctor integration | Optional extras | Optional extra | Optional |
| [#85](https://github.com/atefth/graphql-desk/issues/85) | Separate optional saved client/model generator evaluation | Optional extras | Separate optional integration | Separate |
| [#86](https://github.com/atefth/graphql-desk/issues/86) | Optional other-editor and browser/remote compatibility evaluation | Optional extras | Optional extra | Optional |
| [#87](https://github.com/atefth/graphql-desk/issues/87) | Implement schema-bound scalar enum and input identity helpers | 1 Live typing | Required parity | SCALAR-01 |
| [#88](https://github.com/atefth/graphql-desk/issues/88) | Reconcile every parity capability with staged GitHub tracking | Cross-stage tracking | Planning and evidence | Research/planning only |
| [#253](https://github.com/atefth/graphql-desk/issues/253) | Attach exact parser types to normal parse imports | 1 Live typing | Required parity | API-01 |

## Coverage and counting rules

- All 12 ledger areas map to the stories above: API/parser/runtime/client/internal exports; source binding/maps; schema/operation types and overloads; editor diagnostics/completion/hover/definitions/highlighting/refactors; fragment graph/masking/unmask/testing/colocation/field use; scalar/enum/input helpers; persisted bytes/declarations/manifests/actions/omission; schema/config/load/refresh; CLI/callables/annotations; all eight scan rules/output graph; performance; packaging/security/reference/docs.
- The eight scan rules have separate stories #67–#74. Public and advanced/internal subpaths remain traceable through #25/#26/#36/#44/#52–#54/#76/#87 and their backend dependencies, rather than being lost under one API label.
- #42 is the live-typing milestone acceptance story, not a countable shipped feature. #43–#51 are required immediately after it; #52–#57 cover remaining fragment/editor work; schema/CLI/project and release/performance follow their recorded dependencies.
- #58 local schema loading is deliberately Stage1 because the worker/editor/live matrix needs it; authenticated remote workflows and full CLI remain later. #42 also explicitly depends on scalar mappings, oneOf, highlighting and scalar helper branches.
- #82 standalone .graphql, #83/#84 persistent cache/CLI and #86 additional editor/platform evaluation are optional-extra stories. #85 saved models/clients is a separate optional evaluation, not a core generator choice. Declared platform acceptance in #78 remains required; #86 cannot downgrade it.
- Research #2–#5 and planning #6/#12 are complete evidence, not completed product features. #11/#16/#17/#18 are completed foundation stories spanning five initial production PRs. Their broader ledger areas stay partial/open.
- Every non-epic issue is linked as a GitHub sub-issue of #1, with acceptance criteria, dependency links, classification and milestone/stage. Future implementation PRs must reference the selected small story. If a story needs several PRs, preserve its acceptance scope and split linked followups rather than removing criteria.

## Observed platform blocker

Historical blocker, resolved for bounded acceptance: early isolated VS Code1.141.0 and1.142.0 tests left the restored stub dirty, and the in-memory overlay path was rejected. Later owned-disk adapter tests verify Pylance inference, settings migration and owned-artifact cleanup while preserving foreign edits; #23/#24 are closed on that evidence. Broader lifecycle and inference acceptance remains open in #38 and gate #42. The earlier research proof remains separately bounded. Do not use resource-unbound revert commands or force saves to manufacture a cleanup pass.

## Verified reconciliation

The reconciliation verified **78 GitHub issues, 78 matching private-project issue items and 77 linked epic children**, with every issue assigned a classification, stage and milestone. At this reconciliation point, 66 open implementation/acceptance stories remain: 61 required-parity, four optional-extra and one separate optional integration. The open epic and this tracking task are additional metadata issues, not implementation features. Closed research/planning items are excluded from delivered-feature counts. Later completed stories reduce the live backlog; use GitHub for current counts rather than treating this snapshot as a percentage.
