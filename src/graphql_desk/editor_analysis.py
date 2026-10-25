"""Pure JSON boundary for editor snapshots; no discovery, imports or transport."""

import json
from dataclasses import dataclass
from typing import cast
from urllib.parse import urlsplit

from .config import ScalarMapping
from .fragments import DefinitionFact, FragmentIndex, SchemaContext, index_fragments
from .schema_loading import load_from_introspection, load_from_sdl
from .schema_types import mapped_type_declarations
from .source import Span
from .source_graph import (
    DocumentIdentity,
    FactoryBinding,
    GraphDiagnostic,
    IncrementalResolver,
    ModuleDependency,
    ModuleSnapshot,
    ProjectBindings,
    resolve_project,
    resolve_type_symbols,
)
from .source_maps import build_source_map, convert_offset


def _object(value: object, keys: set[str], label: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError(f"Invalid {label} object fields")
    result = cast(dict[str, object], value)
    if set(result) != keys:
        raise ValueError(f"Invalid {label} object fields")
    return result


def _array(value: object, label: str) -> list[object]:
    if not isinstance(value, list):
        raise ValueError(f"{label} must be an array")
    return cast(list[object], value)


def _string(value: object, label: str, *, empty: bool = False) -> str:
    if not isinstance(value, str) or (not empty and not value):
        raise ValueError(f"{label} must be a string")
    return value


def _optional_string(value: object, label: str) -> str | None:
    return None if value is None else _string(value, label)


def _version(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("Snapshot version must be a nonnegative integer")
    return value


def _uri(value: object) -> str:
    uri = _string(value, "Snapshot URI")
    try:
        parts = urlsplit(uri)
    except ValueError:
        raise ValueError("Invalid snapshot URI") from None
    if (
        parts.scheme not in ("file", "untitled")
        or not parts.path
        or parts.query
        or parts.fragment
    ):
        raise ValueError("Snapshots require local file or untitled URIs")
    return uri


def _module(identity: ModuleDependency) -> dict[str, object]:
    return {
        "project": identity.project,
        "module": identity.module,
        "uri": identity.uri,
        "version": identity.version,
    }


def _span(span: Span) -> dict[str, int]:
    return {"start": span.start, "end": span.end}


def utf16_points(text: str, points: set[int]) -> dict[int, int]:
    result: dict[int, int] = {}
    previous = converted = 0
    for point in sorted(points):
        converted += convert_offset(text[previous:point], point - previous)
        result[point] = converted
        previous = point
    return result


def _pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON object field")
        result[key] = value
    return result


def _constant(_value: str) -> object:
    raise ValueError("Nonfinite JSON numbers are unsupported")


def _json(text: str) -> object:
    try:
        result: object = json.loads(
            text, object_pairs_hook=_pairs, parse_constant=_constant
        )
    except (ValueError, RecursionError):
        raise ValueError("Invalid JSON snapshot payload") from None
    return result


def analyze_editor_json(text: str) -> str:
    """Analyze one JSON value; caller owns process IO and freshness checks."""
    return json.dumps(analyze_editor_snapshot(_json(text)), ensure_ascii=True)


def analyze_editor_snapshot(
    request: object,
    *,
    resolver: IncrementalResolver | None = None,
    external_mapping_modules: tuple[ModuleSnapshot, ...] = (),
) -> dict[str, object]:
    """Resolve supplied facts without reading files or executing project Python.

    The caller must authorize workspace analysis and verify every source, config
    and schema version before publishing. This response is not a freshness proof.
    Invalid GraphQL leaves resolved source maps available for online completion.
    An explicit serialized resolver reuses source facts only, not schema/index work.
    """
    return analyze_editor_snapshot_details(
        request,
        resolver=resolver,
        external_mapping_modules=external_mapping_modules,
    ).response


@dataclass(frozen=True)
class EditorAnalysis:
    response: dict[str, object]
    modules: tuple[ModuleSnapshot, ...]
    schemas: dict[tuple[str, str | None], SchemaContext]
    index: FragmentIndex


def editor_diagnostic(
    item: GraphDiagnostic, snapshots: dict[ModuleDependency, str]
) -> dict[str, object]:
    source = snapshots[item.module]
    return {
        "module": _module(item.module),
        "code": item.code,
        "message": item.message,
        "range": {
            "codepoint": _span(item.range),
            "utf16": {
                "start": convert_offset(source, item.range.start),
                "end": convert_offset(source, item.range.end),
            },
        },
    }


def analyze_editor_snapshot_details(
    request: object,
    *,
    resolver: IncrementalResolver | None = None,
    external_mapping_modules: tuple[ModuleSnapshot, ...] = (),
) -> EditorAnalysis:
    payload = _object(
        request, {"request_id", "modules", "bindings", "schemas"}, "request"
    )
    request_id = _string(payload["request_id"], "Request identity")
    bindings: list[ProjectBindings] = []
    type_mappings: dict[str, list[dict[str, object]]] = {}
    for value in _array(payload["bindings"], "bindings"):
        item = _object(
            value,
            {"project", "schemas", "default_schema", "factories", "type_mappings"},
            "binding",
        )
        factories: list[FactoryBinding] = []
        for factory in _array(item["factories"], "factories"):
            record = _object(factory, {"module", "export", "schema_name"}, "factory")
            factories.append(
                FactoryBinding(
                    _string(record["module"], "Factory module"),
                    _string(record["export"], "Factory export"),
                    _optional_string(record["schema_name"], "Factory schema"),
                )
            )
        mapping_facts: list[dict[str, object]] = []
        project_id = _string(item["project"], "Project identity")
        schema_names = {
            _string(name, "Schema name")
            for name in _array(item["schemas"], "Schema names")
        }
        mapped_schemas: set[str] = set()
        for raw_mapping in _array(item["type_mappings"], "type mappings"):
            mapping = _object(
                raw_mapping, {"schema_name", "scalars", "enums"}, "schema type mapping"
            )
            scalars: list[dict[str, str]] = []
            for raw_scalar in _array(mapping["scalars"], "scalar mappings"):
                scalar = _object(
                    raw_scalar, {"name", "input", "output"}, "scalar mapping"
                )
                scalars.append(
                    {
                        "name": _string(scalar["name"], "Scalar name"),
                        "input": _string(scalar["input"], "Scalar input type"),
                        "output": _string(scalar["output"], "Scalar output type"),
                    }
                )
            enums: list[dict[str, str]] = []
            for raw_enum in _array(mapping["enums"], "enum mappings"):
                enum = _object(raw_enum, {"name", "type"}, "enum mapping")
                enums.append(
                    {
                        "name": _string(enum["name"], "Enum name"),
                        "type": _string(enum["type"], "Enum type"),
                    }
                )
            schema_name = _string(mapping["schema_name"], "Schema mapping name")
            if schema_name not in schema_names or schema_name in mapped_schemas:
                raise ValueError("Type mappings must uniquely match configured schemas")
            mapped_schemas.add(schema_name)
            mapping_facts.append(
                {
                    "schema_name": schema_name,
                    "scalars": scalars,
                    "enums": enums,
                }
            )
        if project_id in type_mappings:
            raise ValueError("Type mappings must uniquely match configured schemas")
        type_mappings[project_id] = mapping_facts
        bindings.append(
            ProjectBindings(
                project_id,
                tuple(
                    _string(name, "Schema name")
                    for name in _array(item["schemas"], "Schema names")
                ),
                _optional_string(item["default_schema"], "Default schema"),
                tuple(factories),
            )
        )
    projects = {item.project: item for item in bindings}
    modules: list[ModuleSnapshot] = []
    uris: set[str] = set()
    for value in _array(payload["modules"], "modules"):
        item = _object(
            value,
            {"project", "module", "uri", "version", "source", "is_package"},
            "module",
        )
        project = _string(item["project"], "Module project")
        if project not in projects:
            raise ValueError("Module project requires supplied binding facts")
        uri = _uri(item["uri"])
        if uri in uris:
            raise ValueError("Duplicate source URI")
        uris.add(uri)
        package = item["is_package"]
        if not isinstance(package, bool):
            raise ValueError("is_package must be a boolean")
        modules.append(
            ModuleSnapshot(
                project,
                _string(item["module"], "Module name"),
                uri,
                _version(item["version"]),
                _string(item["source"], "Module source", empty=True),
                package,
            )
        )
    resolve = resolve_project if resolver is None else resolver.resolve
    sources = resolve(tuple(modules), bindings=tuple(bindings))
    schemas: dict[tuple[str, str | None], SchemaContext] = {}
    schema_facts: list[dict[str, object]] = []
    seen_schemas: set[tuple[str, str]] = set()
    for value in _array(payload["schemas"], "schemas"):
        item = _object(
            value,
            {"project", "schema_name", "uri", "version", "token", "format", "text"},
            "schema",
        )
        project = _string(item["project"], "Schema project")
        name = _string(item["schema_name"], "Schema name")
        identity = (project, name)
        if (
            project not in projects
            or name not in projects[project].schemas
            or identity in seen_schemas
        ):
            raise ValueError(
                "Schema identity must be unique and match supplied bindings"
            )
        seen_schemas.add(identity)
        uri = _uri(item["uri"])
        version = _version(item["version"])
        token = _string(item["token"], "Schema token")
        format_ = _string(item["format"], "Schema format")
        text = _string(item["text"], "Schema text")
        try:
            if format_ == "sdl":
                schema = load_from_sdl(text)
            elif format_ == "introspection":
                data = _json(text)
                if not isinstance(data, dict):
                    raise ValueError("Introspection must be an object")
                schema = load_from_introspection(cast(dict[str, object], data))
            else:
                raise ValueError("Unknown schema format")
        except ValueError:
            raise ValueError("Invalid supplied local schema") from None
        context = SchemaContext(name, schema, token)
        schemas[identity] = context
        if projects[project].default_schema == name:
            schemas[(project, None)] = context
        schema_facts.append(
            {
                "project": project,
                "schema_name": name,
                "uri": uri,
                "version": version,
                "token": token,
                "format": format_,
            }
        )
    index = index_fragments(sources, schemas=schemas)
    mapping_diagnostics: list[GraphDiagnostic] = []
    for project, project_mappings in type_mappings.items():
        for mapping_record in project_mappings:
            schema_name = cast(str, mapping_record["schema_name"])
            schema_context = schemas.get((project, schema_name))
            if schema_context is None:
                continue
            scalar_facts = cast(list[dict[str, str]], mapping_record["scalars"])
            enum_facts = cast(list[dict[str, str]], mapping_record["enums"])
            try:
                mapped_type_declarations(
                    schema_context.schema,
                    tuple(
                        ScalarMapping(item["name"], item["input"], item["output"])
                        for item in scalar_facts
                    ),
                    tuple((item["name"], item["type"]) for item in enum_facts),
                )
            except ValueError as error:
                message = f"Configured type mapping is invalid: {error}"
                for call in sources.calls:
                    if (
                        call.module.project == project
                        and call.schema_name == schema_name
                    ):
                        mapping_diagnostics.append(
                            GraphDiagnostic(
                                call.module, "mapping-invalid", message, call.range
                            )
                        )
                continue
            symbols = tuple(
                symbol
                for item in scalar_facts
                for symbol in (item["input"], item["output"])
            ) + tuple(item["type"] for item in enum_facts)
            external_project = project + "#interpreter"
            external_modules = tuple(
                module
                for module in external_mapping_modules
                if module.project == external_project
            )
            local_module_names = {
                module.module for module in modules if module.project == project
            }
            type_modules = (
                *modules,
                *(
                    ModuleSnapshot(
                        project,
                        module.module,
                        module.uri,
                        module.version,
                        module.source,
                        module.is_package,
                    )
                    for module in external_modules
                    if module.module not in local_module_names
                ),
            )
            resolutions = {
                result.symbol: result
                for result in resolve_type_symbols(
                    symbols, modules=type_modules, project_id=project
                )
            }
            statuses: list[tuple[str, str, str]] = []
            for scalar_mapping in scalar_facts:
                statuses.extend(
                    (scalar_mapping["name"], direction, scalar_mapping[direction])
                    for direction in ("input", "output")
                )
            statuses.extend(
                (enum_mapping["name"], "enum", enum_mapping["type"])
                for enum_mapping in enum_facts
            )
            for name, direction, symbol in statuses:
                resolution = resolutions[symbol]
                if resolution.status == "resolved" or symbol == "typing.Any":
                    continue
                if resolution.status == "external":
                    code = "mapping-unverified"
                    detail = (
                        "is outside the captured project snapshot; "
                        "static verification was skipped"
                    )
                else:
                    code = "mapping-unresolved"
                    detail = resolution.message or f"has status {resolution.status}"
                message = (
                    f"Configured {direction} mapping for {name} to {symbol!r} {detail}."
                )
                for call in sources.calls:
                    if (
                        call.module.project == project
                        and call.schema_name == schema_name
                    ):
                        mapping_diagnostics.append(
                            GraphDiagnostic(call.module, code, message, call.range)
                        )
    indexed = {
        DocumentIdentity(item.call.module, item.call.range): item
        for item in index.documents
    }
    snapshots = {
        ModuleDependency(item.project, item.module, item.uri, item.version): item.source
        for item in modules
    }
    mappings = [
        (call, build_source_map(call, tuple(modules))) for call in sources.calls
    ]
    points: dict[ModuleDependency, set[int]] = {item: set() for item in snapshots}

    def require(module: ModuleDependency, span: Span) -> None:
        points[module].update((span.start, span.end))

    for call, source_map in mappings:
        require(call.module, call.range)
        for atom in source_map.atoms:
            require(atom.host.module, atom.host.range)
        for reference in call.fragments:
            require(reference.target.module, reference.target.call_range)
            require(reference.reference_module, reference.range)
    for diagnostic in index.diagnostics:
        require(diagnostic.module, diagnostic.range)
    for document in index.documents:
        for fact in document.definitions:
            require(fact.document.module, fact.document.call_range)
    offsets = {
        module: utf16_points(text, points[module]) for module, text in snapshots.items()
    }

    def range_(module: ModuleDependency, span: Span) -> dict[str, object]:
        return {
            "codepoint": _span(span),
            "utf16": _span(
                Span(
                    offsets[module][span.start],
                    offsets[module][span.end],
                )
            ),
        }

    def document_(identity: DocumentIdentity) -> dict[str, object]:
        return {
            "module": _module(identity.module),
            "call_range": range_(identity.module, identity.call_range),
        }

    def definition_(fact: DefinitionFact | None) -> dict[str, object] | None:
        if fact is None:
            return None
        return {
            "document": document_(fact.document),
            "kind": fact.kind,
            "name": fact.name,
            "type_condition": fact.type_condition,
            "printed": fact.printed,
        }

    calls: list[dict[str, object]] = []
    for call, source_map in mappings:
        call_identity = DocumentIdentity(call.module, call.range)
        decoded_offsets = utf16_points(
            call.source,
            {
                point
                for atom in source_map.atoms
                for point in (atom.decoded.start, atom.decoded.end)
            },
        )
        codepoint: list[dict[str, object]] = []
        utf16: list[dict[str, object]] = []
        for atom in source_map.atoms:
            codepoint.append(
                {
                    "decoded": _span(atom.decoded),
                    "host": {
                        "module": _module(atom.host.module),
                        "range": _span(atom.host.range),
                    },
                    "linear": atom.linear,
                }
            )
            utf16.append(
                {
                    "decoded": _span(
                        Span(
                            decoded_offsets[atom.decoded.start],
                            decoded_offsets[atom.decoded.end],
                        )
                    ),
                    "host": {
                        "module": _module(atom.host.module),
                        "range": range_(atom.host.module, atom.host.range)["utf16"],
                    },
                    "linear": atom.linear,
                }
            )
        entry = indexed.get(call_identity)
        calls.append(
            {
                "identity": document_(call_identity),
                "schema_name": call.schema_name,
                "source": call.source,
                "operation": call.operation,
                "dependencies": [_module(item) for item in call.dependencies],
                "fragments": [
                    {
                        "target": document_(item.target),
                        "reference_module": _module(item.reference_module),
                        "range": range_(item.reference_module, item.range),
                    }
                    for item in call.fragments
                ],
                "map": {"codepoint": codepoint, "utf16": utf16},
                "index": None
                if entry is None
                else {
                    "schema_name": entry.schema_identity[1],
                    "context_token": entry.context_token,
                    "selected_operation": definition_(entry.selected_operation),
                    "primary_fragment": definition_(entry.primary_fragment),
                    "definitions": [definition_(fact) for fact in entry.definitions],
                    "dependencies": [_module(item) for item in entry.dependencies],
                },
            }
        )
    response: dict[str, object] = {
        "request_id": request_id,
        "modules": [_module(item) for item in snapshots],
        "schemas": schema_facts,
        "calls": calls,
        "diagnostics": [
            editor_diagnostic(item, snapshots) for item in index.diagnostics
        ]
        + [editor_diagnostic(item, snapshots) for item in mapping_diagnostics],
    }
    return EditorAnalysis(response, tuple(modules), schemas, index)
