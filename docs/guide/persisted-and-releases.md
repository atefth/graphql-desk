# Persisted operations and source-free releases

GraphQL Desk generates server manifests and typed ID bindings. It does not provide
a server registry, APQ negotiation, transport or an endpoint that executes an ID.
Your client/server protocol must agree on the ID and registered document bytes.

## Retained source

```python
book_query = graphql("query Book($id: ID!) { book(id: $id) { id } }")
SavedBook = graphql.persisted("book-by-id-v1", book_query)
```

The retained carrier keeps its result/variable typing, AST and selected operation,
and adds the persisted ID. Its AST is the original AST object; the carrier itself
may be a new frozen value. `SavedBook` still works with a retained-document client.
The manifest command finds statically resolved persisted calls, expands supplied
fragment closure and diagnoses conflicting IDs or unresolved documents.

```toml
[tool.graphql-desk]
schema = "schema.graphql"
schema_output = "demo_generated/default.pyi"
generated_package = "demo_generated"
persisted_output = "persisted-manifest.json"
```

```console
graphql-desk generate-persisted
```

Default normalization prints the registered executable document using the pinned
GraphQL representation. `--disable-normalization` preserves authored text where
supported; `--fail-on-warn` prevents writes on warnings. A `sha256:` ID is checked
against the specified editor hash bytes, which are not automatically identical
to normalized manifest bytes. Read the
[persisted byte/fragment rules](../contracts/persisted-and-project-analysis.md)
before computing IDs outside the tool. Do not let a client silently transform a
document after registration, for example by injecting `__typename`.

## ID-only runtime

Put GraphQL source in dedicated selected `authoring_roots`, and import generated
persisted constants from application code instead of importing authoring modules:

```toml
authoring_roots = ["authoring"]
```

The [worked project](../../examples/typed_client/pyproject.toml) demonstrates the
complete TOML, including a wheel target. Its authoring module declares `SavedBook`;
its [application module](../../examples/typed_client/app.py) constructs an ID-only
wire payload. Generated persisted variable/result aliases are stub declarations,
so use type-only imports and postponed annotations rather than constructing them
as runtime classes:

```python
from __future__ import annotations
from typing import TYPE_CHECKING
from demo_generated.persisted.default import SavedBook

if TYPE_CHECKING:
    from demo_generated.persisted.default import SavedBookVariables

def request(variables: SavedBookVariables) -> dict[str, object]:
    return {"id": SavedBook.persisted_id, "variables": variables}
```

This is a payload example, not a universal persisted protocol. ID-only
`PersistedDocument` has no `.ast` and cannot be passed to an AST executor. It does
not contain query text; result/variable declarations remain in its `.pyi`.

## Build and inspect the actual wheel

```console
graphql-desk generate-persisted --omit-runtime-source
python -m build --wheel --no-isolation --outdir dist
graphql-desk check --artifact dist/YOUR_APPLICATION.whl
```

First generate a valid server manifest from the authoring source. Omission creates
a separate projection under `build/graphql-desk/omitted`; it does not rewrite the
authored Python. Configure your build backend to package the projected generated
tree and required application modules, including the projected SDK stubs, as the
example does. Keep authoring modules and the server manifest out of the runtime
wheel. Do not package the ordinary live generated tree: query text can leak from
`Literal` overloads and exact-source AST typing even when runtime strings are gone.

`check --artifact` validates the wheel against the omitted projection manifest,
checks both Python and stub content, and rejects source-bearing or mismatched
artifacts/import dependencies. The example verifier builds a real Hatchling wheel,
runs this check and imports its ID-only entrypoint directly from that wheel in an
isolated Python process. It does not claim installation or server round-trip
acceptance on every platform.

The application must not import excluded authoring modules. Generation and artifact
validation reject cases where that separation cannot be proved; an unsupported
dynamic import is not silently assumed safe. Retain generated ownership files and
resolve conflicts through the reported recovery path.

For executable coverage of same-ID conflicts, source separation, package aliases,
wheel contamination and modified owned output, see
[`tests/test_release_artifacts.py`](../../tests/test_release_artifacts.py),
[`tests/test_persisted_manifest.py`](../../tests/test_persisted_manifest.py) and
the [worked verifier](../../examples/typed_client/verify.py). Full conformance and
release gates remain separate from this source-free build example.
