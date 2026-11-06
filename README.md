# GraphQL Desk

Python GraphQL developer tooling targeting full gql.tada and GraphQLSP feature parity, with a VS Code development extension. Generated schema types and live operation typing are core requirements; full parity remains in progress.

Start with the [user guide](https://github.com/atefth/graphql-desk/blob/main/docs/guide/getting-started.md) and [executable client example](https://github.com/atefth/graphql-desk/tree/main/examples/typed_client).

From a source checkout:

```console
uv sync --locked
uv run python examples/typed_client/verify.py
```

See [configuration and CLI](https://github.com/atefth/graphql-desk/blob/main/docs/guide/configuration-and-cli.md), [typing and fragments](https://github.com/atefth/graphql-desk/blob/main/docs/guide/typing-and-fragments.md), and [persisted releases](https://github.com/atefth/graphql-desk/blob/main/docs/guide/persisted-and-releases.md).

The implementation reuses graphql-core, graphql-language-service, and VS Code/Python/Pylance APIs. The separate saved-generator evaluation is documented in the repository.

[Compatibility and migration](https://github.com/atefth/graphql-desk/blob/main/docs/guide/api-and-compatibility.md) and [acceptance evidence](https://github.com/atefth/graphql-desk/blob/main/docs/guide/evidence.md) explain supported workflows and remaining work.

Licensed under [MIT](LICENSE).
