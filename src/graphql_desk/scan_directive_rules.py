"""Syntactic directive usage facts for the experimental scan command."""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass

from graphql import (
    DirectiveNode,
    FragmentDefinitionNode,
    OperationDefinitionNode,
    parse,
    visit,
)
from graphql.language import Visitor
from typing_extensions import TypedDict

from .fragments import FragmentIndex
from .scan_graph import ScanDefinition, ScanGraph

_SourceKey = tuple[str, str, int, int]
_Coordinate = tuple[str, str, str]


class DirectiveUsageReference(TypedDict):
    project: str
    schemaName: str
    directiveName: str


class DirectiveUsageData(TypedDict):
    count: int
    definitionIds: tuple[str, ...]


@dataclass(frozen=True)
class DirectiveUsagePoint:
    ref: DirectiveUsageReference
    message: str
    data: DirectiveUsageData
    weight: int


class _DirectiveVisitor(Visitor):
    def __init__(self, definition: ScanDefinition) -> None:
        super().__init__()
        self.definition = definition
        self.counts: Counter[_Coordinate] = Counter()

    def enter_directive(self, node: DirectiveNode, *_args: object) -> None:
        self.counts[
            (
                self.definition.project,
                self.definition.schema_name,
                node.name.value,
            )
        ] += 1


def directive_usage_rule(
    graph: ScanGraph, index: FragmentIndex
) -> tuple[DirectiveUsagePoint, ...]:
    """Count directive applications in the original embedded GraphQL source."""
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
    counts: Counter[_Coordinate] = Counter()
    definition_ids: defaultdict[_Coordinate, set[str]] = defaultdict(set)

    for definition in graph.definitions:
        source_key = definition.source_key
        document = documents.get(source_key)
        if document is None:
            raise ValueError("Scan definitions must refer to indexed documents")
        nodes = parsed.get(source_key)
        if nodes is None:
            nodes = {
                (node.loc.start, node.loc.end): node
                for node in parse(document.call.source).definitions
                if isinstance(node, (OperationDefinitionNode, FragmentDefinitionNode))
                and node.loc is not None
            }
            parsed[source_key] = nodes
        node = nodes.get(
            (definition.definition_range.start, definition.definition_range.end)
        )
        if node is None:
            raise ValueError("Scan definition range does not match its document")
        visitor = _DirectiveVisitor(definition)
        visit(node, visitor)
        counts.update(visitor.counts)
        for coordinate in visitor.counts:
            definition_ids[coordinate].add(definition.id)

    points: list[DirectiveUsagePoint] = []
    for (project, schema_name, directive_name), count in counts.items():
        points.append(
            DirectiveUsagePoint(
                ref={
                    "project": project,
                    "schemaName": schema_name,
                    "directiveName": directive_name,
                },
                message=f"@{directive_name} is applied {count} times",
                data={
                    "count": count,
                    "definitionIds": tuple(
                        sorted(definition_ids[(project, schema_name, directive_name)])
                    ),
                },
                weight=count,
            )
        )
    return tuple(
        sorted(
            points,
            key=lambda point: (
                -point.data["count"],
                point.ref["directiveName"],
                point.ref["project"],
                point.ref["schemaName"],
            ),
        )
    )
