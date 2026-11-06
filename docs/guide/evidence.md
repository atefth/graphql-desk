# Guide coverage and remaining acceptance

This guide and the worked project address
[#81](https://github.com/atefth/graphql-desk/issues/81). Documentation and one
example do not close the pinned conformance corpus
[#80](https://github.com/atefth/graphql-desk/issues/80), the full parity epic, or a
release gate. The owner approved the concise [root README](../../README.md) and
the [MIT license](../../LICENSE) on 2026-11-06. The worked workflow is described
by these guide pages.

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

## Config routing and owner-record replacement

For [#329](https://github.com/atefth/graphql-desk/issues/329), the final independent
editor suite passed **472 tests**, with no failures, skips or cancellations, on
2026-11-06. The connected [project-config](../../editor/test/projectPyrightConfig.test.cjs),
[managed-setting](../../editor/test/managedStubPath.test.cjs) and
[lifecycle](../../editor/test/extensionLifecycle.test.cjs) fixtures cover routing
and interrupted owner writes.

One local Windows development-host run passed all seven phases in VS Code
1.142.0: config-empty startup, late JSON creation, permanent folder choice,
hidden Workspace choice, JSON precedence over TOML, JSON removal selecting TOML,
and JSON recreation restoring precedence. It checked precise nullable and
non-null typing, deliberate type errors and the exact native config path.
Python's public API selected the same CPython **3.10.18** before and after the
run. The isolated profile used copied Python **2026.10.0**, Pylance **2026.4.1**,
debugpy **2026.6.0** and Python Environments **1.40.0**, with application extension
updates disabled and Pylance Trace enabled; final versions and paths matched.
Real deactivation restored user settings/configs while preserving unsaved text,
source bytes and user files.

Owner replacement uses one exclusively created stage and at most four async
attempts, waiting 50/100/150 ms between retries. Each attempt rechecks phase,
trust, dirty buffers, settings/config/state, native identity, bytes and ancestry.
Cold scanning stops at a 4,096-entry budget; unknown or unverifiable stages are
preserved and block further allocation. The earlier Windows failure's
lock-versus-permission cause remains unproved.

The physically verified config-empty fixture stayed within the explicitly
approved `E:/code/gql.py` boundary and was removed, along with its isolated host
profile. This local-only harness is not a published general runner. This proves
a source extension in an installed editor's **development host**, not an
installed VSIX or all-platform acceptance. Final exact-head hosted checks remain
required; [#355](https://github.com/atefth/graphql-desk/issues/355) retention and
the broader parity/release gates remain open.

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
items are not silently promised. The MIT/README owner decisions above resolve
those documentation prerequisites. No issue or gate should be closed solely
because this guide or the verifier exists.

An earlier README replacement was not executed: automatic approval review
rejected the PowerShell/Python action `Path('README.md').write_text(<replacement README>,
encoding='utf8')`, stating only **"blocked by policy"**. README was left untouched;
the write was not retried or routed through another tool during that task. The
later owner-approved concise README is a separate update. This historical
rejection does not leave a current README blocker, and the guide/example alone
does not establish #81's complete acceptance.
