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
tuples, and changed transitive dependencies. Generic fragment reader helpers and
factory attachment are separate work; issue #35 remains open.
