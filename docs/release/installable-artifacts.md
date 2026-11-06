# Installable artifact checks

Issue [#78](https://github.com/atefth/graphql-desk/issues/78) requires evidence from
the artifacts users install. This first slice checks the Python wheel and source
distribution. It does not approve a release, implement VSIX packaging, or establish
the complete platform support matrix. The repository owner's project-license
decision is still required.

## Maintained package inputs

Hatchling 1.32.4 remains the build backend. `project.license-files` includes the
[third-party notices](third-party-notices.md),
[candidate dependency inventory](runtime-dependencies.json), and their verbatim
license/notice files using Hatch's native PEP 639 handling. This adds no license
expression or license grant for GraphQL Desk. The inventory also describes Node
candidates; including their notices does not bundle those packages in the Python
wheel. Wheel notice paths preserve the relative links between these documents.

The wheel contains only the maintained `graphql_desk` package, including
`py.typed`, and its distribution metadata, console entry point, notices and
`RECORD`. Dependencies are installed separately from the locked requirements.
Tests, editor sources, installed extensions, caches, profiles and private fixtures
are not wheel contents.

The source distribution deliberately contains a minimal, buildable source set:

| Root | Purpose |
| --- | --- |
| `src/graphql_desk` | Maintained package sources and type marker |
| `docs/release` | Release documentation, inventory and required notices |
| `pyproject.toml` | Build and package metadata |
| `README.md` | Package description |

The staged build also creates exactly two empty regular `.gitignore` and
`.hgignore` files with exclusive creation. Hatch searches for these files up its
parent directories and automatically includes whichever it finds; the local
empty files stop that search before repository or ancestor data becomes an
input. They are purpose-built build configuration metadata, not copies of
tracked project or private ignore files. The source archive requires both with
exactly zero bytes; missing or nonempty controls fail inspection. They are absent
from the wheel. This adds no VCS repository, build hook or archive packer.

Tests, general research/audits, editor development sources and development
environments stay in the repository rather than this source archive. This is an
explicit source-package policy, not a claim that ordinary open source projects
must exclude tests or documentation.

`scripts/check_distributions.py` obtains regular tracked files from exactly those
roots and copies their current bytes into a fresh, repository-contained build
directory. Unresolved index entries, Git symlinks, filesystem links/reparse
points and known cache/private directories are refused. Untracked project files are
never inputs, even if they sit inside `src/graphql_desk`. Hatch's explicit source
inclusion policy also prevents unrelated repository roots from entering a direct
build; use the staged checker for protection against untracked files within the
selected roots. Build outputs are outside the staged inputs, so they cannot be
included recursively. The existing `dist` directory is not read or overwritten.

## Offline build and clean installation

Provision the existing locked development environment first, with a
repository-owned `UV_CACHE_DIR` already configured. The checker exports hashed
runtime requirements from that same lock into its own fresh temporary directory.
A Windows replay, after selecting the repository as the current directory, is:

```powershell
$env:UV_CACHE_DIR = Join-Path (Get-Location) '.graphql-desk-local/cache/uv'
$env:TEMP = Join-Path (Get-Location) '.tmp'
$env:TMP = $env:TEMP
$env:UV_PYTHON_DOWNLOADS = 'never'
New-Item -ItemType Directory -Force -Path $env:UV_CACHE_DIR, $env:TEMP | Out-Null
uv sync --locked
./.venv/Scripts/python.exe -B scripts/check_distributions.py
```

On POSIX, set the same absolute cache/temp variables before `uv sync` and invoke
`.venv/bin/python -B scripts/check_distributions.py`. Use an already installed
supported interpreter. Provisioning may need network access. Every build,
environment creation and installation performed by the checker passes
`--offline`; Python downloads are disabled. The checker refuses a missing,
external or redirected cache. It preserves the inherited provisioned cache,
rather than substituting an empty cache and claiming offline success. Missing
cached dependencies cause failure; there is no download or wheelhouse fallback.

The checker performs these steps:

1. Export locked, non-development runtime requirements offline into its fresh
   task directory with official `uv export`, then run the existing runtime-license
   inventory check. A leftover repository-root `runtime-requirements.txt` is
   neither trusted nor modified.
2. Build a source archive and an independent wheel directly from the staged
   sources with explicit official `uv build --sdist` and `uv build --wheel`, the
   installed pinned Hatch backend and build isolation disabled. The default
   combined build would instead build its first wheel from the source archive.
3. Inspect actual archive members and bytes without importing or extracting them.
   Require exact filenames, source/notices, metadata, runtime requirements,
   `py.typed`, entry points, wheel tags, and every SHA-256/size in `RECORD`.
   Reject unexpected, duplicate, traversing and linked members. Before reading a
   payload, require its declared source/notice size to match the known input;
   bound generated metadata by the known text and member count, with small fixed
   format overhead. Reads are limited to the declared allowance plus one byte.
4. Rebuild a second wheel from the verified source archive with the same official
   tooling, then inspect that wheel too.
5. Create two fresh environments, install hashed locked runtime dependencies and
   each wheel offline, and run `uv pip check`. Check the installed package path
   and type marker with isolated Python, then run both the installed console
   command and module help. No checkout import or editable install counts.

The private official export contains the full locked runtime dependency closure.
Its installation uses `--no-deps` to install that export directly instead of
resolving the dependency graph again; `--require-hashes` remains enabled and the
subsequent `uv pip check` verifies the installed dependencies.

Child processes use task-contained temp directories and the existing uv cache;
`PYTHONPATH`/`PYTHONHOME` are removed and bytecode writes disabled. The checker
removes only its freshly created task directory after success or failure. It
checks the task root's identity and rejects link/reparse ancestors before
cleanup; an unverifiable or replaced root is preserved and reported. This is
not an operating-system guarantee against arbitrary concurrent filesystem or
tool changes. Platform/tool components can still write outside configured paths.
The shared provisioned cache is intentionally retained for reuse.

CI sets the same repository-owned cache before `uv sync`, then separately primes
registry metadata with an online, hash-checked reinstall of the complete locked
runtime export into the disposable CI environment. The checker makes its own
fresh private export; its archive builds and proof installations remain offline.
Standalone builds into `dist` are unnecessary. Focused unit tests use synthetic
archives and mocked official commands; passing them alone is not offline-install
evidence.

## Remaining release acceptance

The local Windows replay with CPython 3.13.7 and uv 0.8.19 passed against reviewed candidate
tree `64a04a8911193abb881a769679a7a9b503c0070e`. It built an independent direct
wheel and source archive with Hatch, rebuilt the second wheel from that archive,
passed the strict contents/bytes/metadata/notices/`RECORD` checks including the two
empty source controls, and installed both wheels into fresh environments offline.
Both installed dependency checks, isolated imports, console help and module help
passed. Cleanup left no new `distribution-check-*` directories. The local log is
`.tmp/issue78-distributions/official-replay-closed-export-20261106.log`.

The three active hash-pinned runtime dependencies were provisioned once online
into a separate task-owned temporary target before this replay, using the same
repository cache. That provisioning is outside the offline claim; both proof
installations afterward were offline. Both wheels had SHA-256
`6b6dc7b5d3cdcb37e4ad5f24ded2b841e98795426354ee84710010807c2dc374`.
This establishes equality within this local run. Cross-platform reproducibility
remains unverified. Hosted validation of the final PR tree, including this evidence
update, is recorded in that PR's exact-head checks; broader platform acceptance
remains open.

Existing local `dist` outputs were stale and are not release evidence: one wheel
declared an older GraphQL-core pin and lacked tomlkit; an old source archive
included `.venv-alt`. They were preserved rather than reused or overwritten.

VSIX creation still needs official `@vscode/vsce` tooling, an explicit packaged
file/runtime closure, notice inspection, and a clean-profile install/activation
of that actual archive. No custom ZIP packer or globally installed extensions
count. Python, Pylance and VS Code are separately installed tools, not bundled
dependencies. Project licensing and publication remain owner-controlled.

The supported contract targets CPython 3.10–3.14, VS Code 1.141/1.142, Windows 11
x64, Ubuntu 24.04 x64 and macOS 14 arm64. Current CI's Windows Server 2025 and
macOS 15 lanes do not by themselves establish Windows 11/macOS 14 acceptance;
remote SSH, WSL and container path/URI evidence also remains outstanding. For
packaged-editor checks, updates must be disabled in the fresh profile's user
settings using the installed schema (`extensions.autoUpdate: "off"` in 1.142),
and all four tool versions verified again at the end. Existing older host modes
that only check initial versions do not establish that final-version guarantee.

The application source-omission checker in
`src/graphql_desk/release_artifacts.py` remains a separate workflow for application
wheels. It is unchanged by the core-package archive checks described here.
