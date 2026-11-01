import unittest

from graphql_desk.scan_fragment_rules import (
    cross_feature_fragment_rule,
    orphan_fragment_rule,
)
from graphql_desk.scan_graph import (
    ScanDefinition,
    ScanFragmentEdge,
    ScanGraph,
    ScanLocation,
    ScanModule,
)
from graphql_desk.source import Span
from graphql_desk.source_graph import GraphDiagnostic, ModuleDependency


def _definition(
    identity: str,
    name: str,
    module: str,
    *,
    project: str = "app",
    schema: str = "shop",
    kind: str = "fragment",
    spreads: tuple[str, ...] = (),
    type_condition: str | None = "User",
) -> ScanDefinition:
    return ScanDefinition(
        id=identity,
        kind=kind,
        name=name,
        project=project,
        schema_name=schema,
        module=module,
        locations=(ScanLocation(project, module, f"file:///{module}.py", 1, 1),),
        variables=(),
        fragment_spreads=spreads,
        hash=identity,
        source_key=(project, module, 0, 1),
        definition_range=Span(0, 1),
        type_condition=type_condition,
    )


def _module(name: str, area: str, *, project: str = "app") -> ScanModule:
    return ScanModule(
        project=project,
        name=name,
        uri=f"file:///{name}.py",
        path=f"{name}.py",
        area=area,
        entrypoint=False,
        distance=None,
        imports=(),
        imported_by=(),
        reverse_reachable=(name,),
    )


class ScanFragmentRuleTests(unittest.TestCase):
    def test_orphans_use_resolved_incoming_spreads_and_keep_cycles_non_orphan(
        self,
    ) -> None:
        definitions = (
            _definition("query", "Get", "queries", kind="query", spreads=("Used",)),
            _definition("used", "Used", "fragments"),
            _definition("parent", "OrphanParent", "fragments", spreads=("Child",)),
            _definition("child", "Child", "fragments"),
            _definition("cycle-a", "CycleA", "cycle", spreads=("CycleB",)),
            _definition("cycle-b", "CycleB", "cycle", spreads=("CycleA",)),
            _definition("shop-same", "Same", "shop-only"),
            _definition("admin-same", "Same", "admin-only", schema="admin"),
        )
        diagnostic = GraphDiagnostic(
            ModuleDependency("app", "cycle", "file:///cycle.py", 1),
            "fragment-reference-cycle",
            "Explicit fragment references form a cycle",
            Span(0, 1),
        )
        graph = ScanGraph(
            definitions,
            (
                _module("queries", "(root)"),
                _module("fragments", "features"),
                _module("cycle", "features/cycle"),
                _module("shop-only", "shop"),
                _module("admin-only", "admin", project="app"),
            ),
            (
                ScanFragmentEdge("query", "used"),
                ScanFragmentEdge("parent", "child"),
                ScanFragmentEdge("cycle-a", "cycle-b"),
                ScanFragmentEdge("cycle-b", "cycle-a"),
            ),
            (diagnostic,),
        )

        points = orphan_fragment_rule(graph)

        self.assertEqual(
            [
                (
                    point.ref["schemaName"],
                    point.ref["name"],
                    point.data,
                    hasattr(point, "weight"),
                )
                for point in points
            ],
            [
                (
                    "admin",
                    "Same",
                    {"typeCondition": "User", "module": "admin-only"},
                    False,
                ),
                (
                    "shop",
                    "OrphanParent",
                    {"typeCondition": "User", "module": "fragments"},
                    False,
                ),
                (
                    "shop",
                    "Same",
                    {"typeCondition": "User", "module": "shop-only"},
                    False,
                ),
            ],
        )
        self.assertIn(diagnostic, graph.diagnostics)

    def test_cross_feature_counts_spreads_and_ranks_distinct_external_areas(
        self,
    ) -> None:
        definitions = (
            _definition("tile", "Tile", "features.tiles"),
            _definition("private", "Private", "features.private"),
            _definition(
                "same-area-use", "SameAreaUse", "features.tiles", spreads=("Tile",)
            ),
            _definition(
                "external-use",
                "ExternalUse",
                "queries.main",
                kind="query",
                spreads=("Tile", "Tile", "Private", "Missing"),
            ),
            _definition(
                "other-area-use", "OtherAreaUse", "features.other", spreads=("Tile",)
            ),
            _definition("other", "Other", "features.other"),
            _definition(
                "other-external-use",
                "OtherExternalUse",
                "features.tiles.consumer",
                spreads=("Other",),
            ),
        )
        graph = ScanGraph(
            definitions,
            (
                _module("features.tiles", "features/tiles"),
                _module("features.private", "features/private"),
                _module("features.tiles.consumer", "features/tiles/consumer"),
                _module("features.other", "features/other"),
                _module("queries.main", "(root)"),
            ),
            (
                ScanFragmentEdge("same-area-use", "tile"),
                ScanFragmentEdge("external-use", "tile"),
                ScanFragmentEdge("other-area-use", "tile"),
                ScanFragmentEdge("other-external-use", "other"),
            ),
            (),
        )

        points = cross_feature_fragment_rule(graph)

        self.assertEqual(
            [(point.ref["name"], point.data, point.weight) for point in points],
            [
                (
                    "Tile",
                    {
                        "definingArea": "features/tiles",
                        "consumerAreas": ("(root)", "features/other", "features/tiles"),
                        "spreadCount": 4,
                    },
                    2,
                ),
                (
                    "Other",
                    {
                        "definingArea": "features/other",
                        "consumerAreas": ("features/tiles/consumer",),
                        "spreadCount": 1,
                    },
                    1,
                ),
            ],
        )


if __name__ == "__main__":
    unittest.main()
