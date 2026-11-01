"""Deprecated-field usage insights for the deterministic scan graph."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from typing_extensions import NotRequired, TypedDict

from .fragments import FragmentIndex, SchemaContext
from .scan_graph import ScanGraph
from .scan_rules import FieldUsageReference, field_usage_rule


class DeprecatedUsageData(TypedDict):
    count: int
    operations: tuple[str, ...]
    reason: NotRequired[str]


@dataclass(frozen=True)
class DeprecatedUsagePoint:
    ref: FieldUsageReference
    message: str
    data: DeprecatedUsageData
    weight: int


def deprecated_usage_rule(
    graph: ScanGraph,
    index: FragmentIndex,
    schemas: Mapping[tuple[str, str | None], SchemaContext],
) -> tuple[DeprecatedUsagePoint, ...]:
    """Rank selected deprecated fields by distinct reaching operation count."""
    points: list[DeprecatedUsagePoint] = []
    for field in field_usage_rule(graph, index, schemas):
        if not field.data["deprecated"]:
            continue
        operations = field.data["operations"]
        data: DeprecatedUsageData = {"count": len(operations), "operations": operations}
        reason = field.data.get("deprecationReason")
        if reason is not None:
            data["reason"] = reason
        type_name = field.ref["typeName"]
        field_name = field.ref["fieldName"]
        points.append(
            DeprecatedUsagePoint(
                ref=field.ref,
                message=(
                    f"{type_name}.{field_name} is deprecated and selected by "
                    f"{len(operations)} operations"
                ),
                data=data,
                weight=len(operations),
            )
        )
    return tuple(
        sorted(
            points,
            key=lambda point: (
                -point.data["count"],
                point.ref["project"],
                point.ref["schemaName"],
                point.ref["typeName"],
                point.ref["fieldName"],
            ),
        )
    )
