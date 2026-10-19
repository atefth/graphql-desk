# GraphQL Desk

Planned Python developer tooling targeting **full user-visible parity with gql.tada 1.11.4 and its GraphQLSP VS Code integration**. Python-native equivalents are welcome; no upstream capability may be silently dropped. No product implementation exists yet.

Start with the [canonical parity plan](docs/parity-plan.md): requirements, acceptance gates, decisions and delivery order. Review [evidence, reuse and support](docs/audits/evidence-and-reuse.md), then the pinned [gql.tada inventory](docs/audits/gql-tada-source-audit.md) and [GraphQLSP inventory](docs/audits/graphqlsp-source-audit.md).

Generated schema typing and live per-operation result/variable typing in VS Code/Pyright/Pylance are core. Static GraphQL embedded in Python calls is required; standalone `.graphql` support is optional extra scope. The persistent turbo-like cache is optional.

One unsaved nullability edit changed Pylance hover/diagnostics after an open generated `.pyi` was updated in memory. Broad semantics/lifecycle remain unproven; disk-only replacement did not refresh within the tested window. Fragment helpers preserve runtime identity without wrapping, copying or validation. No core typing generator backend is selected.

After the live-typing acceptance gate, persisted operations and independent editor conveniences begin immediately, in parallel where independent. Fragment and remaining editor parity, schema/CLI/project workflows, then performance/package/release gates follow. An incremental milestone is not full parity.

## Planning and contributions

[Issue #1](https://github.com/atefth/graphql-desk/issues/1) is the full-parity epic; [issue #6](https://github.com/atefth/graphql-desk/issues/6) owns support/review. Feasibility issues are closed and documented; the roadmap board remains private.

Planning issues use goal, context, acceptance criteria, out of scope and dependencies. Spikes record question, method, deliverables and decision criteria. Production PRs must be small, issue-linked, have GitHub Actions from the first implementation, use Ponytail while coding and pass Ponytail review before publication. Docs are ready for review; [remaining planning gaps](docs/planning-readiness-audit.md) must close before implementation. No product acceptance gates have passed. Product implementation starts only after the docs PRs land; issue/roadmap updates follow landing.
