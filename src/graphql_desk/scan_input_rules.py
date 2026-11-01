"""Input-field and enum-literal usage facts for the experimental scan command."""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal, cast

from graphql import (
    EnumValueNode,
    FragmentDefinitionNode,
    GraphQLEnumType,
    GraphQLInputObjectType,
    ObjectFieldNode,
    OperationDefinitionNode,
    TypeInfo,
    get_named_type,
    parse,
    visit,
)
from graphql.language import Visitor
from graphql.utilities import TypeInfoVisitor
from typing_extensions import TypedDict

from .fragments import FragmentIndex, SchemaContext
from .scan_graph import ScanDefinition, ScanGraph

_Coordinate = tuple[str, str, str, str]
_SourceKey = tuple[str, str, int, int]
_InputKind = Literal["enum-value", "input-field"]


class InputUsageReference(TypedDict):
    project: str
    schemaName: str
    typeName: str
    fieldName: str


class InputUsageData(TypedDict):
    kind: _InputKind
    count: int
    definitionIds: tuple[str, ...]


@dataclass(frozen=True)
class InputUsagePoint:
    ref: InputUsageReference
    message: str
    data: InputUsageData
    weight: int


class _InputUsageVisitor(Visitor):
    def __init__(
        self,
        type_info: TypeInfo,
        definition: ScanDefinition,
        counts: Counter[tuple[_Coordinate, _InputKind]],
        definition_ids: defaultdict[tuple[_Coordinate, _InputKind], set[str]],
    ) -> None:
        super().__init__()
        self.type_info = type_info
        self.definition = definition
        self.counts = counts
        self.definition_ids = definition_ids

    def enter_object_field(self, node: ObjectFieldNode, *_args: object) -> None:
        parent = cast(Any, self.type_info).get_parent_input_type()
        if (
            not isinstance(parent, GraphQLInputObjectType)
            or node.name.value not in parent.fields
        ):
            return
        coordinate = (
            self.definition.project,
            self.definition.schema_name,
            parent.name,
            node.name.value,
        )
        self.counts[(coordinate, "input-field")] += 1
        self.definition_ids[(coordinate, "input-field")].add(self.definition.id)

    def enter_enum_value(self, node: EnumValueNode, *_args: object) -> None:
        type_info = cast(Any, self.type_info)
        enum = cast(Any, get_named_type)(type_info.get_input_type())
        if not isinstance(enum, GraphQLEnumType) or node.value not in enum.values:
            return
        coordinate = (
            self.definition.project,
            self.definition.schema_name,
            enum.name,
            node.value,
        )
        self.counts[(coordinate, "enum-value")] += 1
        self.definition_ids[(coordinate, "enum-value")].add(self.definition.id)


def input_usage_rule(
    graph: ScanGraph,
    index: FragmentIndex,
    schemas: Mapping[tuple[str, str | None], SchemaContext],
) -> tuple[InputUsagePoint, ...]:
    """Count schema-typed input object keys and enum literals in source ASTs."""
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
    counts: Counter[tuple[_Coordinate, _InputKind]] = Counter()
    definition_ids: defaultdict[tuple[_Coordinate, _InputKind], set[str]] = defaultdict(
        set
    )

    for definition in graph.definitions:
        document = documents.get(definition.source_key)
        if document is None:
            raise ValueError("Scan definitions must refer to indexed documents")
        context = schemas.get((definition.project, definition.schema_name))
        if context is None or context.schema_name != definition.schema_name:
            raise ValueError("Scan definitions must have their schema context")
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
            (
                definition.definition_range.start,
                definition.definition_range.end,
            )
        )
        if node is None:
            raise ValueError("Scan definition range does not match its document")
        type_info = TypeInfo(context.schema)
        visitor = _InputUsageVisitor(type_info, definition, counts, definition_ids)
        visit(node, TypeInfoVisitor(type_info, visitor))

    points: list[InputUsagePoint] = []
    for (coordinate, kind), count in counts.items():
        project, schema_name, type_name, field_name = coordinate
        points.append(
            InputUsagePoint(
                ref={
                    "project": project,
                    "schemaName": schema_name,
                    "typeName": type_name,
                    "fieldName": field_name,
                },
                message=f"{type_name}.{field_name} appears {count} times",
                data={
                    "kind": kind,
                    "count": count,
                    "definitionIds": tuple(sorted(definition_ids[(coordinate, kind)])),
                },
                weight=count,
            )
        )
    return tuple(
        sorted(
            points,
            key=lambda item: (
                -item.data["count"],
                item.ref["project"],
                item.ref["schemaName"],
                item.ref["typeName"],
                item.ref["fieldName"],
            ),
        )
    )
