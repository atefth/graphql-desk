# Extended local fixture and historical-output inventory

Supplement to [local coverage audit](local-artifact-coverage.md), inspected 2026-10-19. Paths are relative to the original research `work/` directory. Every row below was read as a fixture/configuration or parsed dependency snapshot; identical generated support modules were compared as copies. SHA-256 prefixes identify the inspected bytes, not a reproduction guarantee. No spike was rerun.

95 files: 88 source/configuration/generated fixture files plus seven npm lockfiles. This inventories research artifacts, not product deliverables. API, Lifecycle, Evidence and Reuse refer to the linked contracts in the parent audit.

| Artifact | SHA-256 prefix | Finding / repository coverage |
|---|---|---|
| `graphql-desk-fragments/fragment_masking.py` | `0515709af70b` | Older allocating wrapper fixture; superseded by identity-only API. API/Evidence. |
| `graphql-desk-fragments/fragments.graphql` | `23d94958a305` | Older allocating wrapper fixture; superseded by identity-only API. API/Evidence. |
| `graphql-desk-fragments/identity-proof/api.py` | `c45ccd3a87e3` | Identity marker, checker boundary and runtime identity inputs; narrow E-FRAGMENT proof. API/Evidence. |
| `graphql-desk-fragments/identity-proof/api.pyi` | `9fcaf70f13ff` | Identity marker, checker boundary and runtime identity inputs; narrow E-FRAGMENT proof. API/Evidence. |
| `graphql-desk-fragments/identity-proof/invalid.py` | `d2a2163002d2` | Identity marker, checker boundary and runtime identity inputs; narrow E-FRAGMENT proof. API/Evidence. |
| `graphql-desk-fragments/identity-proof/pyrightconfig.json` | `f9e12091935f` | Identity marker, checker boundary and runtime identity inputs; narrow E-FRAGMENT proof. API/Evidence. |
| `graphql-desk-fragments/identity-proof/runtime_identity.py` | `9fddf40f31f6` | Identity marker, checker boundary and runtime identity inputs; narrow E-FRAGMENT proof. API/Evidence. |
| `graphql-desk-fragments/identity-proof/valid.py` | `c6bf7b07ca2f` | Identity marker, checker boundary and runtime identity inputs; narrow E-FRAGMENT proof. API/Evidence. |
| `graphql-desk-fragments/invalid_direct_access.py` | `08102c667f08` | Older allocating wrapper fixture; superseded by identity-only API. API/Evidence. |
| `graphql-desk-fragments/pyrightconfig-mypy.ini` | `abdf82c9cf63` | Older allocating wrapper fixture; superseded by identity-only API. API/Evidence. |
| `graphql-desk-fragments/pyrightconfig.json` | `1b1518be5317` | Older allocating wrapper fixture; superseded by identity-only API. API/Evidence. |
| `graphql-desk-fragments/schema.graphql` | `ea401cf13a18` | Older allocating wrapper fixture; superseded by identity-only API. API/Evidence. |
| `graphql-desk-generators/spikes/codegen-comparison/graphql.config.yaml` | `b1567fce97a5` | Optional generator schema/config/model/runtime sample; omission, nullability, aliases and scalar limits retained. API/Reuse/Evidence. |
| `graphql-desk-generators/spikes/codegen-comparison/operations.graphql` | `265025cb42b0` | Optional generator schema/config/model/runtime sample; omission, nullability, aliases and scalar limits retained. API/Reuse/Evidence. |
| `graphql-desk-generators/spikes/codegen-comparison/out-ariadne/ariadne_generated/__init__.py` | `b1a732a3f8cd` | Optional generator schema/config/model/runtime sample; omission, nullability, aliases and scalar limits retained. API/Reuse/Evidence. |
| `graphql-desk-generators/spikes/codegen-comparison/out-ariadne/ariadne_generated/async_base_client.py` | `a403378e09d3` | Optional generator schema/config/model/runtime sample; omission, nullability, aliases and scalar limits retained. API/Reuse/Evidence. |
| `graphql-desk-generators/spikes/codegen-comparison/out-ariadne/ariadne_generated/base_model.py` | `8a6614240b25` | Optional generator schema/config/model/runtime sample; omission, nullability, aliases and scalar limits retained. API/Reuse/Evidence. |
| `graphql-desk-generators/spikes/codegen-comparison/out-ariadne/ariadne_generated/client.py` | `4b28f0922c7c` | Optional generator schema/config/model/runtime sample; omission, nullability, aliases and scalar limits retained. API/Reuse/Evidence. |
| `graphql-desk-generators/spikes/codegen-comparison/out-ariadne/ariadne_generated/exceptions.py` | `986d776a9b27` | Optional generator schema/config/model/runtime sample; omission, nullability, aliases and scalar limits retained. API/Reuse/Evidence. |
| `graphql-desk-generators/spikes/codegen-comparison/out-ariadne/ariadne_generated/fragments.py` | `cd89b15c521a` | Optional generator schema/config/model/runtime sample; omission, nullability, aliases and scalar limits retained. API/Reuse/Evidence. |
| `graphql-desk-generators/spikes/codegen-comparison/out-ariadne/ariadne_generated/user_by_id.py` | `88e7beda1cfa` | Optional generator schema/config/model/runtime sample; omission, nullability, aliases and scalar limits retained. API/Reuse/Evidence. |
| `graphql-desk-generators/spikes/codegen-comparison/out-turms/schema.py` | `3ab9a664b4a5` | Optional generator schema/config/model/runtime sample; omission, nullability, aliases and scalar limits retained. API/Reuse/Evidence. |
| `graphql-desk-generators/spikes/codegen-comparison/pyproject.toml` | `8fa3376622b6` | Optional generator schema/config/model/runtime sample; omission, nullability, aliases and scalar limits retained. API/Reuse/Evidence. |
| `graphql-desk-generators/spikes/codegen-comparison/schema.graphql` | `43e692c056d6` | Optional generator schema/config/model/runtime sample; omission, nullability, aliases and scalar limits retained. API/Reuse/Evidence. |
| `graphql-desk-generators/spikes/codegen-edge-cases/graphql.config.yaml` | `2bf7b6fc763e` | Optional generator schema/config/model/runtime sample; omission, nullability, aliases and scalar limits retained. API/Reuse/Evidence. |
| `graphql-desk-generators/spikes/codegen-edge-cases/operations.graphql` | `65f298e6bb40` | Optional generator schema/config/model/runtime sample; omission, nullability, aliases and scalar limits retained. API/Reuse/Evidence. |
| `graphql-desk-generators/spikes/codegen-edge-cases/out-ariadne/ariadne_generated/__init__.py` | `72f16c18206b` | Optional generator schema/config/model/runtime sample; omission, nullability, aliases and scalar limits retained. API/Reuse/Evidence. |
| `graphql-desk-generators/spikes/codegen-edge-cases/out-ariadne/ariadne_generated/async_base_client.py` | `a403378e09d3` | Optional generator schema/config/model/runtime sample; omission, nullability, aliases and scalar limits retained. API/Reuse/Evidence. |
| `graphql-desk-generators/spikes/codegen-edge-cases/out-ariadne/ariadne_generated/base_model.py` | `8a6614240b25` | Optional generator schema/config/model/runtime sample; omission, nullability, aliases and scalar limits retained. API/Reuse/Evidence. |
| `graphql-desk-generators/spikes/codegen-edge-cases/out-ariadne/ariadne_generated/client.py` | `e81f244ea067` | Optional generator schema/config/model/runtime sample; omission, nullability, aliases and scalar limits retained. API/Reuse/Evidence. |
| `graphql-desk-generators/spikes/codegen-edge-cases/out-ariadne/ariadne_generated/exceptions.py` | `986d776a9b27` | Optional generator schema/config/model/runtime sample; omission, nullability, aliases and scalar limits retained. API/Reuse/Evidence. |
| `graphql-desk-generators/spikes/codegen-edge-cases/out-ariadne/ariadne_generated/fragments.py` | `53e1655d98ae` | Optional generator schema/config/model/runtime sample; omission, nullability, aliases and scalar limits retained. API/Reuse/Evidence. |
| `graphql-desk-generators/spikes/codegen-edge-cases/out-ariadne/ariadne_generated/input_types.py` | `26aa71b5f853` | Optional generator schema/config/model/runtime sample; omission, nullability, aliases and scalar limits retained. API/Reuse/Evidence. |
| `graphql-desk-generators/spikes/codegen-edge-cases/out-ariadne/ariadne_generated/search_users.py` | `9cb7bfed7185` | Optional generator schema/config/model/runtime sample; omission, nullability, aliases and scalar limits retained. API/Reuse/Evidence. |
| `graphql-desk-generators/spikes/codegen-edge-cases/out-turms/schema.py` | `0a731a924b58` | Optional generator schema/config/model/runtime sample; omission, nullability, aliases and scalar limits retained. API/Reuse/Evidence. |
| `graphql-desk-generators/spikes/codegen-edge-cases/pyproject.toml` | `8fa3376622b6` | Optional generator schema/config/model/runtime sample; omission, nullability, aliases and scalar limits retained. API/Reuse/Evidence. |
| `graphql-desk-generators/spikes/codegen-edge-cases/schema.graphql` | `5a25bb9c4748` | Optional generator schema/config/model/runtime sample; omission, nullability, aliases and scalar limits retained. API/Reuse/Evidence. |
| `graphql-desk-generators/spikes/turms-watch-probe/graphql.config.yaml` | `19d520d696a2` | Optional generator schema/config/model/runtime sample; omission, nullability, aliases and scalar limits retained. API/Reuse/Evidence. |
| `graphql-desk-generators/spikes/turms-watch-probe/operations.graphql` | `729a82c667fd` | Optional generator schema/config/model/runtime sample; omission, nullability, aliases and scalar limits retained. API/Reuse/Evidence. |
| `graphql-desk-generators/spikes/turms-watch-probe/out/schema.py` | `0a731a924b58` | Optional generator schema/config/model/runtime sample; omission, nullability, aliases and scalar limits retained. API/Reuse/Evidence. |
| `graphql-desk-generators/spikes/turms-watch-probe/schema.graphql` | `5a25bb9c4748` | Optional generator schema/config/model/runtime sample; omission, nullability, aliases and scalar limits retained. API/Reuse/Evidence. |
| `graphql-desk-lsp/spikes/vscode-client-clean-adapter/.vscode-test.js` | `44bb6cd14e71` | Standalone editor host/protocol fixture; exact replay corrected, broad refresh unproved. E-LSP/Evidence. |
| `graphql-desk-lsp/spikes/vscode-client-clean-adapter/extension.js` | `40442f1a5a99` | Standalone editor host/protocol fixture; exact replay corrected, broad refresh unproved. E-LSP/Evidence. |
| `graphql-desk-lsp/spikes/vscode-client-clean-adapter/package-lock.json` | `37ea28b88a77` | Dependency snapshot inspected; historical versions only, no fresh security claim. Evidence/Reuse. |
| `graphql-desk-lsp/spikes/vscode-client-clean-adapter/package.json` | `eeb0a8dbb7d1` | Standalone editor host/protocol fixture; exact replay corrected, broad refresh unproved. E-LSP/Evidence. |
| `graphql-desk-lsp/spikes/vscode-client-clean-adapter/server.js` | `8e9db52570c0` | Standalone editor host/protocol fixture; exact replay corrected, broad refresh unproved. E-LSP/Evidence. |
| `graphql-desk-lsp/spikes/vscode-client-clean-adapter/test/suite.js` | `4e78038dc536` | Standalone editor host/protocol fixture; exact replay corrected, broad refresh unproved. E-LSP/Evidence. |
| `graphql-desk-lsp/spikes/vscode-client-clean-adapter/test-workspace/.vscode/settings.json` | `27489e162fbd` | Standalone editor host/protocol fixture; exact replay corrected, broad refresh unproved. E-LSP/Evidence. |
| `graphql-desk-lsp/spikes/vscode-client-clean-adapter/test-workspace/operation.graphql` | `f42d4afbb08e` | Standalone editor host/protocol fixture; exact replay corrected, broad refresh unproved. E-LSP/Evidence. |
| `graphql-desk-lsp/spikes/vscode-client-clean-adapter/test-workspace/schema.graphql` | `ca08596d07bf` | Standalone editor host/protocol fixture; exact replay corrected, broad refresh unproved. E-LSP/Evidence. |
| `graphql-desk-lsp/spikes/vscode-lsp/.graphqlrc.yml` | `a372fb59417c` | Older standalone protocol/config/dependency probe; vulnerable CLI tree quarantined. Reuse/Evidence. |
| `graphql-desk-lsp/spikes/vscode-lsp/audit-cli-3.3.32/package-lock.json` | `272399d68cd0` | Dependency snapshot inspected; historical versions only, no fresh security claim. Evidence/Reuse. |
| `graphql-desk-lsp/spikes/vscode-lsp/audit-cli-3.3.32/package.json` | `54babcd6624f` | Older standalone protocol/config/dependency probe; vulnerable CLI tree quarantined. Reuse/Evidence. |
| `graphql-desk-lsp/spikes/vscode-lsp/audit-language-service-only/package-lock.json` | `c8258a2b9d66` | Dependency snapshot inspected; historical versions only, no fresh security claim. Evidence/Reuse. |
| `graphql-desk-lsp/spikes/vscode-lsp/audit-language-service-only/package.json` | `6405b5fed5f9` | Older standalone protocol/config/dependency probe; vulnerable CLI tree quarantined. Reuse/Evidence. |
| `graphql-desk-lsp/spikes/vscode-lsp/audit-latest/.graphqlrc.yml` | `a372fb59417c` | Older standalone protocol/config/dependency probe; vulnerable CLI tree quarantined. Reuse/Evidence. |
| `graphql-desk-lsp/spikes/vscode-lsp/audit-latest/check.mjs` | `aa4d910be9db` | Older standalone protocol/config/dependency probe; vulnerable CLI tree quarantined. Reuse/Evidence. |
| `graphql-desk-lsp/spikes/vscode-lsp/audit-latest/operation.graphql` | `2ba30bf3ca1b` | Older standalone protocol/config/dependency probe; vulnerable CLI tree quarantined. Reuse/Evidence. |
| `graphql-desk-lsp/spikes/vscode-lsp/audit-latest/package-lock.json` | `675ea7807897` | Dependency snapshot inspected; historical versions only, no fresh security claim. Evidence/Reuse. |
| `graphql-desk-lsp/spikes/vscode-lsp/audit-latest/package.json` | `f5b9043ed4a5` | Older standalone protocol/config/dependency probe; vulnerable CLI tree quarantined. Reuse/Evidence. |
| `graphql-desk-lsp/spikes/vscode-lsp/audit-latest/schema.graphql` | `4ad3e6649756` | Older standalone protocol/config/dependency probe; vulnerable CLI tree quarantined. Reuse/Evidence. |
| `graphql-desk-lsp/spikes/vscode-lsp/check.mjs` | `aa4d910be9db` | Older standalone protocol/config/dependency probe; vulnerable CLI tree quarantined. Reuse/Evidence. |
| `graphql-desk-lsp/spikes/vscode-lsp/operation.graphql` | `2ba30bf3ca1b` | Older standalone protocol/config/dependency probe; vulnerable CLI tree quarantined. Reuse/Evidence. |
| `graphql-desk-lsp/spikes/vscode-lsp/package-lock.json` | `93603e63882a` | Dependency snapshot inspected; historical versions only, no fresh security claim. Evidence/Reuse. |
| `graphql-desk-lsp/spikes/vscode-lsp/package.json` | `55ed69ea427c` | Older standalone protocol/config/dependency probe; vulnerable CLI tree quarantined. Reuse/Evidence. |
| `graphql-desk-lsp/spikes/vscode-lsp/schema.graphql` | `4ad3e6649756` | Older standalone protocol/config/dependency probe; vulnerable CLI tree quarantined. Reuse/Evidence. |
| `graphql-desk-pyright/package-lock.json` | `8925b23038d9` | Dependency snapshot inspected; historical versions only, no fresh security claim. Evidence/Reuse. |
| `graphql-desk-pyright/package.json` | `596bfd6512c5` | Older standalone protocol/config/dependency probe; vulnerable CLI tree quarantined. Reuse/Evidence. |
| `graphql-desk-pyright/spikes/pylance-vscode/host-test/extension/extension.js` | `5f9890377792` | Fixed mapping, open-stub WorkspaceEdit and consumer test; no general renderer. E-PYLANCE/Lifecycle. |
| `graphql-desk-pyright/spikes/pylance-vscode/host-test/extension/package.json` | `70a63d7d6fe8` | Fixed mapping, open-stub WorkspaceEdit and consumer test; no general renderer. E-PYLANCE/Lifecycle. |
| `graphql-desk-pyright/spikes/pylance-vscode/host-test/package-lock.json` | `a44ab6e7bdba` | Dependency snapshot inspected; historical versions only, no fresh security claim. Evidence/Reuse. |
| `graphql-desk-pyright/spikes/pylance-vscode/host-test/package.json` | `d3df2bfb24ea` | Fixed mapping, open-stub WorkspaceEdit and consumer test; no general renderer. E-PYLANCE/Lifecycle. |
| `graphql-desk-pyright/spikes/pylance-vscode/host-test/run-tests.cjs` | `6ffa395d50c6` | Fixed mapping, open-stub WorkspaceEdit and consumer test; no general renderer. E-PYLANCE/Lifecycle. |
| `graphql-desk-pyright/spikes/pylance-vscode/host-test/tests/index.cjs` | `a36725c9d211` | Fixed mapping, open-stub WorkspaceEdit and consumer test; no general renderer. E-PYLANCE/Lifecycle. |
| `graphql-desk-pyright/spikes/pylance-vscode/host-test/workspace/consumer.py` | `5922ab49a0d6` | Fixed mapping, open-stub WorkspaceEdit and consumer test; no general renderer. E-PYLANCE/Lifecycle. |
| `graphql-desk-pyright/spikes/pylance-vscode/host-test/workspace/generated.pyi` | `e471b10985bd` | Fixed mapping, open-stub WorkspaceEdit and consumer test; no general renderer. E-PYLANCE/Lifecycle. |
| `graphql-desk-pyright/spikes/pylance-vscode/host-test/workspace/operation.graphql` | `26b422482b83` | Fixed mapping, open-stub WorkspaceEdit and consumer test; no general renderer. E-PYLANCE/Lifecycle. |
| `graphql-desk-pyright/spikes/pylance-vscode/host-test/workspace/pyrightconfig.json` | `78cabc94d3dd` | Fixed mapping, open-stub WorkspaceEdit and consumer test; no general renderer. E-PYLANCE/Lifecycle. |
| `graphql-desk-pyright/spikes/pyright-bridge/fixture/generated.py` | `6d3f5315760a` | Manual snapshot/checker fixture, separate analyzer; no Pylance bridge. E-TSP/Evidence. |
| `graphql-desk-pyright/spikes/pyright-bridge/fixture/get-user.graphql` | `781002f8b4fa` | Manual snapshot/checker fixture, separate analyzer; no Pylance bridge. E-TSP/Evidence. |
| `graphql-desk-pyright/spikes/pyright-bridge/fixture/invalid.py` | `37cbbcf142e5` | Manual snapshot/checker fixture, separate analyzer; no Pylance bridge. E-TSP/Evidence. |
| `graphql-desk-pyright/spikes/pyright-bridge/fixture/pyrightconfig.json` | `0c3d0a3275a4` | Manual snapshot/checker fixture, separate analyzer; no Pylance bridge. E-TSP/Evidence. |
| `graphql-desk-pyright/spikes/pyright-bridge/fixture/schema.graphql` | `63a7b041620c` | Manual snapshot/checker fixture, separate analyzer; no Pylance bridge. E-TSP/Evidence. |
| `graphql-desk-pyright/spikes/pyright-bridge/fixture/tsp_live.py` | `25e6407ae403` | Manual snapshot/checker fixture, separate analyzer; no Pylance bridge. E-TSP/Evidence. |
| `graphql-desk-pyright/spikes/pyright-bridge/fixture/valid.py` | `0a862e9c9ad1` | Manual snapshot/checker fixture, separate analyzer; no Pylance bridge. E-TSP/Evidence. |
| `graphql-desk-pyright/spikes/pyright-bridge/tsp_probe.mjs` | `0ff68f172de6` | Manual snapshot/checker fixture, separate analyzer; no Pylance bridge. E-TSP/Evidence. |
| `graphql-desk-pyright/spikes/query-edit-bridge/consumer.py` | `83137b729cbe` | Narrow scalar renderer, snapshots/checker inputs; unsupported-case guards incomplete. E-TSP/Evidence. |
| `graphql-desk-pyright/spikes/query-edit-bridge/fixture/consumer.py` | `8d701c0720e7` | Narrow scalar renderer, snapshots/checker inputs; unsupported-case guards incomplete. E-TSP/Evidence. |
| `graphql-desk-pyright/spikes/query-edit-bridge/fixture/generated.pyi` | `c76434ddb480` | Narrow scalar renderer, snapshots/checker inputs; unsupported-case guards incomplete. E-TSP/Evidence. |
| `graphql-desk-pyright/spikes/query-edit-bridge/fixture/invalid.py` | `13dde11a6ad9` | Narrow scalar renderer, snapshots/checker inputs; unsupported-case guards incomplete. E-TSP/Evidence. |
| `graphql-desk-pyright/spikes/query-edit-bridge/fixture/pyrightconfig.json` | `0c3d0a3275a4` | Narrow scalar renderer, snapshots/checker inputs; unsupported-case guards incomplete. E-TSP/Evidence. |
| `graphql-desk-pyright/spikes/query-edit-bridge/fixture/valid.py` | `b42f9a0aab3f` | Narrow scalar renderer, snapshots/checker inputs; unsupported-case guards incomplete. E-TSP/Evidence. |
| `graphql-desk-pyright/spikes/query-edit-bridge/operation.graphql` | `985a52ee262e` | Narrow scalar renderer, snapshots/checker inputs; unsupported-case guards incomplete. E-TSP/Evidence. |
| `graphql-desk-pyright/spikes/query-edit-bridge/run.mjs` | `c23414045d62` | Narrow scalar renderer, snapshots/checker inputs; unsupported-case guards incomplete. E-TSP/Evidence. |
| `graphql-desk-pyright/spikes/query-edit-bridge/schema.graphql` | `76be28ebbd97` | Narrow scalar renderer, snapshots/checker inputs; unsupported-case guards incomplete. E-TSP/Evidence. |

## Historical research chats and tool-output boundary

The original planning chat plus these 22 research chats were retrieved with tool outputs enabled. The snapshot exposed 427 shell-command output records across the 23 chats; 35 were marked truncated at the retrieval cap. The final pass located all 35 original local command records: 23 retain complete output, while 12 explicitly contain a permanent persistence truncation marker; see the recovery table below. Research final reports and relevant command results were compared with the contracts, including failed intermediate attempts. Original local rollout files were located and their tool-record metadata inventoried, and the 35 capped command records were individually investigated. Complete unrelated/control transcript bodies were not read end to end. This is an expanded substantive audit, not an exhaustive transcript certification. MCP call metadata does not establish its omitted result body.

| Research chat | Chat ID | Coverage / disposition |
|---|---|---|
| Original planning chat | `01a1554b-e8c6-7023-9270-fe0129f46daa` | User decisions and available research outputs; later full-parity/generated/live-typing instructions supersede early fallbacks. |
| `/root/gql_tada_source_audit` | `01a1558e-5ddb-7951-9df7-d9e47a5e4ae2` | Research report and available outputs compared; retained findings mapped in parent audit/contracts, preliminary alternatives superseded. |
| `/root/graphqlsp_source_audit` | `01a1558e-6c4e-7f00-ade4-c4896a0061fd` | Research report and available outputs compared; retained findings mapped in parent audit/contracts, preliminary alternatives superseded. |
| `/root/fragment_identity_proof` | `01a15592-4710-74f2-842c-09a28877c286` | Research report and available outputs compared; retained findings mapped in parent audit/contracts, preliminary alternatives superseded. |
| `/root/fragment_parity` | `01a1558b-c56f-7a41-b998-1a90b9c3f09f` | Research report and available outputs compared; retained findings mapped in parent audit/contracts, preliminary alternatives superseded. |
| `/root/persisted_and_schema` | `01a1558c-28fc-7993-bfae-6a7e447c6847` | Research report and available outputs compared; retained findings mapped in parent audit/contracts, preliminary alternatives superseded. |
| `/root/scan_perf_types` | `01a1558c-374c-7ab3-89c9-4c7067b18bcc` | Research report and available outputs compared; retained findings mapped in parent audit/contracts, preliminary alternatives superseded. |
| `/root/lsp_gap_closure` | `01a15573-bba9-79a1-9207-bfcfd1d08169` | Research report and available outputs compared; retained findings mapped in parent audit/contracts, preliminary alternatives superseded. |
| `/root/generator_decision` | `01a15573-cb32-7b00-b3d6-7a27742dbe81` | Research report and available outputs compared; retained findings mapped in parent audit/contracts, preliminary alternatives superseded. |
| `/root/full_parity_audit` | `01a15574-431c-78e2-bcf7-e774a6d1172a` | Research report and available outputs compared; retained findings mapped in parent audit/contracts, preliminary alternatives superseded. |
| `/root/pylance_end_to_end` | `01a15577-2cc7-76f3-84cd-aaeff69b53ce` | Research report and available outputs compared; retained findings mapped in parent audit/contracts, preliminary alternatives superseded. |
| `/root/lsp_spike` | `01a15565-5e8c-72f2-9565-d2be353b2fbd` | Research report and available outputs compared; retained findings mapped in parent audit/contracts, preliminary alternatives superseded. |
| `/root/parity_checklist` | `01a1556a-ec9a-7412-953a-7e38ce8a116e` | Research report and available outputs compared; retained findings mapped in parent audit/contracts, preliminary alternatives superseded. |
| `/root/fragment_masking` | `01a1556a-faac-77a2-93c9-5fa686e324dd` | Research report and available outputs compared; retained findings mapped in parent audit/contracts, preliminary alternatives superseded. |
| `/root/support_matrix` | `01a1556d-03dd-70f3-8e4c-8aef6e64cb50` | Research report and available outputs compared; retained findings mapped in parent audit/contracts, preliminary alternatives superseded. |
| `/root/query_to_pyright_bridge` | `01a1556e-0c0c-7b40-93f9-b3177445f5f6` | Research report and available outputs compared; retained findings mapped in parent audit/contracts, preliminary alternatives superseded. |
| `/root/pyright_spike` | `01a15565-74e2-7cf3-8833-e23999f0d73b` | Research report and available outputs compared; retained findings mapped in parent audit/contracts, preliminary alternatives superseded. |
| `/root/generator_spike` | `01a15565-8a45-7c91-a997-f22604c36e47` | Research report and available outputs compared; retained findings mapped in parent audit/contracts, preliminary alternatives superseded. |
| `/root/github_project_setup` | `01a1555c-c5b4-70a2-a37e-a3330a7c26c3` | Research report and available outputs compared; retained findings mapped in parent audit/contracts, preliminary alternatives superseded. |
| `/root/tada_feature_audit` | `01a1555a-40b5-7822-8189-4e1f935564e3` | Research report and available outputs compared; retained findings mapped in parent audit/contracts, preliminary alternatives superseded. |
| `/root/python_backend_audit` | `01a1555a-4ffb-7202-b909-780f7d93246b` | Research report and available outputs compared; retained findings mapped in parent audit/contracts, preliminary alternatives superseded. |
| `/root/lsp_type_bridge` | `01a1555a-5acd-7142-abb3-da66e3439ae1` | Research report and available outputs compared; retained findings mapped in parent audit/contracts, preliminary alternatives superseded. |
| `/root/gql_dx_scoping` | `01a1554d-e5bd-7862-80d3-f58d6207188b` | Research report and available outputs compared; retained findings mapped in parent audit/contracts, preliminary alternatives superseded. |

Raw output adds corroboration, not a new acceptance pass: the isolated Turms watch command recorded `GeneratedTimestampAdvanced: True`, while another attempt failed to launch and broader events crashed the filter. Neither timestamp alone proves generated model correctness. Clean LSP and Pylance success records follow intermediate failed harness runs. Narrow checker results do not prove general GraphQL semantics.

Excluded: unrelated chats/account or project listings, secrets, reasoning records, vendored dependencies/upstream mirrors, extension binaries, checker/cache databases, `.gitignore` bookkeeping, and empty output directories. Existing pinned upstream artifact audits remain the source-reference review; this inventory does not re-audit every vendored byte or recover missing/truncated historical captures.

## Final recovery of the 35 capped command outputs

Each ID below was matched individually to its original `CommandExecution` event in one of the 23 scoped local rollouts. “Complete” means the retained command output has no persistence-truncation marker; it does not mean every upstream behavior was tested. For document/source reads, existing full artifact/source audits supply the substantive reconciliation. Listings and vendored search noise are not product evidence. Missing historical bytes cannot be reconstructed from a current file without changing provenance.

| Command record ID | Retained capture | Substantive disposition |
|---|---|---|
| `exec-4106d0d0-fa3e-42d4-95fe-7d007a9fb3b9` | Persistence-truncated; omitted bytes unavailable | Fixture/dependency listing or historical diff; inspected fixture/report inventory supersedes listing as evidence. |
| `exec-111953b6-2e7b-4fea-abf0-0083a6ebcabc` | Complete retained output | Historical planning/report/source read; compared with retained artifact contracts and deliberate supersessions in parent audit. |
| `exec-dfa4ddd6-1d3f-42cd-a2e7-a5b53fb95269` | Complete retained output | Historical planning/report/source read; compared with retained artifact contracts and deliberate supersessions in parent audit. |
| `exec-5381ff9b-a4ea-4730-86ce-f90dd6caae42` | Persistence-truncated; omitted bytes unavailable | Historical planning/report/source read; compared with retained artifact contracts and deliberate supersessions in parent audit. |
| `exec-9d35a607-5f62-4fac-8397-22fb6d6d45c0` | Complete retained output | Historical planning/report/source read; compared with retained artifact contracts and deliberate supersessions in parent audit. |
| `exec-f5294550-cf92-4352-a197-42793784269d` | Persistence-truncated; omitted bytes unavailable | Pinned upstream source/search or vendored listing; source audits/contracts retain requirements, no added acceptance pass. |
| `exec-c50b641f-414a-4d22-939a-1fccbf12f2df` | Complete retained output | Historical planning/report/source read; compared with retained artifact contracts and deliberate supersessions in parent audit. |
| `exec-8feed262-7092-48e6-ac67-9baeeeb9cf87` | Complete retained output | Historical planning/report/source read; compared with retained artifact contracts and deliberate supersessions in parent audit. |
| `exec-850c884e-cf23-4239-b301-b9a72f92c2c6` | Complete retained output | Historical planning/report/source read; compared with retained artifact contracts and deliberate supersessions in parent audit. |
| `exec-cef38265-e79f-4cd3-84e2-646c47730277` | Complete retained output | Historical planning/report/source read; compared with retained artifact contracts and deliberate supersessions in parent audit. |
| `exec-51c62836-4933-4662-8219-08b8dd440349` | Complete retained output | Historical planning/report/source read; compared with retained artifact contracts and deliberate supersessions in parent audit. |
| `exec-ad8cafea-2976-4b96-8b5c-a271b4731829` | Complete retained output | Historical planning/report/source read; compared with retained artifact contracts and deliberate supersessions in parent audit. |
| `exec-30ee8dd7-6cc3-4629-8f79-7cb6f6251b9a` | Complete retained output | Historical planning/report/source read; compared with retained artifact contracts and deliberate supersessions in parent audit. |
| `exec-8f81d85d-0aaf-478e-8d6a-84a053939ea5` | Complete retained output | Historical planning/report/source read; compared with retained artifact contracts and deliberate supersessions in parent audit. |
| `exec-86e762ec-1f84-45dc-b99a-34046cf4232b` | Persistence-truncated; omitted bytes unavailable | Historical planning/report/source read; compared with retained artifact contracts and deliberate supersessions in parent audit. |
| `exec-9bd6e2e4-4a4f-453c-82b3-9b81c7f95024` | Persistence-truncated; omitted bytes unavailable | Fixture/dependency listing or historical diff; inspected fixture/report inventory supersedes listing as evidence. |
| `exec-deff104b-7d24-4c4f-8896-877cae55b6db` | Complete retained output | Historical planning/report/source read; compared with retained artifact contracts and deliberate supersessions in parent audit. |
| `exec-acb5cf40-81a9-433d-aee6-65ad5ae20402` | Persistence-truncated; omitted bytes unavailable | Pinned upstream source/search or vendored listing; source audits/contracts retain requirements, no added acceptance pass. |
| `exec-6c036955-d579-4184-b64d-d727fc35db89` | Persistence-truncated; omitted bytes unavailable | Fixture/dependency listing or historical diff; inspected fixture/report inventory supersedes listing as evidence. |
| `exec-c06657dc-9c1f-48ee-a888-2b17965a8fcf` | Complete retained output | CLI dependency audit confirms 13 high advisories; excluded tree and fresh production audit already required. |
| `exec-350a7760-02c3-4497-9572-c65851d76c0a` | Complete retained output | Earlier LSP harness fails with `suite is not defined`; later clean test succeeds. No new product result. |
| `exec-8e3bfb20-eb0e-4dbe-805c-9e3fb410ea86` | Persistence-truncated; omitted bytes unavailable | Fixture/dependency listing or historical diff; inspected fixture/report inventory supersedes listing as evidence. |
| `exec-8028c57a-bced-463c-890f-4126eb317a47` | Complete retained output | Earlier Pylance harness rejects test-runner path; isolated later run succeeds. No new product result. |
| `exec-adfb5db5-4c35-46ad-8bab-5d1f08faed09` | Complete retained output | Historical planning/report/source read; compared with retained artifact contracts and deliberate supersessions in parent audit. |
| `exec-3a7fd157-7eff-425c-9bde-7dceb0932cac` | Persistence-truncated; omitted bytes unavailable | Historical planning/report/source read; compared with retained artifact contracts and deliberate supersessions in parent audit. |
| `exec-9d86dbf1-2e0c-444f-b445-407dee64850b` | Complete retained output | Pinned upstream source/search or vendored listing; source audits/contracts retain requirements, no added acceptance pass. |
| `exec-53353b4e-9fb0-4c9b-b4af-74ccbf6d28a9` | Persistence-truncated; omitted bytes unavailable | Pinned upstream source/search or vendored listing; source audits/contracts retain requirements, no added acceptance pass. |
| `exec-9be444c8-2fd0-4eba-b36b-c2c8566f9b55` | Complete retained output | Pinned upstream source/search or vendored listing; source audits/contracts retain requirements, no added acceptance pass. |
| `exec-444b2662-db24-44ba-a5b5-c6c49b8b7967` | Complete retained output | Historical planning/report/source read; compared with retained artifact contracts and deliberate supersessions in parent audit. |
| `exec-c5144f5f-2e48-46b1-ba9f-df6ec09f7e45` | Complete retained output | Historical planning/report/source read; compared with retained artifact contracts and deliberate supersessions in parent audit. |
| `exec-668c2cf6-d76c-4509-ade7-a8cfe1239b13` | Persistence-truncated; omitted bytes unavailable | Historical planning/report/source read; compared with retained artifact contracts and deliberate supersessions in parent audit. |
| `exec-d8b049cc-5767-4cda-99b8-2f2ce4e222f8` | Complete retained output | Historical planning/report/source read; compared with retained artifact contracts and deliberate supersessions in parent audit. |
| `exec-7d9c679a-b055-4a30-a60c-06606f701ec2` | Persistence-truncated; omitted bytes unavailable | Pinned upstream source/search or vendored listing; source audits/contracts retain requirements, no added acceptance pass. |
| `exec-e40c8a6f-a17c-4448-bd25-6762b5c9aea7` | Complete retained output | Pinned upstream source/search or vendored listing; source audits/contracts retain requirements, no added acceptance pass. |
| `exec-b89efce9-22b5-40f3-8c6c-ed0161b4ea61` | Complete retained output | Pinned upstream source/search or vendored listing; source audits/contracts retain requirements, no added acceptance pass. |

Final reconciliation found no additional material requirement or decision omission. The restored contributor safeguards and isolated-generator compatibility rule remain necessary; the exact nested E-LSP replay and narrow E-TSP claim match the actual fixtures. These are research/documentation conclusions only. All 12 product acceptance rows remain open/partial. No product tests, fresh security audit, or physical editor UI interaction were performed during this audit.
