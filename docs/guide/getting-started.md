# Getting started from source

GraphQL Desk is an unreleased development project. The source tree contains a
Python package and a private VS Code development extension. There is no published
wheel, Marketplace install, complete support-matrix result, or full-parity claim
implied by these instructions. The owner selected the [MIT license](../../LICENSE)
on 2026-11-06; release acceptance remains separate.

## Install the locked development environment

Use CPython 3.10–3.14 and the repository's pinned uv version, 0.13.2:

```console
git clone https://github.com/atefth/graphql-desk.git
cd graphql-desk
uv sync --locked
uv run python -m graphql_desk --help
uv run python examples/typed_client/verify.py
```

Provide an existing Node.js 22 development installation on `PATH` for the verifier
and editor development. The verifier invokes the installed Pyright bundle with
that Node binary and refuses to provision a missing runtime. Node is not a new
Python package runtime dependency.

The last command requires the existing locked development dependencies, including
the example-only gql client, checkers and wheel builder. It installs nothing. It
copies the fixture and the maintained `scripts/client_bindings.py` adapter into a
disposable directory under this checkout's `.tmp/worked-example`, generates types,
runs real local executions and both checkers, then builds/verifies a persisted
wheel. Tool caches and temporary output are kept in that directory. It does not
launch VS Code, contact a GraphQL server, or prove an installed-editor gate.

For another environment, install this source checkout into the interpreter you
intend to use, for example `python -m pip install -e .`. That installs the Python
package and CLI; it does not install or activate the VS Code extension. Keep the
lock and [support contract](../contracts/support-performance-and-evidence.md) in
view when choosing dependencies. No automatic Ariadne/Turms backend is involved.

## Configure your project

Start with a local SDL file and this table in the project's `pyproject.toml`:

```toml
[tool.graphql-desk]
schema = "schema.graphql"
schema_output = "graphql_desk_generated/default.pyi"
```

From that directory, explicitly generate the package even when stdout is piped:

```console
graphql-desk generate-output --output graphql_desk_generated/default.pyi
graphql-desk check
graphql-desk doctor
```

`doctor` intentionally exits nonzero when it cannot establish editor, checker,
generated ownership or bridge health. Its terminal report is not evidence of an
attached editor. [Configuration and CLI](configuration-and-cli.md) explains schema
selection, remote access, outputs and degraded reports. Interactive
`graphql-desk init [directory]` is labelled **work in progress**; it previews a
configuration change and asks before writing. It installs no dependencies.

Author operations with the generated binding:

```python
from graphql_desk_generated.default import graphql

greeting = graphql("query Greeting { greeting }")
```

Generate again after a saved source edit, or use the development editor bridge
for unsaved edits. The corresponding aliases are in
`graphql_desk_generated.sources.<source_module>`. Generated source companions do
not replace the original application module. See the
[same-source example](../../examples/typed_client/authoring/operations.py).

## Run the development editor extension

Install GraphQL Desk into the Python environment selected by VS Code's Python
extension. Provision the JavaScript dependencies from the checkout:

```console
cd editor
npm ci --ignore-scripts
```

Launch a VS Code development window with `--extensionDevelopmentPath` pointing to
the absolute path of that `editor` directory, and open the configured Python
workspace. This is a source-development workflow, not a packaged VSIX installer.
The extension requires a desktop workspace extension host. Install the Python and
Pylance extensions through their normal distribution; GraphQL Desk does not bundle
them. The [editor implementation notes](../implementation/editor-providers.md)
describe the pinned host checks and reproduction harness.

A blank `graphqlDesk.pythonPath` uses the Python extension's selected executable.
An override must be an absolute interpreter path with GraphQL Desk installed.
The extension uses a managed `.graphql-desk` workspace directory for generated
typing. When it owns the setting, it can manage `python.analysis.stubPath`, or a
project Pyright configuration that takes precedence, while preserving unrelated
settings and prior custom typing roots. Foreign or changed files cause a conflict;
do not edit generated artifacts or clear recovery state to force a successful
report. See [owned-output and recovery notes](../implementation/owned-analysis-stubs.md).

Trust is required for interpreter processes, managed writes and remote schema
requests. Untrusted workspaces have limited lexical support. Typing alone does not
fetch a remote schema by default; use **GraphQL Desk: Refresh Schema**. A positive
`schema_refresh_interval` explicitly enables trusted polling.

Run **GraphQL Desk: Doctor** for the active host's report. A selected interpreter
or an active Pylance extension alone does not prove that fresh generated types
were consumed. Unknown ownership or bridge verification remains degraded. Full
lifecycle, reader-retention and release gates remain open; use the
[compatibility guide](api-and-compatibility.md) before relying on this for a team.
