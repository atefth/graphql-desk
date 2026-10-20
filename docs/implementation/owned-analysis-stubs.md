# Owned analysis-stub primitive

The authorized managed-directory route permits generated analysis stubs inside the
trusted owning workspace. `createOwnedAnalysisStub(vscode, workspaceRoot, stubRoot,
moduleName, initialText)` creates a fresh single-module package in an existing,
explicitly selected stubPath root. It never adopts existing packages or previous
session leftovers, creates no settings, and leaves all other root entries intact.
This is a bounded primitive; production activation and generated artifact routing
are not connected yet.

The handle exposes `uri`, `publish(text, isCurrent)` and `cleanup()`. Publication is
serialized, capped at 1 MiB of UTF-8 stub text, and requires a synchronous callback
returning exactly `true`. It stages complete bytes and rechecks currentness, trust,
workspace ownership, ancestor/package/file identity, expected bytes and dirty
file/associated-untitled aliases before replacement. Observed foreign edits survive
failure rollback, including modifications during a failed write or fsync. Cleanup
invalidates queued publications immediately and removes only the matching unchanged
owned file and empty owned package. A foreign/dirty artifact is preserved with an
error. Restart collisions fail closed; automatic leftover recovery is unfinished.
The synchronous checks do not provide filesystem compare-and-swap against another
process changing paths between operating-system calls.

Native tests cover exclusive creation, stale work, serialization, links/path changes,
foreign bytes/inodes, dirty aliases, partial initialization, failure rollback and
teardown ordering. `npm run test:host -- 1.141.0 ownedAnalysis` (or `1.142.0`) uses the
shared isolated host runner and pinned official Python/Pylance extensions. The
workspace-local stubPath is configured and seeded before Python/Pylance activation,
following the successful research route. Both Windows hosts verify nullable types
and diagnostics, repair and cleanup back to saved types, preserved disk bytes,
Save All behavior, clean generated buffers and dirty user/generated buffers. They
also refuse creation over a pre-existing dirty associated untitled buffer.

These results do not establish startup with arbitrary missing modules, automatic
factory inference, source/dependency/schema lifecycle integration, restart recovery,
non-Windows host support or full #23/#24/#38/#42 acceptance. No broad gate passes.
