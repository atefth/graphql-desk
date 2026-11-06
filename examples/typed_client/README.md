# Typed client and persisted release example

From the repository root, provision the existing development lock and run:

```console
uv sync --locked
uv run python examples/typed_client/verify.py
```

The verifier requires Node.js 22 on `PATH` and the provisioned development lock.
It invokes the installed Pyright bundle directly, failing if Node or that bundle
is missing instead of allowing automatic Node/Pyright downloads. It installs or
fetches nothing. It copies this fixture into a disposable
directory under the repository's `.tmp/worked-example`, redirects its tool caches
there, and reuses [`scripts/client_bindings.py`](../../scripts/client_bindings.py).
It removes its own run directory on success or failure. The tracked example and
your project/configuration remain unchanged.

The files form one complete project:

| File | Purpose |
| --- | --- |
| [pyproject.toml](pyproject.toml) | Schema, generated package, selected source/authoring roots and source-free Hatchling wheel configuration. |
| [schema.graphql](schema.graphql) | Local schema with nullable fragment data and a nullable failing resolver field. |
| [authoring/operations.py](authoring/operations.py) | Precise factory calls and generic execution in the same original source; exact/passthrough fragment identity and partial errors. |
| [app.py](app.py) | ID-only application import; persisted aliases are imported only for type checking. |
| [verify.py](verify.py) | Generation, runtime, checker, build and wheel checks using the provisioned interpreter. |

Verification steps:

1. Generate the saved typing package and retained persisted bindings/manifest.
2. Run graphql-core and gql's asynchronous local-schema client. Preserve nullable
   data, partial errors and the gql client's native exception. No HTTP is used.
3. Check original source and ID-only app with strict mypy and explicit-interpreter
   Pyright. Require errors for missing/wrong variable values and direct access to
   masked fields; do not count an unrelated import failure as a successful negative.
4. Generate the separate source-free projection, build a real wheel without build
   isolation/network provisioning, and run `check --artifact` against it.
5. Import the ID-only application directly from that wheel in an isolated Python
   process. This is an archive import check, not an installation/support-matrix claim.

The generic bindings are user-side examples, not GraphQL Desk runtime exports or
a new transport. The wheel has no server registry or APQ handshake; `app.request`
only illustrates an ID/variables payload for a server protocol you provide.

The verifier exercises saved generation. It does not launch the editor, emulate
live SDK overlays, verify every fragment/parser edge, or complete release/parity
acceptance. Read the [user guide](../../docs/guide/getting-started.md) and
[coverage/gaps](../../docs/guide/evidence.md) for those boundaries. The repository
and example do not add a license choice.
