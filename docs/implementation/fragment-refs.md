# Shared fragment reference declarations

`fragment_refs.py` supplies checker-only declarations shared by generated factories
and masked results. `FragmentIdentity[Literal[key]]` gives each indexed fragment a
nominal identity. `FragmentDocument[Identity, Result, Variables]` refines the normal
typed document for overload selection; runtime factories still return the ordinary
carrier without changing its AST or identity.

`fragment_identity_key(owner, definition)` requires the fragment's owning indexed
document and one of its fragment facts. Its versioned digest includes the project's
schema identity and context token, source call identity, fragment name and type
condition, binding, and the owner's complete transitive dependency identities.
Use the fragment owner, never the consuming operation's dependency union. Changes
to an imported dependency therefore invalidate the fragment identity too.

`FragmentRef[Identity]` has a required read-only callable phantom key whose name
cannot be a GraphQL field alias. The callable is contravariant, so a reference with
both A and B capabilities can satisfy either reader; A alone cannot satisfy a
reader requiring both. An optional key would be unsound for closed raw dictionary
types and is deliberately not used.

The key is typing metadata. No runtime dictionary key is inserted, read, or checked;
lists, dictionaries, nulls, and document objects stay unchanged. These declarations
do not make constructing a reference dictionary or reading its phantom field valid
runtime operations. They are intended for generated result and reader signatures.

Tests run positive and negative fixtures through both pinned checkers, including
wrong identities, raw closed dictionaries, combined capabilities, exact fragment
tuples, and changed transitive dependencies. Issue #35 remains open for the complete
helper acceptance matrix.

Reader research remains incomplete. A pinned TypeScript comparison corrected an
initially stronger expectation: a different fragment reference with directly
selected compatible fields can legitimately satisfy the raw-result arm. A pure
unmatched reference is passed through upstream; it must not expose fragment-only
fields. Empty and conditional payloads need return-type and field-access checks,
not a blanket requirement that every such call reject.

The pinned helper also permits mutable alias deletion through an optional-field
view, leaving an original required-field annotation invalid. That is an upstream
aliasing limitation, consistent with the identity contract, not justification to
require closed raw shapes or readonly result views. Upstream array results
are readonly; Python reader containers use `Sequence`.

`read_fragment(document, data)` now supplies an identity reader for generated
fragment views. Each valid fragment binding also exports `read_<binding>` from
its generated source companion. `ReadableFragmentDocument[I, R, V, Input]` keeps
the existing three-parameter `FragmentDocument` base and adds a distinct read-only
input projection. Recursive read-only fields accept compatible mutable results
without changing the mutable result view. Generated bound helpers provide literal
context and typename narrowing; the root reader uses checker-only callable facts.
Neither function reads the phantom property at runtime.

This is bounded reader support. Nullable arrays retain the full result union and,
when needed, the absent alternative. Generic raw literals and contextual fallback
inputs can retain a wider result/absence union. Upstream unmatched-reference
passthrough is not yet exposed by these typed Python helpers. Required conditional
empty branches remain absent rather than inventing selected fields. These limits,
mask/result helpers and the full parity matrix keep the broad gate open. Mutable
output aliasing intentionally follows the pinned runtime identity behavior.
