# Decoded source maps

`graphql_desk.source_maps.build_source_map(call, modules)` verifies explicit source snapshots and returns a frozen map for one resolved call's decoded string. It checks project/module identities, URI/version dependencies, contributing literal spans and the final decoded text without executing project code. Snapshot versions are caller-owned and must change with source edits.

`to_host(Span)` returns separate `HostSpan` origins; `to_decoded(module, Span)` returns all matching decoded spans, including repeated uses of an imported constant. Native ranges are half-open Python codepoint offsets. Quotes, comments, concatenation operators and file gaps never become a single guessed host span. Ordinary text uses coalesced linear runs. Escapes and CRLF normalization are indivisible atoms: nonempty overlaps map to the entire opposite atom; empty ranges map only exact endpoints. Zero-output continuations relate both host boundaries to one decoded boundary. Interior escape cursors have no invented exact position.

`convert_offset`, `offset_to_position` and `position_to_offset` convert codepoint, UTF-8 and UTF-16 boundaries, rejecting split UTF-8 sequences and surrogate pairs. Positions use zero-based physical lines and characters; CR, LF and CRLF are recognized, while Unicode line-separator characters remain text. Position conversion also rejects CRLF interiors; encoding-only offset conversion permits that character boundary. UTF-8 conversion of unrepresentable surrogate text fails explicitly.

The literal scanner supports escaped/raw/triple/adjacent strings and uses Python's literal decoder only as a consistency check. Missing/stale snapshots, unsupported contributions or inconsistent decoded text raise errors. This is the mapping foundation for #29; read-only editor diagnostics, completion and definition/navigation providers consume these maps. Guarded edit application remains unimplemented. No edit planner is supplied: map-local occurrence counts cannot establish that changing a shared constant is safe across a project. SRC-01 and live acceptance remain open.

`build_source_map` accepts both schema-bound resolved calls and separate captured
parser-call facts. Both use the same version/dependency/contribution verification
and literal decoder; parser calls do not invent a schema or document identity.
Captured parser companions reuse these maps for syntax diagnostics, preserving
all legitimate EOF boundary origins and falling back to the call if provenance
cannot be verified.
