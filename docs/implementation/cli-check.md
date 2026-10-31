# Project check command

`graphql-desk check` statically discovers Python sources from the selected
GraphQL Desk project, loads its configured schema or schemas, and reports
GraphQL source and validation diagnostics. It uses the shared Python source
resolver, fragment index, persisted manifest validator, field-use analyzer,
colocated-fragment diagnostics, schema validator, and deprecated-field/argument
warnings; it never imports project code. Project preferences control field-use
and colocated-fragment warnings.

`graphql_desk.cli.check(config=None, schema=None, environment=None)` is the
callable form. It returns structured diagnostics with severity, code, message,
and, when source-backed, module and source range. Schema selection is a binding,
not a guess; multi-schema projects can pass `schema` to check one configured
schema.

The CLI's `--level info|warn|error` controls only what is displayed. Errors
always fail the command; warnings fail only with `--fail-on-warn`/`-w`. Output
is diagnostic-only on stderr, leaving stdout empty for shell and CI consumers.
The existing `check --artifact PATH` wheel verification mode remains available.

The command currently checks resolvable embedded documents and schema
validation. Dynamic source expressions are reported as warnings when static
resolution cannot prove their content. Scan rules, GitHub Actions annotation
formatting, JSON output, and full parity are tracked by their own acceptance
stories; this slice does not claim those capabilities or close broad gates.
