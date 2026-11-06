# Advanced Python utilities

`graphql_desk.internal` is an advanced import surface without a stability promise.
It adapts the [pinned internal entrypoint](https://github.com/0no-co/gql.tada/blob/cfe64f433bd29ff6f79e462596389b81c6e01aec/packages/internal/src/index.ts),
including its exported loader/configuration/error records. It does not reproduce
TypeScript compiler objects or JavaScript module layout. Importing the module,
constructing a loader or constructing a schema reference performs no schema reads,
network requests, project imports, output writes or background scheduling.

## Export correspondence

| Pinned export | Python equivalent |
| --- | --- |
| `loadFromSDL`, `loadFromURL`, `getURLConfig`, `load`, `loadRef` | `load_from_sdl`, `load_from_url`, `get_url_config`, `load`, `load_ref` |
| `SchemaLoader`, `SchemaLoaderResult`, `IntrospectionResult`, `SchemaRef` | Typed classes with snake-case methods and the same result/reference responsibilities |
| `OnSchemaUpdate`, `OnSchemaError` | Typed callback aliases |
| `BaseLoadConfig`, `LoadConfig`, `SingleSchemaInput`, `MultiSchemaInput`, `SchemaOrigin` | Frozen load/input records; `SchemaInput` aliases the single-schema input; existing redacted `SchemaOrigin` |
| `TSError`, `TadaError` | `CheckerIntegrationError(CheckerDiagnostic(...))`, `GraphQLDeskError`; no TypeScript diagnostic object |
| `BaseConfig`, `SchemaConfig`, `GraphQLSPConfig`, `ProjectConfig` | Existing validated Python `ProjectConfig`/`SchemaConfig`, including `ScalarMapping`; no duplicate configuration model |
| `parseConfig`, `validateUniqueOutputLocations` | `parse_config`, `validate_unique_output_locations`; reuse the canonical parser and stricter cross-project ownership checks |
| `getSchemaNamesFromConfig`, `getSchemaConfigForName` | `get_schema_names_from_config`, `get_schema_config_for_name`; an unbound single default maps to `None` |
| `readTSConfigFile`, `findTSConfigFile` | `read_config_file` returns UTF-8 TOML text; `find_config_file` locates the nearest active Python project |
| `loadConfig`, `loadConfigs`, `LoadConfigResult` | `load_config`, `load_configs`, existing `EffectiveProject` result (`config`, `origin`); root/path facts are `result.config.root`/`.path` |
| `resolveTypeScriptRootDir` | `resolve_project_root`, returning `None` when no valid project can be selected |
| `minifyIntrospection`, `preprocessIntrospection` | `minify_introspection`, `preprocess_introspection`; JSON introspection and a typed Python `SchemaMap` |
| `outputIntrospectionFile`, `extractIntrospectionHeader` | Pure `output_introspection_file`, `extract_introspection_header`; Python `.py`/`.pyi` and leading `#` comments |
| Root parser/scalar/opaque marker type capabilities (`parseDocument`, `$tada`, `__mapType`) | Standard `parse`/`DocumentNode`, existing exact generated parser companions, `ScalarMapping`, `FragmentIdentity`/`FragmentRef`/`FragmentDocument`, and `SchemaMap`; no runtime marker keys |
| Optional `setupCache`/`AbstractSetupCache` and turbo utilities | Unsupported until #83/#84; no acceleration dependency for required typing or these utilities |

`parse_config` accepts a settings mapping or TOML text with an explicit
`config_path`. `load_config` selects an explicit `pyproject.toml` or nearest project;
`load_configs` accepts one target or an explicit iterable, deduplicates canonical
project identities and checks their combined output ownership. This replaces
TypeScript project references with caller-supplied Python project boundaries;
it does not recursively scan unrelated projects or execute configuration code.

## Loader and reference lifecycle

```python
from pathlib import Path
from graphql_desk.internal import MultiSchemaInput, SchemaInput, SchemaOrigin, load_ref

ref = load_ref(MultiSchemaInput((
    SchemaInput(name="catalog", origin=SchemaOrigin(local_path=Path("catalog.graphql"))),
    SchemaInput(name="account", origin=SchemaOrigin(local_path=Path("account.graphql"))),
)))
ref.load()
stop = ref.autoupdate(lambda current, schema: print(current.version, schema.name))
try:
    # Use ref.multi["catalog"].schema after checking that the result is not None.
    pass
finally:
    stop()
```

`load_from_sdl(path)` constructs a file loader for SDL or introspection JSON;
the existing `schema_loading.load_from_sdl(text)` remains the separate pure parser.
`loader.load()` reuses a successful cached result; `load(reload=True)` rereads or
refetches. Failed reloads clear that cache, so the next ordinary load retries.
`load_schema()` and `load_introspection()` share it. URL headers resolve only from
the explicitly supplied, copied `environment` mapping, using the existing TLS,
redirect, response-limit and redaction rules. No unsafe upstream TLS fallback is
reproduced.

`BaseLoadConfig.assume_valid` accepts only booleans. It is a recognized optimization
hint, not a validation bypass: both values use the existing mandatory SDL/schema
validation. Local polling uses 250 ms, corresponding to the pinned local loader's
fallback interval. URL `fetch_interval` is in seconds; its `None` default maps the
pinned 60,000 ms default to 60 seconds. Zero disables automatic URL refresh.
`load_ref(ProjectConfig)` preserves `schema_refresh_interval`, including zero;
an explicit `load()` still fetches. There are no new project configuration keys.

Only `notify_on_update`/`autoupdate` starts update subscriptions. Local polling
checks metadata and reloads changed files; URL subscriptions reload at the chosen
positive interval. Callbacks run on daemon threads and must synchronize their own
application state. Unsubscribe is idempotent, stops future work for the last
subscriber and prevents late callbacks from an already running read. Calling
unsubscribe from outside a callback also waits for that subscription's active
callback; calling it inside a callback allows other callbacks already dispatched
to finish, avoiding mutual teardown deadlocks. An in-flight
socket operation cannot be forcibly cancelled; it retains the existing loader's
timeout limitations. Callback exceptions are sanitized through the supplied error
callback; a failing error callback does not terminate the watcher.

References expose `current`, named `multi`, and monotonic `version`. Initial and
explicit successful loads advance versions; updates identify the schema input.
A failed schema becomes `None` and advances the version while unaffected schemas
remain available; repair is retried on the next update. This explicitly avoids
presenting a last-good schema as current. A delayed load/update/error commits only
if its loader observation is still current; superseded handoffs neither replace
fresh reference state nor advance its version or call reference subscribers.
Initial autoupdate loads are synchronous,
and partial setup tears down earlier subscriptions if the consumer aborts setup.
Create a new reference to change already captured load settings.

## Pure introspection output

Minification preserves type wrappers, input defaults, one-of metadata and field/enum
deprecation flags. It removes system types, descriptions and directive definitions,
sorts type/field/argument/reference collections as the pinned implementation does,
and never mutates the input. `preprocess_introspection` produces a Python lookup
record, rather than TypeScript type-literal text. It retains schema roots, named
types, field lookup records, possible types and input facts.

`output_introspection_file` returns text without writing files. `.py` exports
JSON-compatible `introspection` plus an optional `schema_map`; `.pyi` declares
those same exports. Both include the existing renderer's scalar/enum/input aliases,
so custom scalar mappings remain required and runtime/stub imports agree. Setting
`should_preprocess=False` omits only `schema_map`. TypeScript syntax, declaration
merging and `.ts`/`.d.ts` formats are not Python output formats. Custom preambles
must be Python comments; encoding directives and executable text are rejected.
Extracted custom headers can be passed back for deterministic regeneration.

`tests/test_internal.py` covers inert imports/construction, cached and failed loads,
named state and update teardown, configured explicit-only URL behavior, secret-safe
errors, configuration discovery, wrapper/default/one-of minification, deterministic
runtime/stub output and positive/negative mypy/Pyright consumers. This is scoped
#76 evidence, not broad editor lifecycle, full parity or performance acceptance.
Metadata polling can miss transient or metadata-preserving edits; explicit forced
reload remains available. Callers own workspace trust, output publication and
configuration freshness; these explicit-path utilities are not a new editor bridge.
