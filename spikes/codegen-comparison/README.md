# Code generator comparison spike

Issue: [#4](https://github.com/atefth/graphql-desk/issues/4)

This fixture runs Ariadne Codegen and Turms independently against the same schema and operation. Generated output is ignored; the committed evidence is this report, the shared inputs, and the two minimal configurations.

## Reproduce

Requires Python 3.10+ and `uv`. From this directory, run:

```powershell
New-Item -ItemType Directory -Force out-ariadne | Out-Null
uvx --from ariadne-codegen==0.19.0 ariadne-codegen
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"
uvx --from turms==2.3.0 turms gen *> turms-run.log
if ($LASTEXITCODE -ne 0) { Get-Content turms-run.log; throw "Turms generation failed" }
```

The commands use pinned releases. They create generated output under `out-ariadne/` and `out-turms/`; those directories are git-ignored. The UTF-8 environment and output redirection avoid a Rich progress display crash on this host with the legacy Windows console encoding. The schema is local; `uvx` may need network access on its first run to download the pinned tools.

## Versions and method

Run on 2026-10-19 on Windows with Python 3.11.13 (uv-managed) and uv 0.8.19. Both actual generator commands completed successfully with their latest stable versions resolved by uv: Ariadne Codegen 0.19.0 and Turms 2.3.0. Package metadata reports Python `>=3.10` for each. Both use Pydantic v2; their `graphql-core` constraints overlap on `>=3.2.7,<3.3` (Ariadne `<3.3,>=3.2.7`; Turms `<4,>=3.2.0`).

Fixture covers a nullable object, non-null ID and string fields, a nullable string field, a mapped `DateTime` scalar, a required Boolean variable, and a shared fragment.

## Results

| Capability | Ariadne Codegen 0.19.0 | Turms 2.3.0 |
| --- | --- | --- |
| Operation result models | Generated Pydantic model for `UserById` and nested result; the nullable `user` is optional in its annotation | Generated Pydantic operation model and nested result; nullable `user` is `UserByIdUser | None` |
| Variables | Typed client method accepts `id: str` and `include_bio: bool`; generated result modules focus on results | `UserById.Arguments` model has typed required `id` and `includeBio` values |
| Nullability | Preserves nullable result types (`Optional[...]`); `user` is a required response key, while conditionally selected `bio` is optional with a `None` default | Preserves nullable result types; both nullable `user` and conditionally selected `bio` default to `None`, so omission and explicit null can be conflated |
| Fragment | Generates a reusable `UserCard` model and client method returning the operation model | Generates reusable `UserCard` model and references it as a base of the operation result model |
| `DateTime` scalar | Config maps the scalar to `datetime.datetime`; result annotations import `datetime`. Ariadne also documents `serialize`/`parse` hooks for custom scalar conversion | Config maps the scalar to `datetime.datetime`; generated annotations import `datetime`, relying on Pydantic validation/serialization for the mapped type |
| Client/runtime | Generates a typed async client and runtime modules (this was the default generated mode) | Generates models and an exact operation document; transport remains caller-selected. Optional functions plugin can generate operation call helpers |
| CLI/config | `pyproject.toml` `[tool.ariadne-codegen]`; command reads schema and operations and generates package. Async/sync and output options are configurable | `graphql.config.yaml` with `extensions.turms`; plugin-driven generation; CLI provides `init`, `gen`, and `watch` |
| Watch | No built-in watch command shown in the CLI; external file watcher can rerun generator | Built-in `turms watch` regenerates when documents change |
| Python/dependencies | Python >=3.10; graphql-core >=3.2.7,<3.3; Pydantic >=2.8,<3 | Python >=3.10; graphql-core >=3.2,<4; Pydantic >2 plus pydantic-settings |

These are observations from the fixture plus public docs/metadata. The generated artifacts are intentionally not committed; commands above reproduce them locally.

Scope limit: this is a small CLI/code-shape comparison, not a performance or large-schema benchmark. Mutations, subscriptions, every scalar hook, embedded API stability, and Pyright/mypy integration were not exercised; those remain separate spikes.

## Recommendation

**Do not integrate either generator as GraphQL Desk's core analysis engine or make both active by default.** Both generate useful Python types, but neither is the live editor/type inference bridge. GraphQL Desk should own schema/document parsing and LSP diagnostics so generator-specific file layout or runtime behavior cannot constrain editor feedback.

If GraphQL Desk offers generated-client output as a separate product feature, expose generator choice through isolated adapters with separate output directories. Turms is the more natural first candidate for this project's general-tool direction because it is transport-agnostic and has native watch mode. Keep Ariadne Codegen as a viable optional backend for users who want its generated async/sync client and scalar serialization hooks. Supporting both adds version matrix, config, output, and support burden; validate actual demand before maintaining two adapters.

Remaining custom GraphQL Desk work regardless of generator: live schema diagnostics/completion/hover/navigation; operation-to-Python source mapping; Pyright type refresh/inference; and a Python-specific fragment masking design. Generated types remain the fallback if checker integration proves unreliable.

## Sources

- [Ariadne Codegen README](https://github.com/mirumee/ariadne-codegen) — features, Python requirement, typed client, scalar hooks, and quickstart.
- [Ariadne Codegen releases](https://github.com/mirumee/ariadne-codegen/releases) — 0.19.0 release and changes.
- [Ariadne Codegen configuration reference](https://ariadnegraphql.org/server/0.25/ariadne-codegen/configuration) — configuration and scalar mapping.
- [Turms README](https://github.com/jhnnsrs/turms) — plugins, runtime separation, watch mode, config, and transport-agnostic design.
- [Turms PyPI project](https://pypi.org/project/turms/) — published package metadata.
