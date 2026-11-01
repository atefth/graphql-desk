"""Import-distance depth facts for query operations in a scan graph."""

from __future__ import annotations

from dataclasses import dataclass

from typing_extensions import TypedDict

from .scan_graph import ScanGraph


class FetchDepthReference(TypedDict):
    project: str
    schemaName: str
    operationId: str
    operationName: str | None
    module: str


class FetchDepthData(TypedDict):
    depth: int | None
    area: str
    module: str


@dataclass(frozen=True)
class FetchDepthPoint:
    ref: FetchDepthReference
    message: str
    data: FetchDepthData
    weight: int | None


def fetch_depth_rule(graph: ScanGraph) -> tuple[FetchDepthPoint, ...]:
    """Rank query operations by their module's shortest import-edge distance."""
    modules = {(module.project, module.name): module for module in graph.modules}
    if len(modules) != len(graph.modules):
        raise ValueError("Scan modules must have unique project/module identities")

    points: list[FetchDepthPoint] = []
    for definition in graph.definitions:
        if definition.kind != "query":
            continue
        module = modules.get((definition.project, definition.module))
        if module is None:
            raise ValueError("Query definitions must refer to selected modules")
        depth = module.distance
        points.append(
            FetchDepthPoint(
                ref={
                    "project": definition.project,
                    "schemaName": definition.schema_name,
                    "operationId": definition.id,
                    "operationName": definition.name,
                    "module": definition.module,
                },
                message=(
                    f"Query {definition.name or definition.id} has import depth "
                    f"{depth if depth is not None else 'unknown'}"
                ),
                data={"depth": depth, "area": module.area, "module": module.name},
                weight=depth,
            )
        )
    return tuple(
        sorted(
            points,
            key=lambda point: (
                point.data["depth"] is None,
                -(point.data["depth"] or 0),
                point.ref["project"],
                point.ref["schemaName"],
                point.ref["operationName"] or "",
                point.ref["module"],
                point.ref["operationId"],
            ),
        )
    )
