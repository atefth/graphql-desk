# Executable-document validation policy

`graphql_desk.validation.validate_document(schema, document, client_directives=())`
validates an existing executable AST without reading files or executing project
Python. It returns the original GraphQL errors as a tuple, preserving node/source
locations for later source mapping. The operation renderer uses this same policy.

The policy retains the standard GraphQL validation rules except unused-fragment
errors: standalone fragments and explicitly supplied unused fragment definitions
are valid inputs to these typing components. Variable-use and field/argument/type
validation still run. This is not a replacement for future unused/deprecated
selection diagnostics or severity policy.

Configured client directive names and the pinned built-in list suppress only
unknown-directive errors. If the schema defines a directive, its argument and
location requirements remain enforced. Names must be ordinary GraphQL identifiers
without `@`; they do not define execution behavior or fabricate field semantics.

Three tests cover unknown-only suppression, schema-authored directives, retained
variable validation, unused fragments and original AST/source identity. This
extracts the existing renderer policy; it does not establish shared editor/CLI
diagnostics, complete source mapping or any broad acceptance gate.

`document_validation.validate_indexed_document` validates a composed indexed
document against its supplied schema context and returns `GraphDiagnostic`
records. It selects definitions from each owner's original parsed AST, retaining
original source locations, and maps errors through verified Python literal
contributions. Imported fragments, adjacent strings and escapes retain their
actual host origins; printed definitions are used only to check index facts.
Locationless errors fall back to the owning call range.

The adapter checks registry ownership, schema/context identities and supplied
source-map snapshots. It trusts the caller's index and whole-snapshot freshness;
it does not discover sources, acquire current files or authorize publication.
Three focused tests cover imported split/escaped error locations, field/argument/
variable validation, directive policy and stale owner/source/context rejection.
Editor and CLI integration, severity policy and full diagnostic acceptance remain
open.
