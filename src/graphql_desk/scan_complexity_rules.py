"""Static operation complexity scores from a schema-qualified scan graph.

The score ranks selected documents; it does not estimate server execution cost.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, cast

from graphql import (
    FieldNode,
    FragmentDefinitionNode,
    FragmentSpreadNode,
    GraphQLCompositeType,
    GraphQLInterfaceType,
    GraphQLList,
    GraphQLNonNull,
    GraphQLObjectType,
    GraphQLType,
    InlineFragmentNode,
    OperationDefinitionNode,
    SelectionSetNode,
    get_named_type,
    is_composite_type,
    parse,
)
from typing_extensions import TypedDict

from .fragments import FragmentIndex, SchemaContext
from .scan_graph import ScanGraph

_SourceKey = tuple[str, str, int, int]
_Coordinate = tuple[str, str]


class OperationComplexityReference(TypedDict):
    project: str
    schemaName: str
    operationId: str
    operationName: str | None
    module: str


class OperationComplexityData(TypedDict):
    depth: int
    fieldCount: int
    listFields: int
    fields: tuple[str, ...]
    score: int


@dataclass(frozen=True)
class OperationComplexityPoint:
    ref: OperationComplexityReference
    message: str
    data: OperationComplexityData
    weight: int


def _list_field(field_type: GraphQLType) -> bool:
    return isinstance(field_type, GraphQLList) or (
        isinstance(field_type, GraphQLNonNull)
        and isinstance(
            cast(Any, field_type).of_type,
            GraphQLList,
        )
    )


def operation_complexity_rule(
    graph: ScanGraph,
    index: FragmentIndex,
    schemas: Mapping[tuple[str, str | None], SchemaContext],
) -> tuple[OperationComplexityPoint, ...]:
    """Score operations by distinct reachable fields, depth, and list fields."""
    documents = {
        (
            document.call.module.project,
            document.call.module.module,
            document.call.range.start,
            document.call.range.end,
        ): document
        for document in index.documents
    }
    if len(documents) != len(index.documents):
        raise ValueError("Indexed scan documents must have unique identities")
    parsed: dict[
        _SourceKey,
        dict[tuple[int, int], OperationDefinitionNode | FragmentDefinitionNode],
    ] = {}
    ast_by_id: dict[str, OperationDefinitionNode | FragmentDefinitionNode] = {}
    definitions_by_id = {definition.id: definition for definition in graph.definitions}

    for definition in graph.definitions:
        document = documents.get(definition.source_key)
        if document is None:
            raise ValueError("Scan definitions must refer to indexed documents")
        nodes = parsed.get(definition.source_key)
        if nodes is None:
            nodes = {
                (node.loc.start, node.loc.end): node
                for node in parse(document.call.source).definitions
                if isinstance(node, (OperationDefinitionNode, FragmentDefinitionNode))
                and node.loc is not None
            }
            parsed[definition.source_key] = nodes
        node = nodes.get(
            (definition.definition_range.start, definition.definition_range.end)
        )
        if not isinstance(node, (OperationDefinitionNode, FragmentDefinitionNode)):
            raise ValueError("Scan definition range does not match its document")
        ast_by_id[definition.id] = node

    fragment_targets: dict[tuple[str, str], set[str]] = defaultdict(set)
    for edge in graph.fragment_edges:
        target = definitions_by_id.get(edge.target_id)
        if target is not None and target.kind == "fragment" and target.name is not None:
            fragment_targets[(edge.source_id, target.name)].add(target.id)

    points: list[OperationComplexityPoint] = []
    for operation in graph.definitions:
        if operation.kind not in {"query", "mutation", "subscription"}:
            continue
        context = schemas.get((operation.project, operation.schema_name))
        if context is None or context.schema_name != operation.schema_name:
            raise ValueError("Scan definitions must have their schema context")
        schema = context.schema
        node = ast_by_id[operation.id]
        if not isinstance(node, OperationDefinitionNode):
            raise ValueError("Operation scan definitions must point to operations")
        root: GraphQLCompositeType | None = {
            "query": schema.query_type,
            "mutation": schema.mutation_type,
            "subscription": schema.subscription_type,
        }[operation.kind]
        if root is None:
            continue

        fields: dict[_Coordinate, GraphQLType] = {}
        maximum_depth = 0

        def walk(
            definition_id: str,
            selection_set: SelectionSetNode,
            parent: GraphQLCompositeType,
            depth: int,
            active_fragments: frozenset[str],
        ) -> None:
            nonlocal maximum_depth
            for selection in selection_set.selections:
                if isinstance(selection, FieldNode):
                    name = selection.name.value
                    field = (
                        parent.fields.get(name)
                        if isinstance(parent, (GraphQLObjectType, GraphQLInterfaceType))
                        else None
                    )
                    if field is None or name.startswith("__"):
                        continue
                    fields[(parent.name, name)] = field.type
                    next_depth = depth + 1
                    maximum_depth = max(maximum_depth, next_depth)
                    if selection.selection_set is not None:
                        child = get_named_type(cast(GraphQLType, field.type))
                        if is_composite_type(child):
                            walk(
                                definition_id,
                                selection.selection_set,
                                child,
                                next_depth,
                                active_fragments,
                            )
                elif isinstance(selection, InlineFragmentNode):
                    condition = (
                        parent
                        if selection.type_condition is None
                        else schema.get_type(selection.type_condition.name.value)
                    )
                    if is_composite_type(condition):
                        walk(
                            definition_id,
                            selection.selection_set,
                            condition,
                            depth,
                            active_fragments,
                        )
                elif isinstance(selection, FragmentSpreadNode):
                    targets = fragment_targets.get(
                        (definition_id, selection.name.value), set()
                    )
                    if len(targets) != 1:
                        continue
                    target_id = next(iter(targets))
                    if target_id in active_fragments:
                        continue
                    target_node = ast_by_id.get(target_id)
                    target_definition = definitions_by_id.get(target_id)
                    if not isinstance(target_node, FragmentDefinitionNode):
                        continue
                    if target_definition is None:
                        continue
                    condition = schema.get_type(target_node.type_condition.name.value)
                    if is_composite_type(condition):
                        walk(
                            target_id,
                            target_node.selection_set,
                            condition,
                            depth,
                            active_fragments | {target_id},
                        )

        walk(operation.id, node.selection_set, root, 0, frozenset())
        field_names = tuple(sorted(f"{type_name}.{name}" for type_name, name in fields))
        list_count = sum(_list_field(field_type) for field_type in fields.values())
        field_count = len(fields)
        score = field_count + maximum_depth**2 + 3 * list_count
        points.append(
            OperationComplexityPoint(
                ref={
                    "project": operation.project,
                    "schemaName": operation.schema_name,
                    "operationId": operation.id,
                    "operationName": operation.name,
                    "module": operation.module,
                },
                message=(
                    f"{operation.name or operation.id} has static complexity score "
                    f"{score}; this is not a server execution-cost estimate"
                ),
                data={
                    "depth": maximum_depth,
                    "fieldCount": field_count,
                    "listFields": list_count,
                    "fields": field_names,
                    "score": score,
                },
                weight=score,
            )
        )
    return tuple(
        sorted(
            points,
            key=lambda point: (
                -point.data["score"],
                point.ref["project"],
                point.ref["schemaName"],
                point.ref["operationName"] or "",
                point.ref["module"],
                point.ref["operationId"],
            ),
        )
    )
