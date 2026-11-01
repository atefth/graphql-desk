import ast
import unittest
from collections.abc import Mapping
from pathlib import Path

from graphql import build_schema

from graphql_desk.fragments import SchemaContext, index_fragments
from graphql_desk.scan_graph import build_scan_graph
from graphql_desk.scan_input_rules import InputUsagePoint, input_usage_rule
from graphql_desk.source import Span
from graphql_desk.source_graph import (
    ModuleDependency,
    ModuleSnapshot,
    ProjectSources,
    ResolvedCall,
    StringContribution,
)


def _call(
    project: str, module: str, graph: str, schema_name: str
) -> tuple[ResolvedCall, ModuleSnapshot]:
    source = f"doc = graphql({graph!r})\n"
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
        schema_name=schema_name,
    )
    return call, snapshot


def _scan(
    graph_text: str,
    schemas: Mapping[tuple[str, str | None], SchemaContext],
) -> tuple[InputUsagePoint, ...]:
    project, schema_name = next(iter(schemas))
    call, module = _call(project, "queries", graph_text, schema_name or "")
    sources = ProjectSources((call,), ())
    index = index_fragments(sources, schemas=schemas)
    graph = build_scan_graph(
        sources,
        index,
        (module,),
        paths={(project, "queries"): Path("queries.py")},
        roots={project: Path.cwd() / "scan-input-rules-fixture"},
    )
    return input_usage_rule(graph, index, schemas)


class ScanInputRuleTests(unittest.TestCase):
    def test_nested_input_fields_enum_literals_and_defaults_are_counted(self) -> None:
        project = "input-project"
        context = SchemaContext(
            "shop",
            build_schema(
                """
                enum Status { ACTIVE USER ADMIN }
                enum Region { EAST WEST }
                input AddressInput { region: Region! }
                input SearchInput {
                  status: Status
                  address: AddressInput
                  tags: [Status!]
                }
                directive @access(level: Status!) on QUERY | FIELD
                type Query {
                  search(input: SearchInput, statuses: [Status!]): String!
                }
                """
            ),
            "shop-schema",
        )
        schemas: dict[tuple[str, str | None], SchemaContext] = {
            (project, "shop"): context
        }
        points = _scan(
            """
            query Search(
              $default: SearchInput = {status: ACTIVE, address: {region: WEST}}
              $runtime: Status
            ) @access(level: ADMIN) {
              search(
                input: {
                  status: $runtime
                  address: {region: EAST}
                  tags: [ACTIVE, $runtime]
                }
                statuses: ACTIVE
              ) @access(level: USER)
            }
            """,
            schemas,
        )
        by_coordinate = {
            (point.ref["typeName"], point.ref["fieldName"]): point for point in points
        }
        expected = {
            ("SearchInput", "status"): ("input-field", 2),
            ("SearchInput", "address"): ("input-field", 2),
            ("SearchInput", "tags"): ("input-field", 1),
            ("AddressInput", "region"): ("input-field", 2),
            ("Status", "ACTIVE"): ("enum-value", 3),
            ("Status", "ADMIN"): ("enum-value", 1),
            ("Status", "USER"): ("enum-value", 1),
            ("Region", "EAST"): ("enum-value", 1),
            ("Region", "WEST"): ("enum-value", 1),
        }
        self.assertEqual(
            {
                coordinate: (point.data["kind"], point.data["count"])
                for coordinate, point in by_coordinate.items()
            },
            expected,
        )
        self.assertEqual(
            [(point.ref["typeName"], point.ref["fieldName"]) for point in points],
            [
                ("Status", "ACTIVE"),
                ("AddressInput", "region"),
                ("SearchInput", "address"),
                ("SearchInput", "status"),
                ("Region", "EAST"),
                ("Region", "WEST"),
                ("SearchInput", "tags"),
                ("Status", "ADMIN"),
                ("Status", "USER"),
            ],
        )
        self.assertTrue(all(point.weight == point.data["count"] for point in points))

    def test_runtime_variables_scalars_and_unknown_schema_fields_are_ignored(
        self,
    ) -> None:
        project = "input-project"
        context = SchemaContext(
            "shop",
            build_schema(
                """
                enum Status { ACTIVE }
                input Filter { status: Status, name: String }
                type Query { search(filter: Filter, term: String): String }
                """
            ),
            "shop-schema",
        )
        schemas: dict[tuple[str, str | None], SchemaContext] = {
            (project, "shop"): context
        }
        points = _scan(
            """
            query Search($runtime: Filter, $unused: Status) {
              search(filter: $runtime, term: \"text\")
              search(
                filter: {status: ACTIVE, unknown: ACTIVE}
                term: \"text\"
              )
            }
            """,
            schemas,
        )
        self.assertEqual(
            [
                (point.ref["typeName"], point.ref["fieldName"], point.data["count"])
                for point in points
            ],
            [("Filter", "status", 1), ("Status", "ACTIVE", 1)],
        )

    def test_coordinates_are_schema_scoped_and_ties_sort_lexically(self) -> None:
        project = "input-project"
        first = SchemaContext(
            "shop",
            build_schema(
                "enum Status { ACTIVE } input Filter { status: Status } "
                "type Query { search(filter: Filter): String }"
            ),
            "shop-schema",
        )
        second = SchemaContext(
            "admin",
            build_schema(
                "enum Status { ACTIVE } input Filter { status: Status } "
                "type Query { search(filter: Filter): String }"
            ),
            "admin-schema",
        )
        first_call, first_module = _call(
            project, "shop", "query Q { search(filter: {status: ACTIVE}) }", "shop"
        )
        second_call, second_module = _call(
            project, "admin", "query Q { search(filter: {status: ACTIVE}) }", "admin"
        )
        sources = ProjectSources((first_call, second_call), ())
        schemas: dict[tuple[str, str | None], SchemaContext] = {
            (project, "shop"): first,
            (project, "admin"): second,
        }
        index = index_fragments(sources, schemas=schemas)
        graph = build_scan_graph(
            sources,
            index,
            (first_module, second_module),
            paths={
                (project, "shop"): Path("shop.py"),
                (project, "admin"): Path("admin.py"),
            },
            roots={project: Path.cwd() / "scan-input-rules-multischema-fixture"},
        )
        points = input_usage_rule(graph, index, schemas)
        self.assertEqual(
            [
                (
                    point.ref["schemaName"],
                    point.ref["typeName"],
                    point.ref["fieldName"],
                )
                for point in points
            ],
            [
                ("admin", "Filter", "status"),
                ("admin", "Status", "ACTIVE"),
                ("shop", "Filter", "status"),
                ("shop", "Status", "ACTIVE"),
            ],
        )


if __name__ == "__main__":
    unittest.main()
