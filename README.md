# GraphQL Desk

Planned Python developer tooling targeting **full user-visible parity with gql.tada 1.11.4 and its GraphQLSP VS Code integration**. Python-native equivalents are welcome; no upstream capability may be silently dropped. The foundation package carries GraphQL documents and produces pure retained-source typing artifacts; no editor or broad typing gate has passed.

See [feature status](docs/feature-status.md) and [complete issue tracking](docs/feature-tracking.md) for built versus remaining work. Start with the [canonical parity plan](docs/parity-plan.md): requirements, acceptance gates, decisions and delivery order. Review [evidence, reuse and support](docs/audits/evidence-and-reuse.md), then the pinned [gql.tada inventory](docs/audits/gql-tada-source-audit.md) and [GraphQLSP inventory](docs/audits/graphqlsp-source-audit.md).

Generated schema typing and live per-operation result/variable typing in VS Code/Pyright/Pylance are core. Static GraphQL embedded in Python calls is required; standalone `.graphql` support is optional extra scope. The persistent turbo-like cache is optional.

One unsaved nullability edit changed Pylance hover/diagnostics after an open generated `.pyi` was updated in memory. Broad semantics/lifecycle remain unproven; disk-only replacement did not refresh within the tested window. Identity fragment references, masked/unmasked renderers and exact executable AST declarations now have focused static foundations. Bounded root and generated fragment readers are implemented; complete reader precision and required mask/result helpers remain open. Primary virtual-document research was negative; seeded local owned-disk research was positive within its fixture. The product policy remains pending, and no production disk analysis adapter is authorized. No optional saved-model/client backend is selected.

After the live-typing acceptance gate, persisted operations and independent editor conveniences begin immediately, in parallel where independent. Fragment and remaining editor parity, schema/CLI/project workflows, then performance/package/release gates follow. An incremental milestone is not full parity.

## Planning and contributions

[Issue #1](https://github.com/atefth/graphql-desk/issues/1) is the full-parity epic; [issue #6](https://github.com/atefth/graphql-desk/issues/6) owns support/review. Feasibility issues are closed and documented; the roadmap board remains private.

Planning issues use goal, context, acceptance criteria, out of scope and dependencies. Spikes record question, method, deliverables and decision criteria. Production PRs must be small, issue-linked, have GitHub Actions from the first implementation, use Ponytail while coding and pass Ponytail review before publication. Planning contracts are ready for review; see the [eight-gap disposition and evidence](docs/planning-readiness-audit.md). No product acceptance gates have passed. The initial docs prerequisites have landed. Implementation proceeds through linked small stories; independent Ponytail review and required hosted CI must pass before readiness/merge, and issue/roadmap states follow actual results.

## Development foundation

Requires Python 3.10–3.14 and uv 0.13.2. Install the locked development environment with `uv sync --locked`, then run:

```text
uv run ruff check src tests scripts
uv run ruff format --check src tests scripts
uv run pyright
uv run mypy
uv run python -m unittest discover -s tests
uv build --no-sources --no-build-isolation
```

CI checks all five Python minors on Ubuntu 24.04 x64, with Windows Server 2025 x64 and macOS 14 arm64 smoke lanes. Windows Server is a CI smoke environment, not evidence for the planned Windows 11 editor support. These package checks do not establish VS Code/Pylance, generated typing, fragment, client, performance or full-parity acceptance. The aggregate `CI` check is required on `main` with strict up-to-date checks and administrator enforcement; this branch protection was verified separately from the workflow.

Runtime dependencies are pinned `graphql-core==3.3.0` (MIT) and `typing_extensions==4.16.0` (PSF-2.0), needed for generated declarations on Python 3.10. CI audits the locked runtime/development dependency set and rejects runtime dependencies outside the reviewed license inventory. Python 3.10 additionally uses `tomli==2.5.0` (MIT) for TOML configuration; later Python versions use the standard library. Optional generators are absent. Builds use the locked Hatchling backend; smoke checks rebuild the source archive, then install both wheels into fresh environments outside the source tree.

The pure [schema declaration renderer](docs/implementation/schema-types.md) currently covers built-in scalars, enums and input objects, independently of operation generation. The [operation renderer](docs/implementation/operation-types.md) adds validated result/variable declarations. Pure [factory artifacts](docs/implementation/factory-artifacts.md) connect supported literal calls to those declarations, nominal fragment references, exact AST views and source companion aliases; publication and live integration remain open. Generated fallback calls expose read-only AST metadata without trusted result/variable generics. Incorrect known constructor calls can still select that view, so source diagnostics and complete inference remain open.

The [scalar helpers](docs/implementation/scalar-helpers.md) return identical values, with configured input checks on generated schema factories. Captured-local mapping-symbol preflight is implemented; external/richer type resolution and full helper/API acceptance remain open.

The [runtime factories](docs/implementation/runtime-factories.md) provide untyped `graphql`/`gql` construction and schema-name binding; precise generated inference remains open.
The [schema loaders](docs/implementation/schema-loading.md) validate explicit SDL, introspection JSON and configured HTTP(S) inputs. The trusted worker supports explicit remote refresh; the complete schema workflow remains open.

The [configuration model](docs/implementation/configuration.md) supplies single/named-schema facts, nearest-project ownership/routing and captured configuration text. Finite [editor snapshots](docs/implementation/editor-snapshots.md) acquire explicit candidates and current buffers without executing project Python; the installed `graphql-desk doctor` command inspects configuration and schemas without writes, and explicitly reports unverified editor/bridge health as degraded. Complete CLI integration remains open under #27.

## Typed-document boundary

`TypedDocument[Result, Variables]` holds a `graphql-core` `DocumentNode` in `.ast` and an optional `.operation_name`. The carrier preserves the supplied AST and performs no schema validation or response coercion. Direct construction uses annotations supplied by the caller. Pure generated artifacts infer supported literal factory calls, but complete inference, generated publication and live integration remain open.

```python
from typing import TypedDict

from graphql import parse
from graphql_desk import TypedDocument

class GreetingResult(TypedDict):
    greeting: str

class GreetingVariables(TypedDict):
    name: str

document = TypedDocument[GreetingResult, GreetingVariables](
    parse("query Greeting($name: String!) { greeting(name: $name) }"),
    operation_name="Greeting",
)
```

An AST-consuming executor receives `document.ast` and `document.operation_name`; its data/error envelope remains unchanged. The carrier cannot verify that manual result/variable annotations describe the AST. A generic user-side signature `execute(document: TypedDocument[R, V], variables: V) -> R` can correlate the two types; this package supplies no execution client. Use an explicitly typed `Variables` binding at that boundary: mypy does not reliably infer the correlated variable type from an inline dictionary. The frozen carrier prevents rebinding its fields; it does not freeze graphql-core's mutable AST internals.

## Incremental live-typing primitives

`graphql_desk.lifecycle` supplies a pure `Snapshot` / `Coordinator[T]` publication guard. Each request gets a fresh ticket; completion requires the active ticket and a newly observed matching source/dependency/schema/config snapshot. New work, cancellation, failure and invalidation hide older values. Callers must serialize transitions and recheck before external writes: this coordinator cannot make VS Code document edits atomic.

This is groundwork for the live bridge. It performs no editor/file mutation and does not prove Pylance refresh, owned-buffer restoration, dirty-state safety or any broader lifecycle gate.


The pure [Python call extractor](docs/implementation/source-extraction.md) recognizes a conservative subset of direct GraphQL Desk imports and inline literals in one source snapshot. It returns decoded text, host ranges and explicit diagnostics without executing Python; it does not yet resolve complete projects or generate types.

The [static module graph](docs/implementation/source-graph.md) resolves a first subset of constants/imports/reexports from explicit versioned snapshots. Exact caller-supplied factory/schema metadata is supported; ordered imported fragment references and exact decoded maps are also implemented. The explicit `IncrementalResolver` reuses parsing and unchanged project results; snapshot acquisition can retain unchanged disk-source versions across generations. These are cache correctness foundations, not measured responsiveness. The read-only worker can reuse this resolver explicitly. Production discovery, complete incremental freshness and safe edit consumers remain open.


## Read-only editor foundation

Merged through #148, 63 foundation slices have 338 Python and 40 Node tests; no broad parity gate has passed. The [read-only providers and activation controller](docs/implementation/editor-providers.md) supply embedded diagnostics, completion, hover, definitions and highlighting through the trusted worker. Actual activation passed isolated VS Code1.141.0/1.142.0 hosts with pinned Python/Pylance extensions, dirty buffers and native Python coexistence.

Configure an absolute `graphqlDesk.pythonPath` interpreter with GraphQL Desk installed. Local candidate enumeration and captured source/configuration/schema buffers feed the worker; publication rechecks inventory and freshness. Remote schemas require the explicit refresh command, and typing does not fetch them. Complete discovery/lifecycle and supported-platform acceptance remain open. No VSIX release, generated-file writes or disk analysis adapter are installed. See [feature status](docs/feature-status.md) for all remaining gates.
