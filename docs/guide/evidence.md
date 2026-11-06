# Guide coverage and remaining acceptance

This guide and the worked project address
[#81](https://github.com/atefth/graphql-desk/issues/81). Documentation and one
example do not close the pinned conformance corpus
[#80](https://github.com/atefth/graphql-desk/issues/80), the full parity epic, or a
release gate. The root README refresh also remains outstanding; the current
worked workflow is described by these guide pages.

## Executable worked evidence

```console
uv run python examples/typed_client/verify.py
```

Locally verified on 2026-11-06 with CPython 3.13.7, Windows 11, mypy 2.4.0,
Pyright 1.1.414 and the repository's provisioned dependencies. This command
generated output from the original source, checked positive and deliberate
negative typing cases, executed local graphql-core/gql clients, checked fragment
identity and partial errors, and built/checked/imported a source-free wheel.
No new environment, dependency install, external server or VS Code host was used.

The verifier's success message is a scoped result. It is not a new measurement
for all Python minors or host operating systems. The wheel is built and imported
as an archive, not installed into a clean consumer environment by this example.

## Requirement-to-example/evidence map

| #81 area | Documentation and runnable evidence | Boundary |
| --- | --- | --- |
| Install/setup | [Getting started](getting-started.md), [project-init contract](../implementation/project-init.md), [package tests](../../tests/test_package.py), [init tests](../../tests/test_cli_init.py) | Source checkout workflow; no published package/Marketplace claim or fresh install performed for this documentation task. |
| Same-source typing/client/errors | [Worked original source](../../examples/typed_client/authoring/operations.py), verifier, [client fixtures](../../tests/test_client_bindings.py) | Local core and gql execution; broader bindings cover mutations/subscriptions/cancellation. No network-transport or arbitrary-client inference promise. |
| Fragment helpers | [Typing guide](typing-and-fragments.md), verifier, [reader fixtures](../../tests/test_fragment_helpers.py), [tuple helper fixtures](../../tests/test_testing_helpers.py) | Runtime identity and precise saved-reader path demonstrated. Live SDK tuple overlays and conservative abstract/raw-input cases use their own fixtures; this example does not create an automatic mypy overlay. |
| Retained/omitted persisted builds | [Release guide](persisted-and-releases.md), [ID-only app](../../examples/typed_client/app.py), verifier, [wheel tests](../../tests/test_release_artifacts.py), [manifest tests](../../tests/test_persisted_manifest.py) | Real local wheel and isolated archive import; no universal APQ protocol, server registration or complete distribution matrix. |
| Config/CLI/doctor | [Command guide](configuration-and-cli.md), [CLI tests](../../tests/test_cli.py), [captured doctor tests](../../tests/test_doctor_worker.py), [doctor contract](../implementation/doctor.md) | Standalone doctor stays degraded. Active editor/checker/publication health comes only from the live host; complete native doctor acceptance is separate. |
| Internal exports | [API guide](api-and-compatibility.md), [full internal reference](../implementation/internal-api.md), [internal fixtures](../../tests/test_internal.py) | Advanced surface without a stability promise; explicit loads/subscriptions own their side effects. Optional cache utilities remain unsupported. |
| Diagnostics and scan | [Command guide](configuration-and-cli.md), [scan acceptance fixture](../../tests/fixtures/scan_acceptance/README.md), [scan tests](../../tests/test_scan_acceptance.py) | Experimental insights and bounded static discovery are not complete project/runtime analysis or upstream output conformance. |
| Maturity/platform/migration | [Compatibility guide](api-and-compatibility.md), [support contract](../contracts/support-performance-and-evidence.md), [CI workflow](../../.github/workflows/ci.yml) | Planned release platforms, performance budgets, clean installs and complete native lifecycle need their own evidence. |

Useful focused replays in the locked environment:

```console
uv run python -m unittest tests.test_client_bindings tests.test_fragment_helpers tests.test_testing_helpers
uv run python -m unittest tests.test_release_artifacts tests.test_persisted_manifest
uv run python -m unittest tests.test_cli tests.test_doctor_worker tests.test_internal
```

These are links/commands to maintained acceptance fixtures, not a claim that all
were rerun on every platform for this documentation change. The worked verifier
was run locally; its helper binding is copied from the existing maintained file.

## Outstanding dependencies and decisions

The client, fixture-helper and omitted-wheel stories
[#41](https://github.com/atefth/graphql-desk/issues/41),
[#54](https://github.com/atefth/graphql-desk/issues/54) and
[#46](https://github.com/atefth/graphql-desk/issues/46) have their own scoped
acceptance. Their closure does not establish the whole product. The prerequisites
for complete #81 acceptance include doctor
[#65](https://github.com/atefth/graphql-desk/issues/65) and upstream conformance
[#80](https://github.com/atefth/graphql-desk/issues/80); consult those issues for
their current state. This documentation does not satisfy their remaining gates.

All broad product gates remain open, including lifecycle/reader retention,
performance and release acceptance. The optional standalone-source/cache/editor
items are not silently promised. The repository license remains an owner
decision, and the blocked root README update remains owner follow-up. No issue
or gate should be closed solely because this guide or the verifier exists.

The README replacement was not executed: automatic approval review rejected the
PowerShell/Python action `Path('README.md').write_text(<replacement README>,
encoding='utf8')`, stating only **"blocked by policy"**. README was left untouched;
the write was not retried or routed through another tool. That owner action is
still outstanding, so this is a reviewable guide/example deliverable, not a claim
that #81's complete documentation readiness has passed.
