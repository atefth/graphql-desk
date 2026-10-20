# Finite editor snapshot acquisition

`acquire_editor_snapshot(workspace=..., candidates=..., buffers=..., generation=...,
editor_fallback=...)` reads only caller-authorized local facts. The trusted caller
supplies finite Python file candidates, captured `OpenBuffer` records, and a new
nonnegative integer generation. It does not enumerate directories, import project
code, start workers, fetch schemas, write files, or publish editor state.

Existing effective-project selection, source eligibility, nested ownership and
factory binding helpers determine the exact request for the [analysis boundary](editor-analysis.md).
Open source/schema buffers override saved bytes. Source candidates must stay in
the workspace; explicitly configured local schema paths retain their existing
outside-workspace support. SDL and introspection JSON preserve their original
text, Unicode and line endings. Schema URLs are not fetched or serialized:
`unavailable_schemas` lists only project/name pairs and absent contexts remain
visible to analysis. Credentials are excluded from analyzer requests and reprs.

Configuration discovery currently reads saved configuration. A dirty, new or
different captured configuration buffer at a candidate ancestor fails closed;
unsaved configuration support requires the later shared overlay-aware reader.
An editor must supply every relevant candidate, including new unsaved source
paths, and all relevant captured configuration/schema buffers. This API cannot
prove that the caller's finite enumeration is complete.

The returned `EditorSnapshot.request` is a fresh JSON-shaped copy, backed by
private immutable observation hashes and configuration identity facts. It is
not a publication permit. Before publication call `is_editor_snapshot_current`
with newly captured candidates, buffers and fallback facts, then check the
caller's latest job/cancellation guard. Reacquisition checks source/config/schema
content and origin changes, buffer versions, ownership and candidate changes.
Closing a buffer changes its origin to disk and invalidates the old snapshot.

Reads are best-effort and non-atomic. End-of-read comparisons catch ordinary
changes but cannot alone detect a configuration changing A-to-B-to-A between
helper reads. The regression for that case demonstrates why full reacquisition
before publication is mandatory. Generation-based schema tokens conservatively
invalidate contexts on every acquisition; incremental caching and performance
acceptance are separate work.

Fifteen focused tests cover real acquisition-to-analysis, imports/fragments,
new source buffers, local schema overlays, nested/fallback projects, dirty/new
configuration rejection, redaction, exclusions, entrypoints, stale input origins,
Unicode/BOM/CRLF and symlink boundaries where the host permits them. No active
extension, production analysis-file adapter or broad editor/live-typing gate is
established by this foundation.
