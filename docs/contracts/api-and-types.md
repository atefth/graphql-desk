# Python API, source and typing contract

Requirements contract for PLAN-01 and PLAN-05. Production foundations are tracked in [feature status](../feature-status.md); no broad acceptance gate has passed. Full upstream parity remains the target. Names below are adopted defaults, not requests for a new product decision. These contracts reach runtime exports, generated schema/operation declarations, editor source binding, CLI discovery, client integration documentation and positive/negative checker fixtures.

## Source evidence

Pinned `gql.tada@1.11.4`: [root exports](https://github.com/0no-co/gql.tada/blob/cfe64f433bd29ff6f79e462596389b81c6e01aec/src/index.ts), [API and overloads](https://github.com/0no-co/gql.tada/blob/cfe64f433bd29ff6f79e462596389b81c6e01aec/src/api.ts), [selection semantics](https://github.com/0no-co/gql.tada/blob/cfe64f433bd29ff6f79e462596389b81c6e01aec/src/selection.ts), [variables](https://github.com/0no-co/gql.tada/blob/cfe64f433bd29ff6f79e462596389b81c6e01aec/src/variables.ts), [schema/scalars](https://github.com/0no-co/gql.tada/blob/cfe64f433bd29ff6f79e462596389b81c6e01aec/src/introspection.ts), [fragment markers](https://github.com/0no-co/gql.tada/blob/cfe64f433bd29ff6f79e462596389b81c6e01aec/src/namespace.ts), and [testing helpers](https://github.com/0no-co/gql.tada/blob/cfe64f433bd29ff6f79e462596389b81c6e01aec/src/testing.ts).

Verified locally with `git show` at that commit: root/testing exports and identity implementations. Compared the pinned commit against the local checkout for API, selection, variables, introspection and namespace: no differences. The checkout HEAD itself was `8bfc793f7d85c4b20ae49334df16468acb89717c`; HEAD alone is not the evidence pin. No upstream tests, package tarball tests or Python product fixtures were executed here.

## Concrete public surface

Use package `graphql_desk`, snake-case Python helpers, keyword-only schema/operation selectors and standard GraphQL AST values. Do not add a second execution client or serializers. A schema binding is generated from project configuration, not inferred by executing Python configuration code.

| Upstream export / member | Python contract and rationale |
|---|---|
| `graphql(input, fragments?)` | `graphql(source: str, fragments=(), *, operation: str \| None = None)` constructs a document. Precise typing requires importing the generated schema-bound callable described below; the root callable is the ordinary untyped baseline. `gql` is an import alias of the same binding. |
| `initGraphQLTada`, its type; `GraphQLTadaAPI` | `init_graphql(schema: str)` returns a schema-bound callable with the same call/scalar/persisted surface; schema name must resolve statically for generated typing. Default `graphql` binds the configured default schema. |
| `parse`, `parseDocument` | `parse(source)` returns the GraphQL AST. Generated exact literal AST declarations preserve definition/node kinds, names, literals, directives, locations where present and nesting; dynamic strings return ordinary AST types. Parsing alone does not imply schema validity. |
| `graphql.scalar` | `graphql.scalar(name, value)` checks the configured scalar/enum/input-object type statically, returns the identical value, performs no serialization/validation. Generated scalar aliases replace TypeScript's type-only `ReturnType`/generic instantiation use; do not fabricate a runtime scalar value or add a value-omission overload. Explicit `None` remains `None`. |
| `graphql.persisted` | `graphql.persisted(id, document)` retains typed result/variables; omitted-document use imports a generated typed persisted binding. See persisted/build contract; arbitrary ID alone never invents an operation type. |
| `TadaDocumentNode`, `DocumentDecoration` | `TypedDocument[Result, Variables]`: AST plus static phantom result/variables metadata; no result wrapper or runtime response coercion. |
| `TadaPersistedDocumentNode` | `PersistedDocument[Result, Variables]`: document ID, optional retained definitions, identical result/variables metadata. |
| `ResultOf`, `VariablesOf`, `FragmentOf` | Deterministic generated aliases `<Binding>Result`, `<Binding>Variables`, `<Binding>FragmentRef` in the source module's generated companion namespace. Python cannot write TypeScript's `typeof value` in a generic type expression; do not promise unsupported `ResultOf[query]` syntax. Generic client inference uses `TypedDocument[R, V]`; annotations use these importable aliases. |
| `readFragment` | `read_fragment(document, data)` plus generated fragment-bound `read_fragment` overloads for type-only/single-argument equivalent. Exact identity return. |
| `maskFragments`, `unsafe_readResult` | Root and `graphql_desk.testing` exports `mask_fragments`, `unsafe_read_result`; exact identity return. |
| testing `readResult` | `graphql_desk.testing.read_result(document, data, fragments)`; recursively checks supplied fragment closure in types, exact identity at runtime. |
| `setupSchema`, `AbstractSetupSchema` | Declarative schema/scalar/masking configuration and generated schema binding types; no TypeScript declaration merging syntax in Python. |
| `setupCache`, `AbstractSetupCache` | Documented optional generated cache format/configuration; cache never required for correct inference. |
| `$tada`, `__mapType` | `graphql_desk.internal` documented advanced marker/schema-map typing surface; generated opaque fragment markers and schema-map types preserve user-visible helper behavior without runtime keys or a public stability promise. |

CLI, internal utility and editor subpath inventories remain in the source audit and project-workflow contract; this table maps the complete root type/value surface and testing helpers, not a replacement for those inventories.

The generated namespace path is `<configured_generated_package>.sources.<source_module>`; exported binding `book_query` yields `BookQueryResult`, `BookQueryVariables`; fragment `book_fields` yields `BookFieldsResult`, `BookFieldsVariables`, `BookFieldsFragmentRef`. Resolve normalized-name collisions with a diagnostic requiring distinct binding names; never silently overwrite. A schema binding exports enum/scalar/input aliases too. Stable aliases survive whitespace/line-number edits. Local expressions without a module binding still infer inline types but cannot export a stable named alias until bound.

### How the same Python file acquires live inferred types

Default generated package: `graphql_desk_generated`, rooted at the project root and importable under the project's normal Python search path. The precise authoring import is `from graphql_desk_generated.<schema> import graphql`. Its generated `.pyi` declares an overload per discovered `(decoded source literal, selected operation, schema, fragment signature)` with `Literal[...]` parameters and a return `TypedDocument[BookQueryResult, BookQueryVariables]`. A corresponding generated runtime module exports the normal schema-bound implementation; declaration-only names must never break runtime imports. Fragment arguments participate in the overload signature through their generated document types. Different fragment composition cannot share a falsely precise overload. A general `str` fallback returns the untyped baseline, visibly diagnosed by GraphQL Desk when exact typing is required.

For `book_query = graphql("query Book { book { id } }")`, ordinary Pyright call overload resolution reads the generated import's stub, matches the exact literal overload, assigns its return type to `book_query`, and propagates its generic parameters into client calls in that **same original source module**. Generated companion aliases support annotations; they do not themselves cause this inference. On unsaved source edits the editor updates that generated schema factory stub in memory; dependent import/call refresh must pass the lifecycle gate. No arbitrary user-module stub replacement, private checker API, or checker understanding of GraphQL is assumed.

Root `graphql_desk.graphql`, dynamically selected `init_graphql(...)` callables, and factories without generated overloads remain valid runtime constructors but carry no precise operation inference. Editor/CLI diagnostics explain the generated import needed to opt into the core typed workflow. Schema initialization is recognized for discovery/generation; the documented typed equivalent is its generated factory import. Core acceptance fixtures use generated factories, including multi-schema projects, fragment imports and client inference. Static aliases/concatenations that the extractor can resolve but Pyright widens from a literal must get a precise actionable diagnostic (inline the literal or annotate a constant with the exact `Literal[...]`); do not claim an extracted value automatically remains a checker literal. Require tests for inferred `Final`/`Literal`, literal concatenation, raw/escaped strings and operation keyword matching. Whether all intended source forms retain overload inference and refresh reliably is an implementation acceptance gate, not established by this mechanism specification.

## Accepted authoring and binding

Required: ordinary/raw single/triple quoted strings, adjacent literal concatenation, parenthesized literals, Python escapes and Unicode with exact host/source offset mapping; positional or `source=` first argument; imported `graphql`/`gql`, aliased imports, module-qualified calls, and statically resolved re-exports/schema-bound callables. Names are resolved through imports and assignments, not by spelling alone. A shadowed local `graphql` must not be indexed as GraphQL Desk. Embedded syntax highlighting uses the same resolved call ranges; incomplete buffers may use a conservative lexical candidate until semantic resolution resumes.

Also accept statically resolvable module-level constant string aliases and literal-only `+` chains, with cycle detection and navigation back to each contributing span. Accept literal list/tuple fragment arguments and imported fragment bindings, including transitive composition. No evaluation of arbitrary Python. Dynamic f-strings (including interpolation), `%`, `.format`, function results, environment reads, mutation-dependent aliases, star-expanded fragments and unresolved imports receive an actionable source-not-static diagnostic. Plain f-strings without expressions may be treated as literals. Runtime parsing can still accept a `str`, but the operation is explicitly untyped and cannot pass the precise-typing parity fixture; never silently return `Any` as if inferred.

One operation permits implicit selection, including an anonymous operation. Multiple named operations require literal `operation="Name"`; generated types and client `operation_name` use that operation only, while fragment closure remains shared. Anonymous-plus-other operations, unknown selected names, duplicate operation/fragment names, wrong schema fragment bindings and fragment cycles are errors. Upstream type extraction uses definition zero; Python's explicit multi-operation selector prevents applying that type to a different executed operation. A fragment-only document must have one explicit primary fragment (first definition) and any dependent definitions; no operation selector. Unknown spreads cannot become silently trusted markers: preserve an unresolved reference diagnostic until composition is supplied.

Acceptance SRC/API: one positive fixture per accepted form and negative fixture per rejected form; binding shadowing, re-export cycles, schema mismatch, duplicate names, source offsets after escapes/Unicode, unsaved fragment changes, multi-operation selection, alias collision and runtime AST composition. Static source forms must behave identically across editor/CLI/scan/persisted tools.

## Result and variable type rules

Use dictionary-shaped `TypedDict` results/inputs and `Literal` enums/typenames; preserve list mutability (`list[T]`), nullable `T | None`, and key absence (`NotRequired[T]`) independently. Python has no JavaScript `undefined`; absent optional dictionary keys are its equivalent. Do not convert absence to an explicit sentinel inside JSON values. Output aliases are keys; unselected fields do not exist in the type. Merge repeated compatible selections; conflicting aliases are validation errors. Built-ins: `ID`, `String` → `str`; `Boolean` → `bool`; `Int` → `int`; `Float` → `float` (Python-native distinction from TypeScript `number`). Input coercion and static typing are separate: a GraphQL transport may accept integer Float inputs, but never convert a response for typing.

Custom scalars require configured Python input/output types; one shared mapping applies to both unless explicit separate mappings are supplied. Missing mapping is a configuration error, never implicit `Any`. Explicit `Any` is user-authorized escape, surfaced in doctor/report output. Scalar helper uses input type when checking values destined for variables and exposes separately named input/output aliases when mappings differ; no implicit decoding. Default enum values use Literal schema strings; an explicit enum type mapping replaces that static type for both input/output without runtime conversion. See the exact TOML shapes in the config contract.

| GraphQL form | Expected Python shape |
|---|---|
| selected `name: String!` / `name: String` | required `name: str` / required `name: str \| None` |
| `[String]`, `[String!]`, `[String]!`, `[String!]!` | `list[str \| None] \| None`, `list[str] \| None`, `list[str \| None]`, `list[str]` |
| `$id: ID!` without default | required `id: str`; missing/`None`/integer is a checker error |
| `$limit: Int = 10` | `NotRequired[int \| None]`; omitted is distinct from explicit null |
| `$limit: Int! = 10` | `NotRequired[int]`; explicit `None` is an error |
| nullable input member / non-null member with no default | optional nullable key / required non-null key, recursively |
| non-null input member with default | optional key; retain pinned upstream input-object default behavior in compatibility fixtures (upstream permits `null` here); warn that GraphQL validation/runtime may reject explicit null. Do not call scalar helper runtime validation. |
| `@oneOf` input object | union of alternatives with exactly one supplied non-null member; extras/multiple members rejected at source validation even where structural checker permissiveness needs a supplemental diagnostic |
| `@skip`, `@include`, `@defer` on field | `NotRequired[field_type]`, even with literal conditions, matching upstream's conservative presence semantics |
| conditional fragment spread | union of absent-spread shape and present-spread shape; independent direct fields remain present |
| `@required` / `@_required` | non-null override of outer selected-field wrapper |
| `@optional` / `@_optional` | nullable override of outer selected-field wrapper; required wins if both occur, matching pinned selection logic |

Union/interface selections distribute across concrete possible types, merging matching inline/spread conditions. Explicit `__typename` is a required discriminator literal (aliased typename uses its alias). Without selection, match upstream's optional abstract `__typename` type aid, but never manufacture it in runtime data; narrowing must check key presence. Scalars/enum/input helpers and query/mutation/subscription root selection all require positive and negative fixtures. Unknown fields/arguments, invalid variable use, impossible fragment conditions and invalid selections produce GraphQL diagnostics rather than an apparently trustworthy result.

Worked acceptance fixture (schema: `Query.book(id: ID!): Book`, `Book.id: ID!`, `Book.title: String`, `Book.tags: [String!]!`):

```python
book_query = graphql('query Book($id: ID!, $show: Boolean! = true) { book(id: $id) { key: id title tags @include(if: $show) } }')
# BookQueryVariables: id: str; show: NotRequired[bool]
# BookQueryResult: book: BookQueryBook | None
# BookQueryBook: key: str; title: str | None; tags: NotRequired[list[str]]
# VALID variables {"id": "1"}; INVALID {}, {"id": None}, {"id": 1}
# VALID key access after book-is-not-None guard.
# INVALID book["id"] (alias only), book["missing"], unguarded nullable book,
# and treating optional tags as an always-present key.
```

Checker diagnostics for direct optional-key indexing differ: require the configured Pyright optional-key diagnostic and a presence guard fixture, not just a hover string. Exhaustive variable fixtures include recursive inputs, defaults, enum mismatch, unknown members, nullable list members, oneOf, and independent input/output scalar mappings. Compare semantic type assignability and exact dictionary key contracts; display ordering is not a compatibility requirement.

## Fragment contract and fixture expectations

Default spread data exposes an opaque fragment reference, not fragment-owned fields. Directly selected fields remain accessible. Fragment refs identify schema, fragment identity and type condition; compatible spreads intersect; same-named fragments in separate schema/project identities cannot interchange. Generated phantom markers must be checker-only: no marker keys in real dictionaries, no wrapper, copy, validation or recursive traversal in helpers.

`read_fragment(fragment, value)` accepts matching masked data or the fragment result, preserves `None`, and supports one outer list with nullable elements. It returns the same object/list by `is`; existing dictionary/list mutation aliasing remains unchanged. Narrow abstract results by selected typename and preserve the upstream empty/absent conditional branch without fabricating fields. Python dictionary key absence handles upstream optional-property `undefined`; do not add a new runtime sentinel API. Nested outer lists are not an added parity promise. Global `disable_masking` and fragment `@_unmask` expose the selected result directly; remove `_unmask` from the runtime fragment definition as upstream does.

`mask_fragments(fragments, data)` statically requires the intersection of supplied fragment results and returns their combined opaque references, preserving nullable/single-list shapes. `read_result(document, data, fragments)` recursively resolves references for exactly the supplied registry; omitted fragments stay masked and can be supplied using `mask_fragments`. It verifies nested fragment fixtures only when their closure is supplied. `unsafe_read_result` checks direct/non-fragment fields but deliberately discards nested fragment requirements, documents this unsoundness and does not imply schema/runtime validation. Every helper returns the exact input identity, including `None` and list instances.

Acceptance FRAG: fragment-only `title` access fails before `read_fragment` and passes after; wrong fragment/schema ref fails; selected `id` remains usable; nested/transitive fragment, conditional spread, union/interface narrowing, unmask/global-disable, empty branches, nullable/single-list helpers and mixed masks all have positive/negative checker cases. A deliberately wrong nested fixture fails `read_result` with complete closure, remains masked with incomplete closure, and illustrates the documented unsafe helper limit. Runtime `is` checks cover every helper and demonstrate no keys added and no nested mutation/copy. Prior opaque-marker proof covers none of this broad matrix; it remains implementation acceptance work.

## Client interoperability without a new client

Adopt standard GraphQL AST runtime interoperability and an explicit generic boundary. `TypedDocument[R, V]` must be acceptable to an AST-consuming executor through its AST view; generated document objects expose `operation_name` and `document_id` where relevant. A synchronous generic execution protocol is `execute(document: TypedDocument[R, V], variables: V) -> R`; asynchronous equivalent returns an awaitable of `R`. Subscriptions return an async iterator of typed payloads. Generated overloads must correlate each document's variables and result, reject wrong variables, and preserve error/null response semantics: `R` is the GraphQL **data** shape, not an assertion that an error envelope always contains data.

No third-party Python client is assumed to infer these generics automatically. Publish a worked raw GraphQL-core executor example and an AST-client example using a thin user-side binding whose generic signature is checked by Pyright; implementations must preserve existing client exceptions and envelope/error behavior. Clients needing strings receive printed AST/source plus explicit selected operation, with result/variable aliases available at their boundary. Persisted-ID transports additionally require an explicit server protocol binding; an ID is not universally executable GraphQL. Do not claim automatic inference for untyped third-party methods or cast away possible partial/error responses.

Acceptance CLIENT: in-process schema execution for query/mutation/subscription, omitted versus explicit-null variables, operation selection, AST equivalence after fragment composition, failure envelope and async cancellation/error propagation; checker fixtures infer the correct result and reject wrong variables through both sync/async generic examples. At least one documented AST-consuming client integration must run on the support matrix before the client interoperability gate passes. Choosing a third-party convenience adapter is optional; no irreducible user choice blocks this contract and no client dependency is selected here.

## Remaining evidence boundary

These defaults close API/typing *specification* questions; they do not prove generated aliases, exact AST types, correlated overloads, checker-only fragment intersections or live Pylance updates are implementable across the required matrix. Implementation must run the named acceptance fixtures before passing API/SRC/TYPE/SCALAR/FRAG gates. If the chosen checker cannot express a contract, record the failed case and investigate a Python-native equivalent; do not weaken parity or relabel dynamic/unsafe fallback as success.

Cross-contract layout: schema-bound factory is `<generated_package>.<schema>.graphql` (single schema `default`), source result/variable/fragment aliases are `<generated_package>.sources.<source_module>`, and persisted constants are `<generated_package>.persisted.<schema>`. Config/CLI contract defines generation and omission commands. Runtime companions never import excluded authoring modules. These precise paths supersede informal companion-namespace examples above.
