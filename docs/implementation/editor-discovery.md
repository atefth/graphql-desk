# Editor discovery host probe

`editor/test/discovery.host.cjs` is an isolated measurement suite, not production
activation or an analysis adapter. The host runner supplies a fresh disposable
workspace, result path and absolute trusted Python interpreter. Run each pinned
host with `npm run test:host -- 1.141.0 discovery` and
`npm run test:host -- 1.142.0 discovery`. Successful runs write
`read-only-discovery-observed`; syntax checks alone provide no host evidence.

The suite creates its own repository and workspace settings. It compares
`workspace.findFiles(new RelativePattern(root, "**/*.py"), null)` with the default
exclusion argument, with local `.gitignore` and `.ignore`, `files.exclude`,
`search.exclude`, and ignore-file opt-in enabled. It requires the null-exclusion
query to retain every explicitly listed disk fixture, including hidden, nested,
untracked and Unicode paths. The default query must honor the fixture's file and
ignore exclusions while retaining its search-excluded file. These are finite
fixture assertions, not a proof that every possible workspace file is enumerated.

The behavior being measured corresponds to the public
[findFiles API](https://github.com/microsoft/vscode/blob/1.142.0/src/vscode-dts/vscode.d.ts)
and the pinned host's
[extension-host translation](https://github.com/microsoft/vscode/blob/1.142.0/src/vs/workbench/api/common/extHostWorkspace.ts):
the null exclusion argument disables file exclusions and local ignore-file use.
The suite records create events from an actual watcher, including a watcher-excluded
fixture, but asserts neither event completeness nor absence. Watchers alone do not
establish a complete candidate inventory.

A separately created untitled document has an explicit caller-known association
with a nonexistent file path. It remains dirty and absent from disk. The finder
must not return that path; the suite adds it explicitly to the finite candidate
set and passes its actual text and version as `OpenBuffer`. This does not assign
module identities to arbitrary untitled documents. The real Python snapshot
acquirer must retain that buffer's content and version without saving it.

The real core selector also receives the disk candidates. VS Code ignore settings
do not become GraphQL Desk policy: the ignored and editor-excluded Python fixtures
remain selected. The configured `.hidden/**` exclusion independently removes the
hidden fixture. Source fixtures raise if executed; the suite only reads/parses
them through the installed package, launched with isolated Python and UTF-8 pipes.
It verifies their contents remain unchanged.

Where file symlinks are permitted, a nested fixture project verifies canonical
deduplication inside its configured source root and rejection of a symlink escaping
that root. Permission/platform failures are recorded as a skip, never a pass. The
targets remain inside the disposable workspace. The suite does not infer backend
symlink traversal policy from these selector checks.

No production providers, worker, generated declarations or analysis-directory
adapter are installed by this probe. The only writes are owned test fixtures,
isolated workspace settings, local Git metadata and the observations artifact.
The intentionally dirty suite-owned untitled buffer is left to isolated-host
teardown.

Both pinned hosts passed this suite with the official pinned extensions. Reviewed
observations are in `editor/test/observations/discovery-1.141.0.json` and
`discovery-1.142.0.json`. Each returned all ten listed disk fixtures with null
exclusions, retained the explicitly added unsaved buffer without saving it, and
passed both symlink checks. Watcher event lists are observations only. These
finite results support this candidate-query choice on those hosts; production
enumeration, incremental invalidation and full discovery acceptance remain open.
