"""Rules evaluated from the deterministic scan graph."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass
from typing import cast

from graphql import (
    FieldNode,
    FragmentDefinitionNode,
    GraphQLCompositeType,
    GraphQLInterfaceType,
    GraphQLObjectType,
    GraphQLSchema,
    GraphQLType,
    InlineFragmentNode,
    OperationDefinitionNode,
    SelectionSetNode,
    get_named_type,
    is_composite_type,
    parse,
)
from typing_extensions import NotRequired, TypedDict

from .fragments import FragmentIndex, SchemaContext
from .scan_graph import ScanDefinition, ScanGraph

_Coordinate = tuple[str, str, str, str]
_SourceKey = tuple[str, str, int, int]


class FieldUsageReference(TypedDict):
    project: str
    schemaName: str
    typeName: str
    fieldName: str


class FieldUsageDirectUsage(TypedDict):
    defId: str
    module: str


class FieldUsageReach(TypedDict):
    modules: tuple[str, ...]
    areas: tuple[str, ...]
    entryPoints: tuple[str, ...]


class FieldUsageData(TypedDict):
    project: str
    schemaName: str
    typeName: str
    fieldName: str
    fieldType: str
    deprecated: bool
    count: int
    directUsages: tuple[FieldUsageDirectUsage, ...]
    operations: tuple[str, ...]
    reach: FieldUsageReach
    deprecationReason: NotRequired[str]


@dataclass(frozen=True)
class FieldUsagePoint:
    ref: FieldUsageReference
    message: str
    data: FieldUsageData
    weight: int


def _fields(
    definition: OperationDefinitionNode | FragmentDefinitionNode,
    schema: GraphQLSchema,
    project: str,
    schema_name: str,
) -> dict[_Coordinate, tuple[str, str | None]]:
    if isinstance(definition, FragmentDefinitionNode):
        parent = schema.get_type(definition.type_condition.name.value)
    else:
        parent = {
            "query": schema.query_type,
            "mutation": schema.mutation_type,
            "subscription": schema.subscription_type,
        }[definition.operation.value]
    selected: dict[_Coordinate, tuple[str, str | None]] = {}

    def collect(selection_set: SelectionSetNode, type_: GraphQLCompositeType) -> None:
        for selection in selection_set.selections:
            if isinstance(selection, FieldNode):
                name = selection.name.value
                if isinstance(type_, (GraphQLObjectType, GraphQLInterfaceType)):
                    field = type_.fields.get(name)
                    if field is not None and not name.startswith("__"):
                        selected[(project, schema_name, type_.name, name)] = (
                            str(cast(object, field.type)),
                            field.deprecation_reason,
                        )
                    if field is not None and selection.selection_set is not None:
                        child = get_named_type(cast(GraphQLType, field.type))
                        if is_composite_type(child):
                            collect(selection.selection_set, child)
            elif isinstance(selection, InlineFragmentNode):
                condition = (
                    type_
                    if selection.type_condition is None
                    else schema.get_type(selection.type_condition.name.value)
                )
                if is_composite_type(condition):
                    collect(selection.selection_set, condition)

    if is_composite_type(parent):
        collect(definition.selection_set, parent)
    return selected


def field_usage_rule(
    graph: ScanGraph,
    index: FragmentIndex,
    schemas: Mapping[tuple[str, str | None], SchemaContext],
) -> tuple[FieldUsagePoint, ...]:
    """Count distinct schema fields per definition and their project reach."""
    documents = {
        (
            document.call.module.project,
            document.call.module.module,
            document.call.range.start,
            document.call.range.end,
        ): document
        for document in index.documents
    }
    parsed: dict[
        _SourceKey,
        dict[tuple[int, int], OperationDefinitionNode | FragmentDefinitionNode],
    ] = {}
    direct: dict[str, dict[_Coordinate, tuple[str, str | None]]] = {}
    definitions_by_id = {definition.id: definition for definition in graph.definitions}

    for definition in graph.definitions:
        source_key = definition.source_key
        span = definition.definition_range
        document = documents.get(source_key)
        if document is None:
            raise ValueError("Scan definitions must refer to indexed documents")
        schema_name = definition.schema_name
        context = schemas.get((definition.project, schema_name))
        if context is None or context.schema_name != schema_name:
            raise ValueError("Scan definitions must have their schema context")
        nodes = parsed.get(source_key)
        if nodes is None:
            nodes = {
                (node.loc.start, node.loc.end): node
                for node in parse(document.call.source).definitions
                if isinstance(node, (OperationDefinitionNode, FragmentDefinitionNode))
                and node.loc is not None
            }
            parsed[source_key] = nodes
        node = nodes.get((span.start, span.end))
        if node is None:
            raise ValueError("Scan definition range does not match its document")
        direct[definition.id] = _fields(
            node, context.schema, definition.project, schema_name
        )

    modules = {(module.project, module.name): module for module in graph.modules}
    if len(modules) != len(graph.modules):
        raise ValueError("Scan modules must have unique project/module identities")
    edges: dict[str, list[str]] = defaultdict(list)
    for edge in graph.fragment_edges:
        if edge.source_id in definitions_by_id and edge.target_id in definitions_by_id:
            edges[edge.source_id].append(edge.target_id)

    operation_ids: dict[_Coordinate, set[str]] = defaultdict(set)
    for operation in graph.definitions:
        if operation.kind == "fragment":
            continue
        reachable = {operation.id}
        pending = [operation.id]
        while pending:
            current = pending.pop()
            for target in edges[current]:
                if target not in reachable:
                    reachable.add(target)
                    pending.append(target)
        for definition_id in reachable:
            for coordinate in direct.get(definition_id, ()):
                operation_ids[coordinate].add(operation.id)

    direct_usages: dict[_Coordinate, list[ScanDefinition]] = defaultdict(list)
    fields: dict[_Coordinate, tuple[str, str | None]] = {}
    for definition in graph.definitions:
        for coordinate, field in direct.get(definition.id, {}).items():
            direct_usages[coordinate].append(definition)
            fields[coordinate] = field

    project_has_entries = {
        project: any(
            module.project == project and module.entrypoint for module in graph.modules
        )
        for project, _, _, _ in direct_usages
    }
    points: list[FieldUsagePoint] = []
    for coordinate, usages in direct_usages.items():
        project, schema_name, type_name, field_name = coordinate
        selected_ids = {definition.id for definition in usages}
        operation_names = operation_ids[coordinate]
        reaching_definitions = {*selected_ids, *operation_names}
        reached_modules: set[tuple[str, str]] = set()
        for definition_id in reaching_definitions:
            definition = definitions_by_id[definition_id]
            module_key = (definition.project, definition.module)
            module = modules.get(module_key)
            if module is None:
                raise ValueError("Scan definitions must refer to selected modules")
            reached_modules.update((project, name) for name in module.reverse_reachable)
        reached_facts = [modules[key] for key in reached_modules if key in modules]
        areas = tuple(sorted({module.area for module in reached_facts}))
        entry_points = tuple(
            sorted(module.name for module in reached_facts if module.entrypoint)
        )
        module_names = tuple(sorted(name for _, name in reached_modules))
        weight = (
            len(entry_points)
            if project_has_entries.get(project, False)
            else len(module_names)
        )
        field_type, reason = fields[coordinate]
        data: FieldUsageData = {
            "project": project,
            "schemaName": schema_name,
            "typeName": type_name,
            "fieldName": field_name,
            "fieldType": field_type,
            "deprecated": reason is not None,
            "count": len(selected_ids),
            "directUsages": tuple(
                {"defId": definition.id, "module": definition.module}
                for definition in sorted(
                    usages, key=lambda item: (item.id, item.module)
                )
            ),
            "operations": tuple(sorted(operation_names)),
            "reach": {
                "modules": module_names,
                "areas": areas,
                "entryPoints": entry_points,
            },
        }
        if reason is not None:
            data["deprecationReason"] = reason
        points.append(
            FieldUsagePoint(
                ref={
                    "project": project,
                    "schemaName": schema_name,
                    "typeName": type_name,
                    "fieldName": field_name,
                },
                message=(
                    f"{type_name}.{field_name} is selected by "
                    f"{len(selected_ids)} definitions"
                ),
                data=data,
                weight=weight,
            )
        )
    return tuple(
        sorted(
            points,
            key=lambda item: (
                -int(item.data["count"]),
                -item.weight,
                item.ref["project"],
                item.ref["schemaName"],
                item.ref["typeName"],
                item.ref["fieldName"],
            ),
        )
    )
