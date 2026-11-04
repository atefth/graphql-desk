# Schema download and conversion

`graphql-desk generate-schema INPUT` (also `generate schema INPUT`) reads a local SDL file, introspection JSON, or HTTP(S) endpoint and emits deterministic SDL through GraphQL-core's `print_schema`. It reuses the same validated local and authenticated URL loaders as the editor and other generation commands. Project Python code is never imported.

```console
graphql-desk generate-schema introspection.json --output schema.graphql
graphql-desk generate schema https://api.example.test/graphql --header 'Authorization: Bearer ${env:API_TOKEN}' > schema.graphql
```

The input is a filename or URL. `--config/-c` selects project configuration; the optional Python selector `--schema NAME` selects a named schema's destination and header context. An explicit output or redirected stdout works without a project. If a project is present, its configuration must be valid before generation proceeds.

Output precedence is explicit `--output/-o`, then redirected stdout, then the selected configured local SDL path in a terminal. With no destination, generation fails before a network request. A JSON origin is never replaced with SDL; choose an SDL output or redirect stdout. stdout contains only SDL, with success reporting on stderr. File writes use the shared atomic output writer after schema validation succeeds. Symlink destinations (including configured aliases), all `.py`/`.pyi` paths, project configuration, the managed generated package and its reserved outputs, and introspection inputs are protected from replacement.

Configured headers apply only when the input URL exactly matches the selected configured URL. Repeat `--header 'Name: value'`; later entries override earlier/configured values case-insensitively. The shared URL loader validates headers, resolves explicit environment references, negotiates introspection support, strips credentials on foreign-origin redirects, and sanitizes failures. CLI failures and callable errors do not echo input contents, endpoint credentials, headers, or response bodies.

```python
from graphql_desk.cli import SchemaGenerationError, generate_schema

generate_schema(input="introspection.json", output="schema.graphql", silent=True)
```

The callable takes keyword arguments, returns `None`, and raises `SchemaGenerationError` with sanitized `diagnostics`; it never exits the process. It uses file-output semantics, so supply an output or a configured local SDL destination. `silent=True` suppresses reporting and still raises failures.

This delivers the schema conversion/download story; it does not by itself pass the broader SCHEMA-01, CLI-01, or full-parity acceptance gates.
