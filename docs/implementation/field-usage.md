# Same-file field consumption

The editor emits warning `graphql-desk.52005` when a query has observed local
consumption but selected leaf fields have no observed read. Unused leaves are
grouped at their nearest selected parent and mapped to the original Python string
field-name ranges. Root leaves use their own ranges. This is the bounded heuristic
tracked by #56, separate from project scan and the deletion actions in #57.

The backend reuses the resolved document identities, fragment index, source maps,
`graphql-core` AST/schema/validation APIs and Python's `ast` and signature binding.
It does not execute source modules or infer identity from a client's name or from
the structural similarity of two dictionaries.

## Provenance and supported reads

A result starts at a resolved GraphQL document passed to an indexed project
function with a visible `TypedDocument[R, V]` parameter and a return type tied to
the same type variable `R`. Direct and imported/aliased functions are supported,
including ordinary/async calls, optional results, awaitables, fixed tuples, and a
`Generic`/`Protocol` response whose typed `data` member exposes `R`. Calls must bind
the actual signature; expanded arguments and unresolved signatures are excluded.
The explicit `graphql_desk.testing.read_result` helper also establishes a result.

Within the same file the analyzer follows normal assignments, aliases, tuple
unpacking, response attributes and string subscripts, numeric list indices,
`for` loops and comprehensions, and nullable guards. Response aliases are path
keys; reserved-name matching uses the schema field name. Same-file
`read_fragment` aliases preserve matching document/schema provenance and track
the fragment's reads. Masked fragment-only subtrees are exempt from query
warnings. `@_unmask` and configured `disable_masking` expose their selections.

Passing, returning or storing a subtree in unknown code consumes that subtree.
Dynamic keys and unsupported method calls do the same. Opaque external client
signatures, arbitrary response wrappers, ambiguous control flow, deferred
callback captures, wildcard imports and reflection do not produce speculative
findings. Ordinary callbacks are escapes rather than interprocedural analysis.
An invalid Python/GraphQL document, mutation/subscription, or result with no local
consumption produces no field-use warning. Cross-file reads are not analyzed.

This bounded implementation analyzes module statements, top-level function
bodies, methods in module-scope classes, and methods in function-local classes
as independent scopes. Function-local class methods are analyzed only when
their operation and typed execution function resolve from module scope; methods
that capture the enclosing function's locals stay silent. It does not infer
class dispatch, inherited overrides, or data stored on instances. Class-suite
assignments are treated as escapes because a metaclass may retain assigned
values. Signature imports nested under a recognized `typing.TYPE_CHECKING` or
`typing_extensions.TYPE_CHECKING` guard are available to the same-file signature
resolver; the guard body is not treated as runtime code.
Container method escapes consume their contained results, and comprehension
assignment expressions invalidate enclosing bindings conservatively.
Python list literals consume contained results and do not retain callable or
document identities, since alias mutations can change their contents. GraphQL
result arrays still support indexed reads, iteration and comprehensions.

## Configuration and suppression

`track_field_usage = false` disables this heuristic for the owning project.
`reserved_keys = ["key", "createdAt"]` adds GraphQL field names to the built-in
`id`, `_id`, and `__typename` exemptions. Both settings participate in the
captured configuration and the analysis fingerprint.

A Python comment `# graphql-desk: ignore[52005]` on the document declaration's
opening line or immediately before it suppresses that document's findings,
including a multiline GraphQL string. The same marker on or immediately before
a reported field's physical source line suppresses that location. The marker
must be a Python comment, so identical text inside a string is not a suppression.

For example, reading `result["user"]["name"]` from a query selecting
`user { id name email }` warns at `user` about `user.email`. Passing
`result["user"]` to another function suppresses that subtree's warning.
Undeclared runtime side effects and uses outside the captured file can make this
heuristic incomplete; it does not prove dead code and supplies no deletion fix.

The focused corpus covers positive reads, reserved aliases, exact escaped/Unicode
source ranges, wrappers, imports, fragments, overwrites, shadowing, callback and
dynamic-access suppression, configuration propagation and the editor warning
severity. Broader performance and full parity gates remain separate acceptance.
