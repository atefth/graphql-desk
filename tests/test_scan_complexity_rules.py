import ast
import unittest
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path

from graphql import build_schema

from graphql_desk.fragments import FragmentIndex, SchemaContext, index_fragments
from graphql_desk.scan_complexity_rules import operation_complexity_rule
from graphql_desk.scan_graph import ScanFragmentEdge, ScanGraph, build_scan_graph
from graphql_desk.source import Span
from graphql_desk.source_graph import (
    ModuleDependency,
    ModuleSnapshot,
    ProjectSources,
    ResolvedCall,
    StringContribution,
)


def _scan(
    source: str, document: str
) -> tuple[
    ScanGraph,
    FragmentIndex,
    Mapping[tuple[str, str | None], SchemaContext],
]:
    project = "complexity"
    module = "app"
    snapshot = ModuleSnapshot(project, module, "file:///complexity/app.py", 1, source)
    dependency = ModuleDependency(project, module, snapshot.uri, snapshot.version)
    literal = next(
        node
        for node in ast.walk(ast.parse(source))
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
        schema_name="shop",
    )
    sources = ProjectSources((call,), ())
    schema = SchemaContext(
        "shop",
        build_schema(
            "type Query { users: [User!]!, user: User, profile: Profile } "
            "type User { id: ID!, profile: Profile } "
            "type Profile { name: String }"
        ),
        "shop-token",
    )
    schemas: Mapping[tuple[str, str | None], SchemaContext] = {
        (project, "shop"): schema
    }
    index = index_fragments(sources, schemas=schemas)
    graph = build_scan_graph(
        sources,
        index,
        (snapshot,),
        paths={(project, module): Path("app.py")},
        roots={project: Path.cwd() / "scan-complexity-fixture"},
    )
    return graph, index, schemas


class OperationComplexityRuleTests(unittest.TestCase):
    def test_distinct_coordinates_fragments_aliases_lists_and_static_score(
        self,
    ) -> None:
        document = (
            "query List { users { id idAlias: id profile { name } } ...Shared } "
            "fragment Shared on Query { user { id } }"
        )
        graph, index, schemas = _scan(f"doc = {document!r}\n", document)

        (point,) = operation_complexity_rule(graph, index, schemas)

        self.assertEqual(
            point.data["fields"],
            (
                "Profile.name",
                "Query.user",
                "Query.users",
                "User.id",
                "User.profile",
            ),
        )
        self.assertEqual(point.data["fieldCount"], 5)
        self.assertEqual(point.data["depth"], 3)
        self.assertEqual(point.data["listFields"], 1)
        self.assertEqual(point.data["score"], 17)
        self.assertEqual(point.weight, 17)
        self.assertIn("not a server execution-cost estimate", point.message)

    def test_fragment_cycles_terminate_and_do_not_inflate_depth(self) -> None:
        document = (
            "query Cycle { ...A } "
            "fragment A on Query { user { id } ...B } "
            "fragment B on Query { profile { name } #      \n}"
        )
        graph, index, schemas = _scan(f"doc = {document!r}\n", document)
        definitions = {definition.name: definition for definition in graph.definitions}
        cyclic_document = document.replace("#      ", "...A   ")
        indexed_document = index.documents[0]
        cyclic_call = replace(indexed_document.call, source=cyclic_document)
        index = replace(
            index,
            documents=(replace(indexed_document, call=cyclic_call),),
        )
        graph = replace(
            graph,
            fragment_edges=(
                *graph.fragment_edges,
                ScanFragmentEdge(definitions["A"].id, definitions["B"].id),
                ScanFragmentEdge(definitions["B"].id, definitions["A"].id),
            ),
        )

        (point,) = operation_complexity_rule(graph, index, schemas)

        self.assertEqual(
            point.data["fields"],
            (
                "Profile.name",
                "Query.profile",
                "Query.user",
                "User.id",
            ),
        )
        self.assertEqual(point.data["fieldCount"], 4)
        self.assertEqual(point.data["depth"], 2)
        self.assertEqual(point.data["listFields"], 0)
        self.assertEqual(point.data["score"], 8)


if __name__ == "__main__":
    unittest.main()
