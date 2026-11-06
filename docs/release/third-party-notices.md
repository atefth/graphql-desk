# Locked runtime dependencies and third-party notices

This is a reviewable inventory of the current locked runtime candidates for
GraphQL Desk 0.1.0. It is not a license grant for GraphQL Desk, a legal-clearance
opinion, or evidence of what a released wheel or VSIX contains. The owner selected
the [MIT license](../../LICENSE) for GraphQL Desk on 2026-11-06; dependencies keep
their own terms. Issue [#79](https://github.com/atefth/graphql-desk/issues/79)
and artifact acceptance [#78](https://github.com/atefth/graphql-desk/issues/78) remain open.

## Scope and provenance

[runtime-dependencies.json](runtime-dependencies.json) records four Python runtime
distributions and sixteen non-development npm package locations. Python entries
come from the exact requirements in `pyproject.toml` and `uv.lock`; these four
packages currently have no transitive runtime requirements. `tomli` is required
only when `python_full_version < '3.11'`; it is not a Python 3.11+ runtime
requirement. npm entries are every non-dev location in `editor/package-lock.json`,
including the `vscode-languageclient` minimatch override. They are candidates for
extension packaging, not an assertion that a VSIX already bundles all of them.

Each record includes the version, upstream SPDX metadata, locked source URL and
archive integrity, original license/notice paths and SHA256 of their actual
installed bytes. Python includes all locked wheel and source-archive hashes;
the hashes identify allowed archives, not a claim that every archive was
installed or independently downloaded and verified. npm integrity is the lock's
SRI value. Installed versions and license metadata matched the locks when this
inventory was prepared. The license/notice file contents were inspected locally;
no project code or package setup hook was executed to collect them.

Two installed tomli 2.5.0 wheels supplied the same license text with different
line endings. The CPython 3.13 Windows wheel (`cp313-cp313-win_amd64`, WHEEL
generator `setuptools (84.0.0)`) has 1,093 CRLF bytes, SHA256
`34b59b39e7ba577a1d86924218c71229b163d3bf3903aec784299f725c89d324`.
The existing Python 3.10 environment's pure wheel (`py3-none-any`, generator
`flit 4.1.0`) has 1,072 LF bytes, SHA256
`b80816b0d530b8accb4c2211783790984a6e3b61922c2b5ee92f3372ab2742fe`.
Both match their respective installed RECORD hash and size; neither records an
archive URL. Removing CR from CRLF produces exactly the other file. The checker
accepts only these two exact raw hashes, and verifies that the alternate hash is
the LF conversion of the preserved CRLF copy. It does not ignore other whitespace
or copyright changes. This installed RECORD/WHEEL evidence does not establish
which archive bytes were downloaded, or verify every locked wheel's contents.

The files below are unmodified copies, including copyright lines and upstream
third-party notices. Identical byte sequences share one preserved copy. The
local `.gitattributes` disables line-ending conversion for these copies, so their
SHA256 values remain meaningful. The full typing-extensions license/history is
retained rather than replaced with an SPDX label. Microsoft's shipped notice
files are retained too; their generic references to other licenses do not add
an unlisted dependency to the npm lock or change its SPDX metadata.

## Python runtime notices

| Distribution | Version | SPDX metadata | Preserved license/notice |
| --- | --- | --- | --- |
| graphql-core | 3.3.1 | MIT | [LICENSE](licenses/python-graphql-core-license.txt) |
| tomli | 2.5.0 | MIT | [LICENSE](licenses/python-tomli-license.txt) |
| tomlkit | 0.15.2 | MIT | [LICENSE](licenses/python-tomlkit-license.txt) |
| typing-extensions | 4.16.0 | PSF-2.0 | [LICENSE](licenses/python-typing-extensions-license.txt) |

## Editor runtime notices

| npm package | Version | SPDX metadata | Preserved license/notice |
| --- | --- | --- | --- |
| @isaacs/nonrecursive-glob-match | 1.0.3 | BlueOak-1.0.0 | [LICENSE.md](licenses/node--isaacs-nonrecursive-glob-match-license.md.txt) |
| balanced-match | 4.0.4 | MIT | [LICENSE.md](licenses/node-balanced-match-license.md.txt) |
| brace-expansion | 5.0.12 | MIT | [LICENSE](licenses/node-brace-expansion-license.txt) |
| debounce-promise | 3.1.2 | MIT | [LICENSE](licenses/node-debounce-promise-license.txt) |
| graphql | 16.12.0 | MIT | [LICENSE](licenses/node-graphql-license.txt) |
| graphql-language-service | 5.7.0 | MIT | [LICENSE](licenses/node-graphql-language-service-license.txt) |
| jsonc-parser | 3.3.1 | MIT | [LICENSE.md](licenses/node-jsonc-parser-license.md.txt) |
| minimatch | 11.0.1 | BlueOak-1.0.0 | [LICENSE.md](licenses/node-minimatch-license.md.txt) |
| nullthrows | 1.1.1 | MIT | [LICENSE](licenses/node-nullthrows-license.txt) |
| semver | 7.8.5 | ISC | [LICENSE](licenses/node-semver-license.txt) |
| vscode-jsonrpc | 9.0.3 | MIT | [License.txt](licenses/node-vscode-jsonrpc-license.txt.txt), [thirdpartynotices.txt](licenses/node-vscode-jsonrpc-thirdpartynotices.txt.txt) |
| vscode-languageclient | 10.1.2 | MIT | [License.txt](licenses/node-vscode-jsonrpc-license.txt.txt) |
| vscode-languageserver | 10.1.2 | MIT | [License.txt](licenses/node-vscode-jsonrpc-license.txt.txt), [thirdpartynotices.txt](licenses/node-vscode-languageserver-thirdpartynotices.txt.txt) |
| vscode-languageserver-protocol | 3.18.4 | MIT | [License.txt](licenses/node-vscode-jsonrpc-license.txt.txt), [thirdpartynotices.txt](licenses/node-vscode-languageserver-protocol-thirdpartynotices.txt.txt) |
| vscode-languageserver-textdocument | 1.0.15 | MIT | [License.txt](licenses/node-vscode-jsonrpc-license.txt.txt), [thirdpartynotices.txt](licenses/node-vscode-jsonrpc-thirdpartynotices.txt.txt) |
| vscode-languageserver-types | 3.18.4 | MIT | [License.txt](licenses/node-vscode-jsonrpc-license.txt.txt), [thirdpartynotices.txt](licenses/node-vscode-jsonrpc-thirdpartynotices.txt.txt) |

## Check and update the inventory

Run the existing checker after the normal pinned development environment is
installed:

```text
python -B scripts/check_runtime_licenses.py
```

It checks Python runtime pins/markers, the runtime projection of both locks,
the retained notice hashes and the active interpreter's Python license bytes.
An inactive conditional distribution is checked against the lock and retained
copy; run the same check on Python 3.10 to verify installed `tomli` as well.
Development-only lock changes do not invalidate this runtime inventory.

After the existing `npm ci --ignore-scripts` setup, also run:

```text
python -B scripts/check_runtime_licenses.py --installed-node
```

That additionally requires every listed npm package to be installed and checks
its name, version, license metadata and actual recorded notice bytes. Neither
mode downloads dependencies, writes notices, runs project code, or regenerates
this inventory. Missing files, new runtime packages, pin/integrity changes,
changed notices and an unreviewed transitive Python dependency fail the check.
A dependency update needs a fresh review of the lock and actual upstream notice
files, then explicit updates to this JSON and the preserved copies; do not edit
or summarize away an upstream copyright or license condition.

## Distribution boundary and remaining release checks

The Python wheel currently declares these Python dependencies; it does not
vendor them. Preserve the relevant notices when redistributing their code,
wheels or another bundled form. A future VSIX must include notices for the npm
code actually shipped. Under #78, inspect the final wheel/sdist/VSIX file lists,
verify the hashes of the artifacts actually distributed, check for unexpected
bundled code or missing notices, and test an isolated installation. This source
inventory alone does not establish that final-artifact coverage or integrity.

Hatchling/build, mypy, Pyright, Ruff, packaging, pip-audit, `gql` used by examples,
`@vscode/test-electron` and their development-only dependencies are not declared
GraphQL Desk runtime dependencies. They remain subject to their own licenses
when redistributed; this inventory does not authorize bundling them. Optional
saved-generator evaluation dependencies are separately locked in the example
and are not part of the core Python or editor runtime.

VS Code, Node/Python runtimes supplied by the host, the Python extension and
Pylance are installed and licensed separately. No Pylance binary or other editor
extension is included by this inventory. CI test downloads are not a product
redistribution inventory.

Existing dependency CI runs the Python license checker, hash-required
`pip-audit` on the exported locked requirements and the editor dependency audit.
Those checks are evidence for their exact run, not a permanent advisory-free
claim. Review any new advisory or license change before release; no findings
are waived here. Packaged workspace-trust, secret-redaction and adversarial
acceptance and final artifact checks remain required before #79 can close.
