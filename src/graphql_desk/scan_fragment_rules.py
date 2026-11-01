"""Fragment reach insights for the experimental scan command."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

from typing_extensions import TypedDict

from .scan_graph import ScanDefinition, ScanGraph, ScanModule


class FragmentReference(TypedDict):
    project: str
    schemaName: str
    fragmentId: str
    name: str


class OrphanFragmentData(TypedDict):
    typeCondition: str
    module: str


class CrossFeatureFragmentData(TypedDict):
    definingArea: str
    consumerAreas: tuple[str, ...]
    spreadCount: int


@dataclass(frozen=True)
class OrphanFragmentPoint:
    ref: FragmentReference
    message: str
    data: OrphanFragmentData


@dataclass(frozen=True)
class CrossFeatureFragmentPoint:
    ref: FragmentReference
    message: str
    data: CrossFeatureFragmentData
    weight: int


def _fragment_reference(definition: ScanDefinition) -> FragmentReference:
    if definition.kind != "fragment" or definition.name is None:
        raise ValueError("Fragment rules require named fragment definitions")
    return {
        "project": definition.project,
        "schemaName": definition.schema_name,
        "fragmentId": definition.id,
        "name": definition.name,
    }


def _resolved_spread_sources(
    graph: ScanGraph,
) -> tuple[
    dict[str, ScanDefinition],
    dict[tuple[str, str], ScanModule],
    dict[str, set[str]],
]:
    definitions = {item.id: item for item in graph.definitions}
    if len(definitions) != len(graph.definitions):
        raise ValueError("Scan definitions must have unique identities")
    modules = {(item.project, item.name): item for item in graph.modules}
    if len(modules) != len(graph.modules):
        raise ValueError("Scan modules must have unique project/module identities")
    incoming: dict[str, set[str]] = defaultdict(set)
    for edge in graph.fragment_edges:
        source = definitions.get(edge.source_id)
        target = definitions.get(edge.target_id)
        if (
            source is None
            or target is None
            or target.kind != "fragment"
            or target.name is None
            or (source.project, source.schema_name)
            != (target.project, target.schema_name)
        ):
            raise ValueError("Fragment edges must resolve within their scan graph")
        if target.name not in source.fragment_spreads:
            raise ValueError("Fragment edges must match a direct spread")
        if any((item.project, item.module) not in modules for item in (source, target)):
            raise ValueError("Fragment definitions must refer to scan modules")
        incoming[target.id].add(source.id)
    return definitions, modules, incoming


def orphan_fragment_rule(graph: ScanGraph) -> tuple[OrphanFragmentPoint, ...]:
    """Report fragments that are never a resolved direct-spread target."""
    _, _, incoming = _resolved_spread_sources(graph)
    points: list[OrphanFragmentPoint] = []
    for fragment in graph.definitions:
        if fragment.kind != "fragment" or fragment.id in incoming:
            continue
        if fragment.type_condition is None:
            raise ValueError("Fragments must have a type condition")
        points.append(
            OrphanFragmentPoint(
                ref=_fragment_reference(fragment),
                message=f"Fragment {fragment.name} is never a resolved spread target",
                data={
                    "typeCondition": fragment.type_condition,
                    "module": fragment.module,
                },
            )
        )
    return tuple(
        sorted(
            points,
            key=lambda point: (
                point.ref["project"],
                point.ref["schemaName"],
                point.ref["name"],
                point.ref["fragmentId"],
            ),
        )
    )


def cross_feature_fragment_rule(
    graph: ScanGraph,
) -> tuple[CrossFeatureFragmentPoint, ...]:
    """Rank fragments spread from directories outside their defining area."""
    definitions, modules, _ = _resolved_spread_sources(graph)
    areas: dict[str, set[str]] = defaultdict(set)
    spread_counts: dict[str, int] = defaultdict(int)
    for edge in graph.fragment_edges:
        source = definitions[edge.source_id]
        target = definitions[edge.target_id]
        source_module = modules[(source.project, source.module)]
        occurrences = sum(
            spread_name == target.name for spread_name in source.fragment_spreads
        )
        areas[target.id].add(source_module.area)
        spread_counts[target.id] += occurrences

    points: list[CrossFeatureFragmentPoint] = []
    for fragment in graph.definitions:
        if fragment.kind != "fragment" or fragment.id not in areas:
            continue
        fragment_area = modules[(fragment.project, fragment.module)].area
        consumer_areas = tuple(sorted(areas[fragment.id]))
        external_areas = len({area for area in consumer_areas if area != fragment_area})
        if external_areas == 0:
            continue
        points.append(
            CrossFeatureFragmentPoint(
                ref=_fragment_reference(fragment),
                message=(
                    f"Fragment {fragment.name} is spread from "
                    f"{external_areas} external areas"
                ),
                data={
                    "definingArea": fragment_area,
                    "consumerAreas": consumer_areas,
                    "spreadCount": spread_counts[fragment.id],
                },
                weight=external_areas,
            )
        )
    return tuple(
        sorted(
            points,
            key=lambda point: (
                -point.weight,
                point.ref["project"],
                point.ref["schemaName"],
                point.ref["name"],
                point.ref["fragmentId"],
            ),
        )
    )
