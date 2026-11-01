"""Stable machine and terminal reports for the experimental project scan."""

from __future__ import annotations

import json
import textwrap
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from typing import Any

from graphql import GraphQLInterfaceType, GraphQLObjectType

from .fragments import SchemaContext
from .scan_graph import ScanDefinition, ScanGraph


@dataclass(frozen=True)
class ScanReport:
    """JSON-safe result of one read-only scan."""

    overview: dict[str, Any]
    operations: tuple[dict[str, Any], ...]
    fragments: tuple[dict[str, Any], ...]
    rules: dict[str, tuple[dict[str, Any], ...]]
    warnings: tuple[dict[str, Any], ...]
    graph: dict[str, tuple[dict[str, str], ...]]
    terminal_locations: dict[tuple[str, int], tuple[tuple[str, int, int], ...]]


def _finding(point: Any) -> dict[str, Any]:
    values = asdict(point)
    values.get("data", {}).pop("definitionIds", None)
    return {
        key: values[key]
        for key in ("ref", "message", "data", "weight")
        if key in values
    }


def _definition_record(
    definition: ScanDefinition, module_paths: dict[tuple[str, str], str]
) -> dict[str, Any]:
    location = definition.locations[0] if definition.locations else None
    record: dict[str, Any] = {
        "id": definition.id,
        "name": definition.name,
        "schemaName": definition.schema_name,
        "module": definition.module,
        "loc": (
            {
                "file": module_paths[(location.project, location.module)],
                "line": location.line,
                "column": location.column,
            }
            if location is not None
            else None
        ),
        "variables": [
            {
                "name": name,
                "type": type_,
                **({"defaultValue": default} if default is not None else {}),
            }
            for name, type_, default in definition.variables
        ],
        "fragmentSpreads": list(definition.fragment_spreads),
        "hash": definition.hash,
    }
    if definition.kind == "fragment":
        record["typeCondition"] = definition.type_condition
    else:
        record["kind"] = definition.kind
    return record


def build_scan_report(
    graph: ScanGraph,
    schemas: Mapping[str, SchemaContext],
    rules: Mapping[str, tuple[Any, ...]],
    warnings: tuple[dict[str, Any], ...],
) -> ScanReport:
    """Build the version-1 report while keeping scan facts schema-qualified."""
    module_paths = {
        (module.project, module.name): module.path for module in graph.modules
    }
    operations = tuple(
        _definition_record(item, module_paths)
        for item in graph.definitions
        if item.kind != "fragment"
    )
    fragments = tuple(
        _definition_record(item, module_paths)
        for item in graph.definitions
        if item.kind == "fragment"
    )
    definition_records = {item["id"]: item for item in (*operations, *fragments)}
    serialized_rules: dict[str, tuple[dict[str, Any], ...]] = {}
    terminal_locations: dict[tuple[str, int], tuple[tuple[str, int, int], ...]] = {}
    for name, points in rules.items():
        serialized: list[dict[str, Any]] = []
        for index, point in enumerate(points):
            values = asdict(point)
            ids = values.get("data", {}).get("definitionIds", ())
            locations = {
                (
                    definition_records[identity]["loc"]["file"],
                    definition_records[identity]["loc"]["line"],
                    definition_records[identity]["loc"]["column"],
                )
                for identity in ids
                if identity in definition_records
                and definition_records[identity].get("loc")
            }
            if locations:
                terminal_locations[(name, index)] = tuple(sorted(locations))
            serialized.append(_finding(point))
        serialized_rules[name] = tuple(serialized)
    field_points = rules["field-usage"]
    used_coordinates = {
        (
            point.ref["project"],
            point.ref["schemaName"],
            point.ref["typeName"],
            point.ref["fieldName"],
        )
        for point in field_points
    }
    schema_fields: set[tuple[str, str, str, str]] = set()
    project_ids = {item.project for item in graph.modules}
    project_id = next(iter(project_ids), "project")
    for context in schemas.values():
        for type_name, type_ in context.schema.type_map.items():
            if type_name.startswith("__") or not isinstance(
                type_, (GraphQLObjectType, GraphQLInterfaceType)
            ):
                continue
            schema_fields.update(
                (project_id, context.schema_name, type_name, field_name)
                for field_name in type_.fields
                if not field_name.startswith("__")
            )
    total_fields = len(schema_fields)
    used_fields = len(schema_fields & used_coordinates)
    percent = (
        100
        if not total_fields
        else (200 * used_fields + total_fields) // (2 * total_fields)
    )
    operations_by_kind = {
        kind: sum(item.kind == kind for item in graph.definitions)
        for kind in ("query", "mutation", "subscription")
    }
    coverage_by_schema: dict[str, tuple[int, int, int]] = {}
    for schema_name in sorted({context.schema_name for context in schemas.values()}):
        selected = {
            coordinate for coordinate in schema_fields if coordinate[1] == schema_name
        }
        used = len(selected & used_coordinates)
        total = len(selected)
        coverage_by_schema[schema_name] = (
            used,
            total,
            100 if not total else (200 * used + total) // (2 * total),
        )

    module_nodes = {
        module.name: {
            "id": f"module:{module.name}",
            "kind": "module",
            "label": module.path,
        }
        for module in graph.modules
    }
    nodes: dict[str, dict[str, str]] = {
        item["id"]: item for item in module_nodes.values()
    }
    edges: set[tuple[str, str, str]] = {
        (f"module:{module.name}", f"module:{imported}", "imports")
        for module in graph.modules
        for imported in module.imports
        if imported in module_nodes
    }
    def_node_ids: dict[str, str] = {}
    for definition in graph.definitions:
        kind = "fragment" if definition.kind == "fragment" else "operation"
        node_id = f"{kind}:{definition.id}"
        def_node_ids[definition.id] = node_id
        label = definition.name or f"anonymous {definition.kind}"
        nodes[node_id] = {"id": node_id, "kind": kind, "label": label}
        edges.add((f"module:{definition.module}", node_id, "defines"))
    for edge in graph.fragment_edges:
        source = def_node_ids.get(edge.source_id)
        target = def_node_ids.get(edge.target_id)
        if source is not None and target is not None:
            edges.add((source, target, "spreads"))
    for definition in graph.definitions:
        if definition.kind != "fragment" or definition.type_condition is None:
            continue
        type_id = f"schemaType:{definition.schema_name}:{definition.type_condition}"
        nodes[type_id] = {
            "id": type_id,
            "kind": "schemaType",
            "label": definition.type_condition,
        }
        edges.add((def_node_ids[definition.id], type_id, "onType"))
    for point in field_points:
        ref = point.ref
        schema_name = ref["schemaName"]
        type_name = ref["typeName"]
        field_name = ref["fieldName"]
        type_id = f"schemaType:{schema_name}:{type_name}"
        field_id = f"schemaField:{schema_name}:{type_name}.{field_name}"
        nodes[type_id] = {"id": type_id, "kind": "schemaType", "label": type_name}
        nodes[field_id] = {
            "id": field_id,
            "kind": "schemaField",
            "label": f"{type_name}.{field_name}: {point.data['fieldType']}",
        }
        for usage in point.data["directUsages"]:
            source = def_node_ids.get(usage["defId"])
            if source is not None:
                edges.add((source, field_id, "selects"))
    graph_report: dict[str, tuple[dict[str, str], ...]] = {
        "nodes": tuple(nodes[key] for key in sorted(nodes)),
        "edges": tuple(
            {"from": source, "to": target, "kind": kind}
            for source, target, kind in sorted(edges)
        ),
    }
    return ScanReport(
        overview={
            "operations": len(operations),
            "fragments": len(fragments),
            "modules": len(graph.modules),
            "operationsByKind": operations_by_kind,
            "coverage": {
                "usedFields": used_fields,
                "totalFields": total_fields,
                "percent": percent,
            },
            "coverageBySchema": {
                name: {
                    "usedFields": values[0],
                    "totalFields": values[1],
                    "percent": values[2],
                }
                for name, values in coverage_by_schema.items()
            },
        },
        operations=operations,
        fragments=fragments,
        rules=serialized_rules,
        warnings=warnings,
        graph=graph_report,
        terminal_locations=terminal_locations,
    )


def json_report(report: ScanReport, *, graph: bool = False) -> str:
    """Return deterministic machine output with a trailing newline."""
    payload: object = (
        report.graph
        if graph
        else {
            "version": 1,
            "overview": report.overview,
            "operations": report.operations,
            "fragments": report.fragments,
            "rules": report.rules,
            "warnings": report.warnings,
        }
    )
    return json.dumps(payload, ensure_ascii=False, indent=2) + "\n"


def _point_locations(
    point: dict[str, Any], definitions: Mapping[str, dict[str, Any]]
) -> str:
    ref = point["ref"]
    data = point["data"]
    ids = [ref.get("operationId"), ref.get("fragmentId")]
    ids.extend(item["defId"] for item in data.get("directUsages", ()))
    ids.extend(data.get("operations", ()))
    ids.extend(data.get("definitionIds", ()))
    locations = {
        (item["loc"]["file"], item["loc"]["line"], item["loc"]["column"])
        for identity in ids
        if identity in definitions and definitions[identity].get("loc")
        for item in (definitions[identity],)
    }
    return ", ".join(
        f"{file}:{line}:{column}" for file, line, column in sorted(locations)
    )


def terminal_report(report: ScanReport, *, width: int | None = None) -> str:
    """Render ranked insight sections; ``None`` keeps pipe output unbounded."""
    definitions = {item["id"]: item for item in (*report.operations, *report.fragments)}
    lines = [
        "GraphQL Desk scan (experimental)",
        "Operations: {operations}  Fragments: {fragments}  Modules: {modules}".format(
            operations=report.overview["operations"],
            fragments=report.overview["fragments"],
            modules=report.overview["modules"],
        ),
        "Operations by kind: "
        + ", ".join(
            f"{kind} {count}"
            for kind, count in report.overview["operationsByKind"].items()
        ),
        "Schema coverage: "
        + "; ".join(
            f"{name} {value['usedFields']}/{value['totalFields']} fields "
            f"({value['percent']}%)"
            for name, value in report.overview["coverageBySchema"].items()
        ),
    ]
    for name, points in report.rules.items():
        lines.append(f"\n{name} ({len(points)})")
        for index, point in enumerate(points):
            ref = point["ref"]
            schema = ref.get("schemaName")
            label = next(
                (
                    str(ref[key])
                    for key in ("fieldName", "directiveName", "operationName", "name")
                    if ref.get(key) is not None
                ),
                str(ref.get("operationId", ref.get("fragmentId", ""))),
            )
            locations = ", ".join(
                f"{file}:{line}:{column}"
                for file, line, column in report.terminal_locations.get(
                    (name, index), ()
                )
            ) or _point_locations(point, definitions)
            location = f" [{locations}]" if locations else ""
            prefix = f"  {schema + ': ' if schema else ''}{label}{location}: "
            message = point["message"]
            if width is None or len(prefix + message) <= width:
                lines.append(prefix + message)
            else:
                lines.extend(
                    textwrap.wrap(
                        prefix + message,
                        width=max(width, 24),
                        subsequent_indent="    ",
                    )
                )
    if report.warnings:
        lines.append(f"\nAnalysis warnings: {len(report.warnings)}")
        for warning in report.warnings:
            where = (
                f"{warning['file']}:{warning['line']}:{warning['column']}: "
                if warning.get("file") is not None
                else ""
            )
            lines.append(f"  {where}{warning['code']}: {warning['message']}")
    else:
        lines.append("\nNo discovery or parse warnings.")
    if width is not None:
        lines = [
            wrapped
            for line in lines
            for wrapped in (
                textwrap.wrap(
                    line,
                    width=max(width, 24),
                    break_on_hyphens=False,
                )
                or [""]
            )
        ]
    return "\n".join(lines) + "\n"
