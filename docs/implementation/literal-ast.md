# Literal executable AST declarations

`graphql_desk.literal_ast.render_literal_ast_types(source, *, name, no_location=False)` returns annotation-only `.pyi` declarations exposing `<name>Document`. It parses with graphql-core and refines frozen executable AST nodes: exact kinds, names, scalar/enum/operation values, tuple shapes, nesting and absent attributes. Operations, fragments, directives, variables/defaults, selection/type nodes and recursive values are supported. SDL definitions and unsafe declaration names reject; syntax errors retain parser locations. Parsing does not imply schema validity.

Private subclasses remain compatible with ordinary `Node`/`DocumentNode` consumers in both strict checkers. They are static views, not runtime classes: no import, construction or `isinstance` promise applies to generated refinements. Future generated bindings must preserve the original parser objects without wrappers or copying. Tests check real object identity and ordinary AST consumers.

`render_document_ast_types(document, *, name)` renders the same declarations
directly from an existing `DocumentNode`. It does not print, reparse, copy or
normalize nodes. This matters for composed runtime documents: removing a primary
`@_unmask` directive leaves an empty tuple, while reparsing its printed text would
produce `None`. The direct renderer preserves that distinction, definition order,
shared nodes and actual location presence. The source API parses then delegates
to this same renderer. Neither entry point mutates the supplied runtime AST.

Variable defaults and variable-directive arguments use const-required checker bases. The pinned parser actually returns ordinary list/object/directive classes there, while its annotations require corresponding `Const*` types. These refinements follow the real node attributes without claiming the runtime object changed class.

Location presence is precise (`Location` or `None`), but numeric offsets, tokens and source metadata keep ordinary mutable types. Narrowing mutable `Location.start` to a numeric literal is rejected by Pyright and becomes false after legal mutation through a normal `Location` consumer. This limitation remains explicit rather than publishing unsound precision.

`graphql_desk.parse_generation.render_parse_companion(sources)` accepts explicitly
supplied literal strings and returns runtime/stub text without writing files.
The runtime companion reexports the identical root `parse` function. Stub overloads
refine supplied executable literals in default parser mode using the existing AST
renderer. Dynamic strings, `Source` objects, SDL and nondefault options retain the
ordinary `DocumentNode` return. Sources deduplicate deterministically; invalid
syntax raises the original parser error, and unknown schema fields still parse.

This is a retained-source opt-in companion foundation for #25. Attachment to original imports, regeneration/editor publication and
complete exact-location acceptance remain open. API-01 and the live-typing gate
have not passed. Tests cover actual generated imports and exact positive/negative
bindings in both strict checkers, runtime identity, fallback/options, schema-independent
parsing, syntax errors, complex defaults, unsafe names and excessive nesting.

`source_graph.resolve_parse_sources(modules)` separately resolves captured default-mode
parser calls through existing import/reexport, static-string, provenance and mutation
rules. Its immutable facts are independent of schema-bound factory documents and
fragment indexes. Dynamic inputs, nondefault options and unsupported scopes retain
ordinary ASTs with local fallback diagnostics. No project modules are imported.

`render_captured_parse_companion(modules, *, project_id, module)` selects one unique
captured owner and returns its identity, opt-in companion and local diagnostics.
Independent valid literals survive invalid parser syntax. Sources deduplicate
deterministically and remain schema-independent. Callers still own publication and
explicit companion imports; neither function rewrites original imports or writes
files. Full automatic attachment and exact-location acceptance remain open.
