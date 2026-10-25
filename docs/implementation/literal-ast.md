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

Runtime location values and spans are preserved exactly. The generated type records location presence (`Location` for ordinary parsing and `None` with `no_location=True`); offset, token and source fields keep their ordinary mutable types. A literal `Location.start` would become false after legal mutation through a normal `Location` consumer, so it is not narrowed. The pinned gql.tada `parseDocument` type returns only `{ kind, definitions }` and omits `loc`; GraphQL Desk retains more safe location information without promising mutable offsets as literals.

`graphql_desk.parse_generation.render_parse_companion(sources)` accepts explicitly
supplied literal strings and returns runtime/stub text without writing files.
The runtime companion reexports the identical root `parse` function. Stub overloads
refine supplied executable literals in default parser mode using the existing AST
renderer. Dynamic strings, `Source` objects, SDL and nondefault options retain the
ordinary `DocumentNode` return. Sources deduplicate deterministically; invalid
syntax raises the original parser error, and unknown schema fields still parse.

This is a retained-source opt-in companion foundation for #25. Automatic typing
for ordinary `graphql_desk.parse` imports is tracked separately in #253. API-01
and the live-typing gate have not passed. Tests cover actual generated imports and exact positive/negative
bindings in both strict checkers, runtime identity, fallback/options, schema-independent
parsing, syntax errors, complex defaults, unsafe names and excessive nesting.

`source_graph.resolve_parse_sources(modules)` separately resolves captured default-mode
parser calls through existing import/reexport, static-string, provenance and mutation
rules. Its immutable facts are independent of schema-bound factory documents and
fragment indexes. Dynamic inputs, nondefault options and unsupported scopes retain
ordinary ASTs with local fallback diagnostics. No project modules are imported.

`render_captured_parse_companion(modules, *, project_id, module)` selects one unique
captured owner and returns its identity, opt-in companion and selected-call diagnostics.
Independent valid literals survive invalid parser syntax. Sources deduplicate
deterministically and remain schema-independent. Callers still own publication and
explicit companion imports; neither function rewrites original imports or writes
files. Explicit import attachment remains caller-owned; automatic import assistance
remains open under #253.

The trusted editor artifact response now includes these captured companions under
`<generated_package>.parsers.<source_module>` and publishes their stubs in the same managed
generation as the schema-bound factory stubs. That managed stub path serves Pylance;
the runtime companion must also be present in the configured generated package for Python
imports to work. Parser analysis remains available when
a project's schema is unavailable. Importing that generated module is recognized on
subsequent captures so its literal overloads stay current. The runtime companion
re-exports the exact `graphql_desk.parse` function. The editor does not rewrite the
user's source import; automatic import assistance and two-host Pylance verification
remain open under #253.

Captured parser syntax errors use the existing versioned source map to locate the
actual contributing Python literals, including imported constants, concatenation,
escapes, Unicode and EOF boundaries. Diagnostic module identities can therefore
differ from the selected call owner. Each call maps its own provenance even when
parsing is deduplicated by decoded source. Unavailable or unverifiable maps retain
an honest whole-call diagnostic; resource fallbacks also remain on the call.
This does not attach companions to original imports; automatic import assistance
remains open under #253.

Captured parser discovery also accepts direct ordinary/async function-body calls
when inputs are literals or proven stable module globals. Whole-function local
bindings and unsupported scopes remain conservative, using the same scope checks
as schema-bound calls. Explicit companion imports are still caller-owned; no
automatic import attachment or writes are introduced.
