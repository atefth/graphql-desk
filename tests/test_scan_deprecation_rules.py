import ast
import unittest
from collections.abc import Mapping
from pathlib import Path

from graphql import build_schema

from graphql_desk.fragments import SchemaContext, index_fragments
from graphql_desk.scan_deprecation_rules import (
    DeprecatedUsagePoint,
    deprecated_usage_rule,
)
from graphql_desk.scan_graph import build_scan_graph
from graphql_desk.source import Span
from graphql_desk.source_graph import (
    ModuleDependency,
    ModuleSnapshot,
    ProjectSources,
    ResolvedCall,
    StringContribution,
)


def _call(
    project: str, module: str, document: str, schema_name: str
) -> tuple[ResolvedCall, ModuleSnapshot]:
    source = f"doc = graphql({document!r})\n"
    snapshot = ModuleSnapshot(
        project, module, f"file:///{project}/{module}.py", 1, source
    )
    dependency = ModuleDependency(project, module, snapshot.uri, snapshot.version)
    tree = ast.parse(source)
    literal = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and node.value == document
    )
    starts = [0]
    for line in source.splitlines(keepends=True):
        starts.append(starts[-1] + len(line))
    assert literal.end_lineno is not None
    assert literal.end_col_offset is not None
    literal_range = Span(
        starts[literal.lineno - 1] + literal.col_offset,
        starts[literal.end_lineno - 1] + literal.end_col_offset,
    )
    call = ResolvedCall(
        dependency,
        Span(0, len(source)),
        document,
        (StringContribution(dependency, literal_range, document),),
        (dependency,),
        schema_name=schema_name,
    )
    return call, snapshot


def _deprecated_points(
    calls: tuple[ResolvedCall, ...],
    modules: tuple[ModuleSnapshot, ...],
    schemas: Mapping[tuple[str, str | None], SchemaContext],
) -> tuple[DeprecatedUsagePoint, ...]:
    sources = ProjectSources(calls, ())
    index = index_fragments(sources, schemas=schemas)
    paths = {
        (module.project, module.module): Path(f"{module.module}.py")
        for module in modules
    }
    graph = build_scan_graph(
        sources,
        index,
        modules,
        paths=paths,
        roots={project: Path.cwd() for project, _ in schemas},
    )
    return deprecated_usage_rule(graph, index, schemas)


class DeprecatedUsageRuleTests(unittest.TestCase):
    def test_expands_fragments_deduplicates_operations_and_keeps_orphans(self) -> None:
        project = "shop"
        context = SchemaContext(
            "shop",
            build_schema(
                """
                type Query {
                  old: String @deprecated(reason: "Use current")
                  current: String
                  orphaned: String @deprecated(reason: "Never reached")
                }
                """
            ),
            "shop-schema",
        )
        schemas: dict[tuple[str, str | None], SchemaContext] = {
            (project, "shop"): context
        }
        first, first_module = _call(
            project,
            "first",
            "query First { old alias: old ...Shared } fragment Shared on Query { old }",
            "shop",
        )
        second, second_module = _call(
            project,
            "second",
            "query Second { ...Shared } fragment Shared on Query { old }",
            "shop",
        )
        orphan, orphan_module = _call(
            project,
            "orphan",
            "fragment Orphan on Query { orphaned }",
            "shop",
        )
        points = _deprecated_points(
            (first, second, orphan),
            (first_module, second_module, orphan_module),
            schemas,
        )

        self.assertEqual(
            [
                (
                    point.ref["schemaName"],
                    point.ref["typeName"],
                    point.ref["fieldName"],
                    point.data,
                    point.weight,
                )
                for point in points
            ],
            [
                (
                    "shop",
                    "Query",
                    "old",
                    {
                        "count": 2,
                        "operations": (
                            "shop:shop:query:First",
                            "shop:shop:query:Second",
                        ),
                        "reason": "Use current",
                    },
                    2,
                ),
                (
                    "shop",
                    "Query",
                    "orphaned",
                    {
                        "count": 0,
                        "operations": (),
                        "reason": "Never reached",
                    },
                    0,
                ),
            ],
        )

    def test_same_coordinate_stays_schema_qualified_and_rank_ties_are_stable(
        self,
    ) -> None:
        project = "multi-schema"
        first = SchemaContext(
            "first",
            build_schema('type Query { value: String @deprecated(reason: "first") }'),
            "first-schema",
        )
        second = SchemaContext(
            "second",
            build_schema('type Query { value: Int @deprecated(reason: "second") }'),
            "second-schema",
        )
        schemas: dict[tuple[str, str | None], SchemaContext] = {
            (project, "first"): first,
            (project, "second"): second,
        }
        first_call, first_module = _call(
            project, "first", "query First { value }", "first"
        )
        second_call, second_module = _call(
            project, "second", "query Second { value }", "second"
        )
        points = _deprecated_points(
            (second_call, first_call), (second_module, first_module), schemas
        )

        self.assertEqual(
            [point.ref["schemaName"] for point in points], ["first", "second"]
        )
        self.assertEqual(
            [point.data.get("reason") for point in points], ["first", "second"]
        )
        self.assertEqual([point.data["count"] for point in points], [1, 1])


if __name__ == "__main__":
    unittest.main()
