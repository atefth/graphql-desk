# Client interoperability examples

`scripts/client_bindings.py` contains user-side bindings for `graphql-core` and
`gql==4.4.0`. Copy the bindings into application code as needed; they are not
package exports, a transport, or automatic inference for third-party methods.
The `gql` dependency is development-only. Runtime package dependencies do not
change.

Each binding accepts `TypedDocument[Result, Variables]`, forwards its original
`DocumentNode` and selected operation name, and passes variables through unchanged.
Use a generated variables `TypedDict` constructor, including for no-variable
operations:

```python
variables = ReadVariables(name="Ada")
response = execute_core_sync(schema, document, variables)
if response.data is not None:
    print(response.data["greeting"])
```

Here `ReadVariables` and the result declaration can come from a generated source
companion. A retained-source generated factory supplies the correlated document
types; both strict checkers verify inference through the existing client bindings.
Mypy cannot reliably infer the variables type from a bare dictionary literal at
this generic boundary. Constructing the generated `TypedDict` explicitly works
with both pinned checkers and checks its required keys and value types.

`TypedResponse[Result]` is a read-only typing view of the native execution result.
The bindings return the original envelope without copying or transforming data,
errors, or extensions. Its `data` can be `None`; nullable partial results remain
nullable. The cast at the client boundary assumes the document's result declaration
matches the schema and operation. It performs no runtime result validation.

The `gql` bindings request the native execution envelope. GraphQL errors still
raise the client's original `TransportQueryError`, including its partial data,
errors, and extensions. Transport exceptions and cancellation propagate. The
subscription binding returns an `AsyncIterator` and closes the underlying stream
when the caller closes it. Callers should close a subscription explicitly when
stopping consumption early.

Run the executable examples and checker fixtures with:

```console
uv run --python 3.10 python -m unittest tests.test_client_bindings
```

Coverage includes real local execution, operation selection, fragment composition,
omitted versus null variables, partial errors, mutation, asynchronous execution,
subscription cleanup, and cancellation. It does not exercise network transports
or promise inference for arbitrary client APIs. A generated-factory integration
fixture checks inferred result/variables through every binding signature, rejects
invalid variables, and executes generated query and mutation documents through
graphql-core and gql's sync/async bindings, plus a generated subscription through
gql. It checks unchanged AST, variables and native envelope identity. This bounded
proof does not establish automatic import attachment or a broad compatibility
gate.
