# Python call extraction primitive

`from graphql_desk.source import extract_calls` exposes `extract_calls(source: str) -> ExtractionResult`. Frozen result records contain calls and diagnostics. Each call has decoded `source`, optional literal `operation`, and `call_range` / `literal_range` with zero-based Python character offsets and exclusive ends. Adjacent literal tokens share an expression range; decoded-character-to-host maps are not implemented.

The initial slice recognizes direct `from graphql_desk import graphql/gql` imports with aliases and module-qualified calls. Ordinary/raw/triple/adjacent string literals and positional or `source=` arguments are supported, along with absent/empty fragment sequences and literal operation selectors. Python is parsed and compiled for syntax/scope validity; the code object is never executed, and imports are never performed.

Recognized calls with dynamic source, arguments, fragments or selectors produce actionable diagnostics instead of successful calls. Invalid Python or excessive nesting returns a diagnostic without partial successful extraction. This function discovers source candidates; it does not parse/validate their GraphQL or prove that a runtime factory exists.

Resolution is deliberately conservative: rebound, conditionally imported and ambiguous names are excluded throughout their scope, including calls before a later rebind. Nested attribute mutation can exclude same-named aliases from unrelated scopes. Classes, lambdas, comprehensions, generic-function/type-alias scopes and other unsupported binding forms are not approximated as trusted calls; candidate calls in excluded scopes can receive conservative unsupported-scope diagnostics. This is not a full Python binding/effects analysis.

Cross-module reexports/constants, alias expressions, literal `+` chains, generated schema factories, fragment resolution, exact decoded-character maps and incomplete-buffer highlighting remain required future slices. See the [full source contract](../contracts/api-and-types.md#accepted-authoring-and-binding). No SRC/API/editor/live-typing gate is passed by this recognizer.
