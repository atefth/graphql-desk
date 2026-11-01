import ast
import unittest
from collections.abc import Mapping
from pathlib import Path

from graphql import build_schema

from graphql_desk.fragments import SchemaContext, index_fragments
from graphql_desk.scan_depth_rules import fetch_depth_rule
from graphql_desk.scan_graph import ScanGraph, build_scan_graph
from graphql_desk.source import Span
from graphql_desk.source_graph import (
    ModuleDependency,
    ModuleSnapshot,
    ProjectSources,
    ResolvedCall,
    StringContribution,
)


def _call(
    project: str, module: str, source: str, graph: str
) -> tuple[ResolvedCall, ModuleSnapshot]:
    snapshot = ModuleSnapshot(
        project, module, f"file:///{project}/{module}.py", 1, source
    )
    dependency = ModuleDependency(project, module, snapshot.uri, snapshot.version)
    literal = next(
        node
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Constant) and node.value == graph
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
        graph,
        (StringContribution(dependency, literal_range, graph),),
        (dependency,),
        schema_name="shop",
    )
    return call, snapshot


def _graph(
    documents: Mapping[str, tuple[str, str]], schema: SchemaContext
) -> ScanGraph:
    project = "fetch-depth"
    schemas: Mapping[tuple[str, str | None], SchemaContext] = {
        (project, "shop"): schema
    }
    calls: list[ResolvedCall] = []
    modules: list[ModuleSnapshot] = []
    paths: dict[tuple[str, str], Path] = {}
    for module, (source_prefix, document) in documents.items():
        call, snapshot = _call(
            project,
            module,
            f"{source_prefix}doc = graphql({document!r})\n",
            document,
        )
        calls.append(call)
        modules.append(snapshot)
        paths[(project, module)] = Path(
            {
                "service": "features/service.py",
                "db": "storage/db.py",
                "cycle_a": "cycles/a.py",
                "cycle_b": "cycles/b.py",
                "isolated": "other/isolated.py",
            }.get(module, f"{module}.py")
        )
    sources = ProjectSources(tuple(calls), ())
    index = index_fragments(sources, schemas=schemas)
    graph = build_scan_graph(
        sources,
        index,
        tuple(modules),
        paths=paths,
        roots={project: Path.cwd() / "scan-depth-fixture"},
    )
    return graph


class FetchDepthRuleTests(unittest.TestCase):
    def test_queries_use_shortest_import_distance_cycles_null_and_rank_null_last(
        self,
    ) -> None:
        schema = SchemaContext(
            "shop",
            build_schema(
                "type Query { value: String } "
                "type Mutation { change: String } "
                "type Subscription { events: String }"
            ),
            "shop-token",
        )
        graph = _graph(
            {
                "app": ("import service\n", "query Root { value }"),
                "service": ("import db\n", "query Deep { value }"),
                "mutations": ("", "mutation Write { change }"),
                "subscriptions": ("", "subscription Events { events }"),
                "db": ("", ""),
                "cycle_a": ("import cycle_b\n", "query ClosedCycle { value }"),
                "cycle_b": ("import cycle_a\n", ""),
                "isolated": ("", "query Zero { value }"),
            },
            schema,
        )

        points = fetch_depth_rule(graph)
        self.assertEqual(
            [(point.ref["operationName"], point.data["depth"]) for point in points],
            [("Deep", 1), ("Root", 0), ("Zero", 0), ("ClosedCycle", None)],
        )
        self.assertEqual(
            [(point.data["area"], point.data["module"]) for point in points],
            [
                ("features", "service"),
                ("(root)", "app"),
                ("other", "isolated"),
                ("cycles", "cycle_a"),
            ],
        )
        self.assertEqual([point.weight for point in points], [1, 0, 0, None])
        self.assertEqual({point.ref["operationId"] for point in points}.__len__(), 4)


if __name__ == "__main__":
    unittest.main()
