# Interactive project setup

`graphql-desk init [dir]` configures an existing project directory, defaulting to
the current directory. The command is labelled **work in progress**, matching the
[pinned upstream initializer](https://github.com/0no-co/gql.tada/blob/cfe64f433bd29ff6f79e462596389b81c6e01aec/packages/cli-utils/src/commands/init/runner.ts).
It asks for a local SDL/introspection JSON schema or HTTP(S) URL and a generated
Python package directory. The default package directory is
`graphql_desk_generated`; a choice such as `api/generated` selects the Python
package `api.generated` and schema output `api/generated/default.pyi`.
The upstream [command entrypoint](https://github.com/0no-co/gql.tada/blob/cfe64f433bd29ff6f79e462596389b81c6e01aec/packages/cli-utils/src/commands/init/index.ts)
resolves a target and invokes the runner; neither creates missing project
directories. GraphQL Desk likewise requires the chosen project directory to exist.

The command previews the missing `[tool.graphql-desk]` keys and asks for explicit
confirmation before writing `pyproject.toml`. HTTP(S) URLs are redacted in the
preview, and unrelated configuration is never printed. Existing settings and
comments are preserved with the already-installed `tomlkit` dependency; valid
partial single-schema configuration receives only missing keys. A complete valid
configuration is unchanged and exits successfully without prompting. Existing
schema/package choices are retained rather than silently replaced.

Local schema choices are validated with the shared schema loader. URL choices
are validated as configuration without fetching them. Init never imports project
Python, installs dependencies, creates generated output directories, or changes
editor settings. It prints the next `graphql-desk generate-output` step and the
instruction to enable the GraphQL Desk VS Code extension. Generation performs
any configured remote schema request later.

Malformed, ambiguous or conflicting settings fail without an automatic edit and
show a minimal nonsecret proposal for manual review. Incomplete named-schema
configuration is treated as ambiguous rather than guessing a schema binding.
Read-only files, symlinked configuration/directory paths, invalid Python package
directory names and output paths outside the project are rejected. The shared
configuration and ownership validators check the resulting project and its
nearest configured ancestor. `--config` and `--schema` are not applicable to init;
its directory argument selects the configuration to create or extend.

Cancellation, end-of-input and interruption before publication leave the project
unchanged. Successful setup exits 0, configuration failures exit 1, invalid CLI
arguments exit 2, and cancellation exits 130. Prompts, previews and instructions
use stderr; stdout remains empty.

## Publishing and recovery

Init captures the selected directory's identity before prompting and rechecks it
and its non-symlink ancestors before staging, writing content, and publishing.
A directory replaced while the user confirms the edit cannot redirect staging
into another project.

Init reads the saved configuration as a snapshot of bytes, file identity,
ownership, mode, and, on Windows, its discretionary access-control list (DACL).
It stages the complete edit beside the target. Existing POSIX owner/group and
mode, or Windows DACL protection, are applied to the empty staging file **before**
writing configuration content;
failure to copy that security information aborts before publication. On Windows,
the original owner SID must equal the empty stage's owner before copying the DACL
or writing content. Different owners are refused rather than changing ownership
or obtaining additional privileges. Owner and DACL are captured together and
included in subsequent snapshot verification. The Windows
implementation uses supported handle-based `GetSecurityInfo`/`SetSecurityInfo`
calls. It preserves the DACL and its protected/inherited setting; it does not
claim to preserve auditing policy or arbitrary extended metadata.
On POSIX, failure to retain the original owner/group aborts before content is
written. Extended access ACLs are refused on both the original and empty staging
file rather than discarded. Their absence is rechecked when validating snapshots
and before writing content. Linux requires the descriptor-based
`system.posix_acl_access` lookup to report `ENODATA`; attribute presence and other
errors fail closed.

On macOS, descriptor-based capability checks first require nonopaque local
authorization. A filesystem without extended security has no effective VFS ACL.
For a capable filesystem, `acl_get_fd_np` must prove no ACL property or return an
empty ACL; an entry, unexpected result or read error is refused. The absence
interpretation follows Apple's [filesystem capability dispatch](https://github.com/apple-oss-distributions/xnu/blob/main/bsd/vfs/vfs_vnops.c)
and [ACL fallback/error propagation](https://github.com/apple-oss-distributions/xnu/blob/main/bsd/vfs/kpi_vfs.c),
plus the [ACL getter](https://github.com/apple-oss-distributions/Libc/blob/main/posix1e/acl_file.c)
and [entry iteration](https://github.com/apple-oss-distributions/Libc/blob/main/posix1e/acl_entry.c).
Opaque remote authorization and filesystems whose ACL absence cannot be
established are unsupported for automatic setup. No ACL-copying or privilege
management framework is introduced.

For an existing file, init moves the original to an owned recovery path and
rechecks its snapshot before exclusively publishing the complete staged file.
Fresh configuration also uses exclusive publication. The intended staged bytes,
identity and permissions are checked before moving the original, immediately
before publication, and against the published target before deleting recovery.
A competing file is never
overwritten by a fallback write. When restoration would conflict with a new
target, the competing target remains in place and the original is retained at a
reported `.pyproject.*.recovery` path. An edit through an already-open original
handle detected after publication similarly retains that original for recovery.
Successful publication removes the verified temporary and recovery files.
Changed or replaced staging files are retained and their paths reported rather
than deleting another writer's data.

There is a brief interval between moving the original and publishing the new
file when `pyproject.toml` is absent. This is a checked disk-snapshot protocol,
not a universal operating-system compare-and-swap or coordination with every
external writer. Init cannot inspect or save unsaved editor buffers; save or
reconcile unsaved configuration before running it. Recovery copies are retained
when needed to preserve user data, rather than deleted automatically.

## Verification

`tests/test_cli_init.py` exercises fresh and partial projects, actual schema and
output choices, preservation of comments and unrelated secrets, complete-project
idempotence, malformed/ambiguous input, cancellation, no installs/network/project
execution, symlink/read-only refusal, and competing edits before and during
publication, staged-file tampering, and confirmation-time directory replacement.
A real Windows test verifies equal owner SIDs and restrictive DACLs on the empty
staged file and final result. An injected other-SID owner descriptor verifies
refusal before DACL copying/content; it does not require changing a real file's
owner. A failure-injection test verifies no secret content is
written when permission copying fails. Conditional POSIX tests verify owner/group
on the empty staging file and published output, and refusal when ownership cannot
be preserved. Linux/macOS fixtures also verify refusal of an original access ACL
and an inherited staging ACL. Synthetic guard tests cover proven absence, opaque
authorization, ACL presence, unexpected/error returns and ACL-handle cleanup.
Native POSIX cases require POSIX CI; the Windows run does not execute them.
Existing generation and CLI tests cover
the unchanged default output-staging callers. Broader doctor/editor and parity
gates retain their own acceptance criteria.
