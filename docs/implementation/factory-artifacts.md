# Retained-source factory artifacts

`generate_factory_artifacts(context, index, *, modules, project_id,
generated_package, scalars=(), enums=(), client_directives=(),
disable_masking=False)` returns immutable `FactoryArtifacts(files, diagnostics)`.
Each `GeneratedFile` has a relative path and text. This pure backend accepts one
schema's complete trusted index and snapshots. It does not write files, import
authoring modules, launch workers or refresh an editor.

`generate_project_factory_artifacts(config, contexts, index, *, modules,
project_id)` assembles all configured schemas into one immutable artifact set.
It reuses captured `ProjectConfig` settings and requires exactly one context per
configured schema. Each schema retains its scalar/enum mappings and masking
policy; client directives follow the shared project setting. No configuration or
schema is reloaded.

Private document modules are scoped as `<package>._documents.<schema>.dN` for both
entry points. Typed export facts combine aliases from different schemas into the
same `<package>.sources.<source_module>` companion. Same-basename source modules
retain their full package paths; conflicting normalized public aliases reject
rather than silently choosing a schema. Configured output, context, source/index
identity and package-parent failures reject atomically. An error-bearing index
still returns no files; this does not implement per-document recovery.

The generated `<package>.<schema>.graphql` runtime callable delegates to the
ordinary schema-bound constructor. Its stub correlates exact decoded source,
literal operation selection and fixed ordered tuples of nominal fragment document
views with result/variable declarations. Importing `graphql as gql` uses that same
binding. The schema module also exports schema aliases. Public source companions
follow `<package>.sources.<source_module>` and export normalized binding aliases
such as `BookQueryResult`, `BookQueryVariables` and `BookFieldsFragmentRef`.
Private per-document modules keep schema preludes separate without text slicing.

Runtime values stay ordinary `TypedDocument` carriers. Checker-only return views
attach the actual composed AST shape, selected operation, schema metadata and
fragment-owner identity. Exact AST declarations consume the real runtime AST,
including primary `@_unmask` removal; they do not reparse printed GraphQL or
construct runtime subclasses. Source companions never import the authoring modules.

Inline, adjacent, raw and escaped literals can select precise overloads. Tested
top-level `Final` and `Literal` sources also work in both pinned checkers.
Ordinary aliases, concatenations, nonliteral selectors and fragment lists receive
explicit precision diagnostics because their checker types can widen. Fixed
tuples carry exact fragment order and arity for precise overload selection.

The general `str` fallback exposes a private read-only protocol containing only
`ast`, `operation_name` and `schema_name`. It carries no trusted result/variable
generics and cannot satisfy a precise `TypedDocument` client or assignment. Both
checkers also accept incorrect known constructor arguments through this opaque
fallback; the constructor call itself is not statically rejected. Source
diagnostics remain required. Dynamic AST execution and untyped fragment
composition still use the original runtime carriers without wrappers or copies.
A source/index error prevents the artifact set from being returned. A discovered dynamic
source still produces the resolver's `source-not-static` diagnostic and no files;
the fallback test for external callers does not establish dynamic discovery.
These gaps keep #36 open.

Source maps verify decoded contributions against supplied snapshots before
emission. Schema/context/dependency mismatches, unsafe names, normalized alias or
file collisions, and ambiguous fragment signatures reject atomically. Identical
operation signatures can deduplicate, but identical fragment call signatures with
distinct owner identities cannot be distinguished by a literal overload and
produce a diagnostic. Invalid top-level generation arguments raise `ValueError`.
Precision diagnostics can accompany usable files; fatal indexed generation
errors return no files. A future publisher must enforce ownership, freshness and
batch recovery before writing any returned artifact.

The generated callable protocol also includes the [scalar member](scalar-helpers.md)
using the same schema declarations and configured input aliases once. The runtime
factory remains the ordinary schema-name binding.

Thirteen focused tests check unchanged original Python source with both strict
checkers and actual generated runtime imports, generic client inference and
negative variable/result cases, exact ASTs, explicit fallback limits, aliases,
source packages, diagnostics and stale/collision failures. Dynamic discovery,
persisted/reader helpers, writer integration,
incremental generation and the full editor/client matrix remain separate work.
No broad typing, API or live-typing gate is passed.
