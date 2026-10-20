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

Configuration text is captured once for every candidate ancestor through the
workspace boundary. Open buffers override saved text, including dirty or new
configuration files. Both discovery and nearest source ownership consume this
same closed map: `None` records captured absence, while a missing key rejects
instead of reading disk later. The ordinary configuration parser still governs
whole-object precedence, inactive tables, errors and scope. Disk directories and
broken symlinks cannot masquerade as buffered configuration files.
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

An optional `previous` snapshot from the same workspace and no later generation
retains unchanged disk-source versions. Reuse requires identical project/module/
package identity, URI, canonical path, disk origin, raw bytes and decoded text.
Open buffers retain their actual editor versions. Without a baseline, disk
versions retain the acquisition generation. Freshness reacquisition uses the
snapshot itself as its baseline; private observations, configuration facts and
schema tokens still participate in full comparison. Request IDs remain specific
to the current generation, and the latest-job guard remains mandatory.

`prepare_editor_snapshot(...)` returns an immutable `EditorPreparation` with
`local_schemas` path facts before reading source/schema contents. It shares
configuration capture, ownership and selection with acquisition. Each fact gives
the project, schema name, validated resolved path and canonical identity; original
TOML path spelling is not retained. Missing files and unsupported suffixes remain
listed, so a caller can capture matching open schema buffers, including explicit
external paths. Remote origins are not fetched or included as local paths.

Pass `preparation=...` to acquisition to reject changed configuration observations,
private credential hashes, candidate identities or schema paths before content
reads. This does not verify the caller's open-document inventory: newly opened,
closed or changed schema buffers must still invalidate the editor job and enter
fresh acquisition/current checks. Invalid schema contents and unsupported formats
retain the existing analyzer/acquisition errors; preparation does not validate
schemas or permit publication. Worker transport remains separate work.

Actual `IncrementalResolver` counter tests prove unchanged-project reuse across
three later generations and recomputation of only a changed project. Schema/index
caching, worker integration and performance acceptance remain open.

Source/schema reads are best-effort and non-atomic. Configuration discovery and
ownership share one captured text set, preventing independent helper reads from
observing different configurations. End-of-read comparisons do not replace full
reacquisition and the latest-job guard before publication. Generation-based local schema tokens conservatively
invalidate contexts on every acquisition; incremental caching and performance
acceptance are separate work.

Explicit `RemoteSchemaSnapshot` records can supply introspection text from the
same captured configuration. Their private config/credential identity, version and
opaque token participate in freshness. Stale records remain unavailable and never
enter analyzer schema contexts. Synthetic untitled URIs avoid disclosing endpoint
or header values. Acquisition and current checks still perform no network I/O;
the worker's explicit refresh operation owns fetching and cancellation.

Twenty-nine focused acquisition tests cover real acquisition-to-analysis, imports/fragments,
new source buffers, local schema overlays, nested/fallback projects, dirty/new
configuration routing and malformed-file rejection, redaction, exclusions,
entrypoints, stale input origins, Unicode/BOM/CRLF and symlink boundaries where
the host permits them. Shared project/selector tests additionally exercise closed
map absence, missing keys, aliases, explicit scope and nested ownership. No active
extension, production analysis-file adapter or broad editor/live-typing gate is
established by this foundation.
