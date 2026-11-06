# Typed operations, clients and fragments

The executable [worked example](../../examples/typed_client/README.md) uses the
same original Python module to declare operations and consume their inferred
types. Run its verifier from the repository root after `uv sync --locked`:

```console
uv run python examples/typed_client/verify.py
```

## Which import provides inference

Use `from <generated_package>.<schema> import graphql` for precise authoring.
The generated factory stub matches exact source/operation/fragment signatures and
returns the corresponding typed document. The original Python call receives that
type; generated aliases alone do not make arbitrary runtime functions infer it.

For `book_query` in `authoring.operations`, the aliases are
`<generated_package>.sources.authoring.operations.BookQueryResult` and
`BookQueryVariables`. A fragment `book_fields` also exposes `BookFieldsResult`,
`BookFieldsFragmentRef` and `read_book_fields` in that source companion.

Root `graphql_desk.graphql`/`gql` and `init_graphql("name")` remain useful runtime
constructors. They do not supply the generated operation inference. A generated
factory's unsupported/dynamic fallback exposes read-only document metadata rather
than a falsely precise result. Static diagnostics explain unresolved calls.
Use an inline literal, or a supported exact `Literal`/`Final` constant, when a
checker widens an extracted string. Dynamic f-string interpolation and arbitrary
Python evaluation are not static GraphQL discovery.

Selected fields become dictionary-shaped types. Nullability (`T | None`), optional
key presence (`NotRequired[T]`) and list members are separate. Check a nullable
object before indexing it; test key presence for conditional selections. Field
aliases replace the original response key. A non-null required variable has a
required key, and omission differs from explicitly supplying `None`.

Custom scalars require explicit input/output mappings. Enums normally use schema
string literals; a configured enum type changes the static view without decoding
runtime values. `graphql.scalar("TypeName", value)` returns the same value; its
generated signature checks the configured input shape. Explicit `typing.Any` is
an escape hatch and is reported by doctor, not silently added for missing mappings.

The root `parse` is graphql-core's parser. Generated parser companions and editor
overloads add exact executable literal AST views for supported default options;
dynamic strings, `Source`, SDL and nondefault modes retain ordinary AST types.
Parsing is not schema validation. The [API/type contract](../contracts/api-and-types.md)
specifies the complete required corpus and current acceptance boundary.

## Connect an existing client

`TypedDocument[Result, Variables]` holds `.ast`, `.operation_name` and optional
schema/persisted metadata. An executor receives the original AST and operation
selection. GraphQL Desk provides no transport, response coercion or runtime result
validation, and third-party methods do not acquire inference automatically.

The repository's [generic client bindings](../../scripts/client_bindings.py)
correlate each document with its result and variables. The example copies that
maintained file instead of introducing another client. A normal call is:

```python
variables = BookQueryVariables(id="1")
response = execute_core_sync(schema, book_query, variables)
if response.data is not None:
    book = response.data["book"]
```

Construct or explicitly annotate the generated variables type at a generic client
boundary; a bare dictionary does not reliably preserve correlation in mypy.
The verifier checks that missing `id`, an integer `id`, and reading masked
`title` directly are errors in both pinned checkers.

The response's `data` can be `None`, even when selected GraphQL fields are non-null.
Partial data and errors remain in the native envelope. The gql binding requests
that envelope and preserves its native `TransportQueryError`, including partial
data. Transport errors and cancellation propagate. gql's local schema transport
is asynchronous in this example; it makes no network requests. Sync, async,
mutation and subscription/cancellation coverage is linked in
[client interoperability](../implementation/client-interop.md).

## Read a masked fragment without changing the data

```python
book_fields = graphql("fragment BookFields on Book { title tags }")
book_query = graphql(
    "query Book($id: ID!) { book(id: $id) { id ...BookFields } }",
    (book_fields,),
)
```

The query exposes `id` directly; fragment-owned fields require a reader:

```python
card = read_fragment(book_fields, book)
title = card["title"]  # str | None
```

`read_fragment`, generated `read_<binding>` helpers, `read_fragment_passthrough`,
`mask_fragments`, `read_result` and `unsafe_read_result` preserve runtime object
identity. They do not add phantom keys, traverse/copy responses or validate JSON.
The example checks identity and absence of a runtime marker key.

Use exact `read_fragment` for a known matching reference. Use the separate
`read_fragment_passthrough` when an unknown fragment reference must remain usable
as a reference: its static return retains `Result | FragmentRef[...]`, so fragment
fields are not always accessible. Bind the returned value before using it in a
wider generic context; checker contextual inference can otherwise be conservative.
This split is the explicit Python equivalent of an identity-exclusion constraint
Python's type system cannot express. Mutable aliases stay mutable.

For fixture helpers, precise registry typing requires a fixed tuple:

```python
masked = mask_fragments((book_fields,), fixture)
expanded = read_result(book_query, data, (book_fields,))
```

`mask_fragments` checks combined fragment inputs; `read_result` exposes exactly
the supplied indexed fragment closure and leaves omitted fragments masked. Their
call-site overloads come from the editor worker's SDK overlay. Saved CLI factory
generation alone is not an automatic mypy SDK-overlay adapter. Lists/dynamic
registries remain valid runtime identity calls but get conservative typing and a
diagnostic. Mypy does not consume Pylance's `stubPath`; explicit mypy integration
must supply the generated SDK typing package and call-site modules.

`unsafe_read_result` and `unsafe_read_<binding>_result` deliberately check only
direct selections and can accept invalid masked values. Generated fixture aliases
or bound helpers provide better dictionary-literal context. They are not a proof
that a server response satisfies the schema. See the complete
[fragment helper notes and limits](../implementation/fragment-refs.md), including
nullable abstract arrays, absent branches, raw structural inputs and enum/typename
fixture widening. The full conformance gate remains open.
