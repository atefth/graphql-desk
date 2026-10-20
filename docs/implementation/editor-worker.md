# Read-only editor worker

Launch the installed package with an explicitly selected absolute interpreter:
`python -I -X utf8 -m graphql_desk.editor_worker`. The trusted editor caller owns
workspace authorization, interpreter selection, process lifetime and cancellation.
The worker performs finite local acquisition and pure analysis; it does not execute
project Python, fetch remote schemas or write generated files.

Standard input/output carry UTF-8 NDJSON, one request/reply per line. Both request
and serialized reply, including newline, are limited to 8 MiB. Malformed, duplicate
key, nonfinite, invalid UTF-8 and oversized requests return only
`{"ok": false, "error": "editor-request-failed"}` and invalidate active state.
Oversized replies do the same. The reply limit does not bound peak analysis or
serialization memory; large-project responsiveness remains unmeasured. Standard
output contains protocol replies only. These errors do not echo source text,
schema contents or configuration credentials.

Every request has `op`, `workspace`, `candidates`, `buffers`, `documents` and
`editor_fallback`. Paths are absolute local paths. Candidates are finite Python
source paths; buffers contain `path`, `version`, `text` and `is_dirty` for captured
candidate sources and ancestor configurations. Document metadata contains `id`,
`path`, `version` and `is_dirty` for the caller's complete open local-document
inventory. Unrelated document text is rejected. Versions are nonnegative integers,
not booleans; conflicting canonical aliases reject.

The protocol has three operations:

- `prepare` uses the shared configuration preflight, returning `ok`, an opaque
  job `id`, `local_schemas` path facts and `required_buffers` document IDs. Missing
  files and unsupported schema suffixes remain visible. Remote schemas are not
  fetched. No source/schema contents are read during preparation.
- `analyze` repeats the same capture and adds `id` and `schema_buffers`. Schema
  buffers must exactly cover open documents matching configured local schemas,
  including external paths, with current text/version/dirty facts. Changed capture
  or preparation rejects. Success returns `ok`, `id`, the actual analyzer `request`
  and `response`, and `unavailable_schemas`; unavailable remote contexts remain
  explicit. A job can be analyzed only once.
- `current` repeats those inputs and checks the active snapshot through full
  reacquisition. It returns `ok: true` and a boolean `current` for the active job.
  Unknown/superseded IDs reject. False freshness invalidates state.

One worker owns one workspace and one active job. New preparation supersedes old
IDs; failure clears preparation, snapshots and source caches. Process restart
invalidates all IDs. Successful later jobs may reuse exact disk-source identities
and one caller-serialized `IncrementalResolver`; schemas and fragment indexes are
rebuilt. There is no persistent disk cache or performance acceptance claim.

The worker cannot prove that supplied document metadata is complete or current.
The editor must recapture it, invalidate on new/closed/changed buffers, and check
the latest job, cancellation, workspace epoch and trust after the final await
before publishing. Trust revocation/disposal must clear published results and
stop the process. Synchronous work cannot consume a cancellation message while
busy; the caller must terminate/restart it. This module supplies neither that
editor adapter nor production activation, automatic discovery or live typing.

Fourteen tests include a persistent isolated subprocess outside the repository,
Unicode pipes, dirty external SDL and missing buffered introspection, named schema
preflight, exact buffer metadata/canonical sets, source/config/disk changes,
supersession/restart, retained source versions, remote unavailability, redaction
and bounded malformed/oversized request/reply recovery. No broad acceptance gate
is passed by this transport foundation.
