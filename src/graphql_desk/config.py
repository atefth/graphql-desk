"""Declarative project facts; no discovery, credentials, imports or schema loading.

Paths are resolved for identity checks only. Header environment references and
qualified type symbols remain unresolved until their dedicated integrations.
"""

import keyword
import math
import os
import re
import sys
import unicodedata
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Literal, cast
from urllib.parse import urlsplit

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib

_NAME = re.compile(r"[_A-Za-z][_0-9A-Za-z]*\Z")
_ENV_REFERENCE = re.compile(r"\$\{env:([A-Za-z_][A-Za-z_0-9]*)\}")
_EXCLUDE = (
    "**/.git/**",
    "**/.venv/**",
    "**/venv/**",
    "**/__pycache__/**",
    "**/site-packages/**",
    "**/build/**",
    "**/dist/**",
)
_SCHEMA_KEYS = {
    "schema",
    "schema_output",
    "persisted_output",
    "cache_output",
    "scalars",
    "enums",
    "disable_masking",
}
_PROJECT_KEYS = _SCHEMA_KEYS | {
    "schemas",
    "generated_package",
    "cache_enabled",
    "template",
    "track_field_usage",
    "check_colocated_fragments",
    "client_directives",
    "schema_refresh_interval",
    "disable_preprocessing",
    "source_roots",
    "include",
    "exclude",
    "entrypoints",
    "authoring_roots",
    "templateIsCallExpression",
}


class ProjectConfigParseError(ValueError):
    """Sanitized configuration failure with a source range in the TOML text."""

    def __init__(
        self, message: str, start: int, end: int, *, original_message: str | None = None
    ) -> None:
        super().__init__(original_message or message)
        self.message = message
        self.start = start
        self.end = end


def _toml_error_offset(text: str, error: Exception) -> int:
    match = re.search(r"\(at line (\d+), column (\d+)\)$", str(error))
    if match is None:
        return len(text) if "at end of document" in str(error) else 0
    line, column = (int(part) for part in match.groups())
    starts = [0]
    starts.extend(match.end() for match in re.finditer(r"\r\n|\r|\n", text))
    line_start = starts[min(max(line - 1, 0), len(starts) - 1)]
    return min(line_start + max(column - 1, 0), len(text))


def _project_table_span(text: str) -> tuple[int, int]:
    match = re.search(
        r"(?m)^[ \t]*\[+tool\.graphql-desk(?:\.[^\]\r\n]+)?\]+[ \t]*(?:#.*)?$",
        text,
    )
    if match is not None:
        return match.start(), match.end()
    return (0, min(len(text), text.find("\n") if "\n" in text else len(text)))


@dataclass(frozen=True)
class SchemaOrigin:
    local_path: Path | None = None
    url: str | None = field(default=None, repr=False)
    headers: tuple[tuple[str, str], ...] = field(default=(), repr=False)


@dataclass(frozen=True)
class ScalarMapping:
    name: str
    input: str
    output: str


@dataclass(frozen=True)
class SchemaConfig:
    name: str
    origin: SchemaOrigin
    schema_output: Path
    persisted_output: Path | None
    cache_output: Path | None
    scalars: tuple[ScalarMapping, ...]
    enums: tuple[tuple[str, str], ...]
    disable_masking: bool


@dataclass(frozen=True)
class ProjectConfig:
    path: Path
    root: Path
    generated_package: str
    schemas: tuple[SchemaConfig, ...]
    template: str | None
    track_field_usage: bool
    check_colocated_fragments: bool
    client_directives: tuple[str, ...]
    schema_refresh_interval: float
    disable_preprocessing: bool
    cache_enabled: bool
    source_roots: tuple[Path, ...]
    include: tuple[str, ...]
    exclude: tuple[str, ...]
    entrypoints: tuple[str, ...] | None
    authoring_roots: tuple[Path, ...]
    source_scope: Literal["project", "explicit_cli"] = field(
        default="project", kw_only=True
    )


def _table(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be a table")
    mapping = cast(Mapping[object, object], value)
    if any(not isinstance(key, str) for key in mapping):
        raise ValueError(f"{label} keys must be strings")
    return cast(dict[str, object], dict(mapping))


def _known(table: dict[str, object], keys: set[str], label: str) -> None:
    if table.keys() - keys:
        # Do not repeat untrusted keys or configuration values in diagnostics.
        raise ValueError(f"Unknown configuration key in {label}")


def _string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ValueError(f"{label} must be a nonempty string")
    return value


def _identifier(value: object, label: str) -> str:
    name = _string(value, label)
    if (
        not name.isidentifier()
        or keyword.iskeyword(name)
        or unicodedata.normalize("NFKC", name) != name
    ):
        raise ValueError(f"{label} must be a canonical Python identifier")
    return name


def _symbol(value: object, label: str) -> str:
    symbol = _string(value, label)
    parts = symbol.split(".")
    if len(parts) < 2:
        raise ValueError(f"{label} must be an import-qualified type or callable")
    for part in parts:
        _identifier(part, label)
    return symbol


def _boolean(table: dict[str, object], key: str, default: bool) -> bool:
    value = table.get(key, default)
    if not isinstance(value, bool):
        raise ValueError(f"{key} must be a boolean")
    return value


def _strings(value: object, label: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ValueError(f"{label} must be an array of strings")
    return tuple(_string(item, label) for item in cast(list[object], value))


def _path(value: object, root: Path, label: str) -> Path:
    text = _string(value, label).replace("${configDir}", str(root))
    if "${" in text:
        raise ValueError(f"Unknown substitution in {label}")
    try:
        return (root / text).resolve()
    except (OSError, ValueError, RuntimeError):
        raise ValueError(f"Invalid path in {label}") from None


def _inside(path: Path, root: Path, label: str) -> None:
    if not path.is_relative_to(root):
        raise ValueError(f"{label} must stay inside its owning directory")


def _header_value(value: object) -> str:
    if not isinstance(value, str) or any(char in value for char in "\r\n\x00"):
        raise ValueError("Schema header values must be single-line strings")
    return value


def _origin(value: object, root: Path) -> SchemaOrigin:
    headers: tuple[tuple[str, str], ...] = ()
    is_record = isinstance(value, Mapping)
    if is_record:
        table = _table(cast(Mapping[object, object], value), "schema")
        _known(table, {"url", "headers"}, "schema")
        value = table.get("url")
        raw_headers = _table(table.get("headers", {}), "schema headers")
        pairs: list[tuple[str, str]] = []
        for key, item in sorted(raw_headers.items()):
            if not re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", key):
                raise ValueError("Invalid schema header name")
            item = _header_value(item)
            remainder = _ENV_REFERENCE.sub("", item)
            if "${" in remainder:
                raise ValueError("Unknown substitution in schema headers")
            pairs.append((key, item))
        headers = tuple(pairs)
    text = _string(value, "schema")
    if text.lower().startswith(("http:", "https:")):
        try:
            parsed = urlsplit(text)
            if (
                not parsed.hostname
                or parsed.port == 0
                or "${" in text
                or any(character.isspace() for character in text)
            ):
                raise ValueError
        except ValueError:
            raise ValueError("Invalid HTTP(S) schema URL") from None
        return SchemaOrigin(url=text, headers=headers)
    if headers or is_record or "://" in text:
        raise ValueError("Schema URL records require an HTTP(S) URL")
    local_path = _path(text, root, "schema")
    if local_path.is_dir():
        raise ValueError("Local schema must be a file path")
    return SchemaOrigin(local_path=local_path)


def resolve_schema_headers(
    origin: SchemaOrigin, *, environment: Mapping[str, str]
) -> SchemaOrigin:
    """Expand explicit header references once, returning a new redacted origin.

    The environment must be supplied by the caller; no process environment is
    read. Expanded values are never interpreted as additional substitutions.
    This does not load a schema, contact a URL or modify the config facts.
    """

    def substitute(match: re.Match[str]) -> str:
        name = match.group(1)
        try:
            value = _header_value(environment[name])
        except KeyError:
            raise ValueError(f"Missing environment variable: {name}") from None
        except ValueError:
            raise ValueError(f"Invalid header environment variable: {name}") from None
        return value

    resolved: list[tuple[str, str]] = []
    for key, value in origin.headers:
        if not re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", key):
            raise ValueError("Invalid schema header name")
        value = _header_value(value)
        if "${" in _ENV_REFERENCE.sub("", value):
            raise ValueError("Unknown substitution in schema headers")
        resolved.append((key, _ENV_REFERENCE.sub(substitute, value)))
    return replace(origin, headers=tuple(resolved))


def _mappings(
    table: dict[str, object],
) -> tuple[tuple[ScalarMapping, ...], tuple[tuple[str, str], ...]]:
    scalars: list[ScalarMapping] = []
    enums: list[tuple[str, str]] = []
    for name, value in sorted(_table(table.get("scalars", {}), "scalars").items()):
        if not _NAME.fullmatch(name):
            raise ValueError("Scalar mapping keys must be GraphQL names")
        if isinstance(value, str):
            input_type = output_type = _symbol(value, "scalar mapping")
        else:
            mapping = _table(value, "scalar mapping")
            if mapping.keys() != {"input", "output"}:
                raise ValueError("Scalar mappings require exactly input and output")
            input_type = _symbol(mapping["input"], "scalar input")
            output_type = _symbol(mapping["output"], "scalar output")
        scalars.append(ScalarMapping(name, input_type, output_type))
    for name, value in sorted(_table(table.get("enums", {}), "enums").items()):
        if not _NAME.fullmatch(name):
            raise ValueError("Enum mapping keys must be GraphQL names")
        enums.append((name, _symbol(value, "enum mapping")))
    return tuple(scalars), tuple(enums)


def _schema(
    table: dict[str, object], name: str, root: Path, package: Path
) -> SchemaConfig:
    output = _path(table.get("schema_output"), root, "schema_output")
    expected = package / f"{name}.pyi"
    if output != expected:
        raise ValueError("schema_output must match generated_package/schema.pyi")
    _inside(output, package, "schema_output")
    scalars, enums = _mappings(table)
    return SchemaConfig(
        name=name,
        origin=_origin(table.get("schema"), root),
        schema_output=output,
        persisted_output=_path(table["persisted_output"], root, "persisted_output")
        if "persisted_output" in table
        else None,
        cache_output=_path(table["cache_output"], root, "cache_output")
        if "cache_output" in table
        else None,
        scalars=scalars,
        enums=enums,
        disable_masking=_boolean(table, "disable_masking", False),
    )


def generated_output_paths(
    root: Path, package: Path, schemas: Iterable[SchemaConfig]
) -> tuple[Path, ...]:
    """Enumerate canonical reserved outputs for config and project validation."""
    _inside(package, root, "generated_package")
    if package == root:
        raise ValueError("generated_package must lie below the project root")
    paths: list[Path] = []
    current = package
    while current != root:
        for suffix in (".py", ".pyi"):
            initializer = _path(
                str(current / f"__init__{suffix}"), root, "generated initializer"
            )
            _inside(initializer, current, "generated initializer")
            paths.append(initializer)
        current = current.parent
    for schema in schemas:
        output = _path(str(schema.schema_output), root, "schema_output")
        _inside(output, package, "schema_output")
        runtime = _path(
            str(schema.schema_output.with_suffix(".py")),
            root,
            "generated runtime companion",
        )
        _inside(runtime, package, "generated runtime companion")
        paths.extend((output, runtime))
        persisted = package / "persisted"
        for suffix in (".py", ".pyi"):
            paths.append(
                _path(
                    str(persisted / f"{schema.name}{suffix}"),
                    root,
                    "generated persisted binding",
                )
            )
        for destination in (schema.persisted_output, schema.cache_output):
            if destination is not None:
                paths.append(_path(str(destination), root, "generated output"))
    return tuple(paths)


def parse_project_config(
    text: str,
    *,
    config_path: Path,
    source_scope: Literal["project", "explicit_cli"] = "project",
) -> ProjectConfig | None:
    """Parse facts only; an absent table means this project is inactive.

    Does not resolve environment credentials, imported symbols, project ownership
    across files, source glob matches, entrypoint existence or write permissions.
    """
    try:
        document = cast(dict[str, object], tomllib.loads(text))
    except (ValueError, RecursionError) as error:
        offset = _toml_error_offset(text, error)
        raise ProjectConfigParseError(
            "Invalid pyproject TOML", offset, min(offset + 1, len(text))
        ) from None
    try:
        tool = _table(document.get("tool", {}), "tool")
        if "graphql-desk" not in tool:
            return None
        return parse_project_settings(
            _table(tool["graphql-desk"], "tool.graphql-desk"),
            config_path=config_path.resolve(),
            source_scope=source_scope,
        )
    except (ValueError, RecursionError) as error:
        start, end = _project_table_span(text)
        raise ProjectConfigParseError(
            "Invalid GraphQL Desk project configuration",
            start,
            end,
            original_message=str(error),
        ) from None


def parse_project_settings(
    settings: Mapping[str, object],
    *,
    config_path: Path,
    source_scope: Literal["project", "explicit_cli"] = "project",
) -> ProjectConfig:
    """Validate project settings shared by TOML and the editor fallback object.

    For editor fallback, config_path is a virtual workspace-relative context,
    not a claim that a pyproject was read. This function does not write it.
    """
    if source_scope not in ("project", "explicit_cli"):
        raise ValueError("Unknown source selection scope")
    table = _table(settings, "tool.graphql-desk")
    _known(table, _PROJECT_KEYS, "tool.graphql-desk")
    # A fallback's virtual filename must not follow an inactive pyproject symlink.
    path = config_path.parent.resolve() / config_path.name
    root = path.parent
    package_name = _string(
        table.get("generated_package", "graphql_desk_generated"), "generated_package"
    )
    for part in package_name.split("."):
        _identifier(part, "generated_package")
    package = root
    for part in package_name.split("."):
        package = _path(part, package, "generated_package")
        _inside(package, root, "generated_package")
        if package == root:
            raise ValueError("generated_package must lie below the project root")
    if not _boolean(table, "templateIsCallExpression", True):
        raise ValueError("templateIsCallExpression must be true for Python calls")
    cache_enabled = _boolean(table, "cache_enabled", False)
    if cache_enabled:
        raise ValueError("Persistent cache is not implemented; disable cache_enabled")
    schemas: list[SchemaConfig] = []
    if "schemas" in table:
        if table.keys() & _SCHEMA_KEYS:
            raise ValueError("schemas is mutually exclusive with single-schema fields")
        records = table["schemas"]
        if not isinstance(records, list) or not records:
            raise ValueError("schemas must be a nonempty array of schema records")
        for record in cast(list[object], records):
            item = _table(record, "schemas record")
            _known(item, _SCHEMA_KEYS | {"name"}, "schemas record")
            name = _identifier(item.get("name"), "schema name")
            if not _NAME.fullmatch(name):
                raise ValueError("Schema names must be GraphQL identifiers")
            if name in {"sources", "persisted", "parsers", "__init__"}:
                raise ValueError("Schema name collides with a generated namespace")
            schemas.append(_schema(item, name, root, package))
    else:
        schemas.append(_schema(table, "default", root, package))
    if len({item.name for item in schemas}) != len(schemas):
        raise ValueError("Schema names must be unique")
    used = {os.path.normcase(str(path)), os.path.normcase(str(path.resolve()))}
    for schema in schemas:
        if schema.origin.local_path is not None:
            used.add(os.path.normcase(str(schema.origin.local_path)))
    for output in generated_output_paths(root, package, schemas):
        identity = os.path.normcase(str(output))
        if output.is_dir() or any(
            Path(identity).is_relative_to(Path(other))
            or Path(other).is_relative_to(Path(identity))
            for other in used
        ):
            raise ValueError("Generated output paths collide")
        used.add(identity)
    refresh = table.get("schema_refresh_interval", 0)
    if isinstance(refresh, bool) or not isinstance(refresh, (int, float)):
        raise ValueError("schema_refresh_interval must be a nonnegative finite number")
    try:
        interval = float(refresh)
    except OverflowError:
        raise ValueError("schema_refresh_interval is not finite") from None
    if not math.isfinite(interval) or interval < 0:
        raise ValueError("schema_refresh_interval must be a nonnegative finite number")
    directives = _strings(table.get("client_directives", []), "client_directives")
    if any(not _NAME.fullmatch(name) for name in directives):
        raise ValueError("client_directives must contain GraphQL names without @")
    roots = tuple(
        dict.fromkeys(
            _path(value, root, "source_roots")
            for value in _strings(table.get("source_roots", ["."]), "source_roots")
        )
    )
    if source_scope == "project":
        for source_root in roots:
            _inside(source_root, root, "source_roots")
    authoring = tuple(
        dict.fromkeys(
            _path(value, root, "authoring_roots")
            for value in _strings(table.get("authoring_roots", []), "authoring_roots")
        )
    )
    if any(
        not any(value.is_relative_to(base) for base in roots) for value in authoring
    ):
        raise ValueError("authoring_roots must lie under source_roots")
    entrypoints = (
        _strings(table["entrypoints"], "entrypoints")
        if "entrypoints" in table
        else None
    )
    for value in entrypoints or ():
        if (
            "\\" in value
            or any(char in value for char in "*?{}![]")
            or value.startswith("/")
            or ".." in value.split("/")
            or not value.endswith(".py")
            or ":" in value
        ):
            raise ValueError("entrypoints must be exact root-relative Python paths")
    includes = _strings(table.get("include", ["**/*.py"]), "include")
    excludes = _strings(table.get("exclude", list(_EXCLUDE)), "exclude")
    for value in (*includes, *excludes):
        if (
            "\\" in value
            or value.startswith(("/", "!"))
            or ".." in value.split("/")
            or any(c in value for c in "{}[]:")
        ):
            raise ValueError(
                "include/exclude require relative /-separated glob patterns"
            )
    return ProjectConfig(
        path=path,
        root=root,
        generated_package=package_name,
        schemas=tuple(schemas),
        template=_symbol(table["template"], "template")
        if "template" in table
        else None,
        track_field_usage=_boolean(table, "track_field_usage", True),
        check_colocated_fragments=_boolean(table, "check_colocated_fragments", True),
        client_directives=directives,
        schema_refresh_interval=interval,
        disable_preprocessing=_boolean(table, "disable_preprocessing", False),
        cache_enabled=cache_enabled,
        source_roots=roots,
        include=includes,
        exclude=excludes,
        entrypoints=entrypoints,
        authoring_roots=authoring,
        source_scope=source_scope,
    )


def load_project_config(
    path: Path, *, source_scope: Literal["project", "explicit_cli"] = "project"
) -> ProjectConfig | None:
    """Read one explicitly supplied pyproject; never search or execute code."""
    return parse_project_config(
        path.read_text(encoding="utf-8"), config_path=path, source_scope=source_scope
    )
