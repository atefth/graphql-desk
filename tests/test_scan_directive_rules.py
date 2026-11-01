import ast
import unittest
from collections.abc import Mapping
from pathlib import Path

from graphql import build_schema

from graphql_desk.fragments import FragmentIndex, SchemaContext, index_fragments
from graphql_desk.scan_directive_rules import (
    DirectiveUsagePoint,
    directive_usage_rule,
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
        schema_name=schema_name,
    )
    return call, snapshot


def _points(
    calls: tuple[ResolvedCall, ...],
    modules: tuple[ModuleSnapshot, ...],
    schemas: Mapping[tuple[str, str | None], SchemaContext],
) -> tuple[DirectiveUsagePoint, ...]:
    sources = ProjectSources(calls, ())
    index: FragmentIndex = index_fragments(sources, schemas=schemas)
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
    return directive_usage_rule(graph, index)


class DirectiveUsageRuleTests(unittest.TestCase):
    def test_counts_all_directives_from_original_operation_and_fragment_ast(
        self,
    ) -> None:
        project = "shop"
        context = SchemaContext(
            "shop",
            build_schema(
                """
                directive @_unmask on FRAGMENT_DEFINITION
                directive @defer(if: Boolean = true)
                  on FRAGMENT_SPREAD | INLINE_FRAGMENT
                directive @custom on
                  QUERY | VARIABLE_DEFINITION | FIELD
                  | FRAGMENT_SPREAD | INLINE_FRAGMENT | FRAGMENT_DEFINITION
                type Query { value: String }
                """
            ),
            "shop-schema",
        )
        schemas: dict[tuple[str, str | None], SchemaContext] = {
            (project, "shop"): context
        }
        call, module = _call(
            project,
            "queries",
            """
            query Q($should: Boolean! @custom) @custom {
              value @include(if: $should) @skip(if: false) @custom
              ...Shared @defer(if: $should) @custom
              ... on Query @defer(if: $should) @custom { value }
            }
            fragment Shared on Query @_unmask { value @custom }
            """,
            "shop",
        )

        points = _points((call,), (module,), schemas)

        self.assertEqual(
            [
                (point.ref["schemaName"], point.ref["directiveName"], point.data)
                for point in points
            ],
            [
                ("shop", "custom", {"count": 6}),
                ("shop", "defer", {"count": 2}),
                ("shop", "_unmask", {"count": 1}),
                ("shop", "include", {"count": 1}),
                ("shop", "skip", {"count": 1}),
            ],
        )
        self.assertEqual([point.weight for point in points], [6, 2, 1, 1, 1])

    def test_equal_counts_sort_by_name_then_schema(self) -> None:
        project = "multi-schema"
        first = SchemaContext(
            "first",
            build_schema(
                "directive @one on QUERY | FIELD "
                "directive @two on QUERY type Query { value: String }"
            ),
            "first-schema",
        )
        second = SchemaContext(
            "second",
            build_schema(
                "directive @one on QUERY | FIELD "
                "directive @two on QUERY type Query { value: String }"
            ),
            "second-schema",
        )
        schemas: dict[tuple[str, str | None], SchemaContext] = {
            (project, "first"): first,
            (project, "second"): second,
        }
        first_call, first_module = _call(
            project, "first", "query Q @one { value @one @two }", "first"
        )
        second_call, second_module = _call(
            project, "second", "query Q @two { value @one }", "second"
        )

        points = _points(
            (second_call, first_call), (second_module, first_module), schemas
        )

        self.assertEqual(
            [
                (
                    point.ref["directiveName"],
                    point.ref["schemaName"],
                    point.data["count"],
                )
                for point in points
            ],
            [
                ("one", "first", 2),
                ("one", "second", 1),
                ("two", "first", 1),
                ("two", "second", 1),
            ],
        )


if __name__ == "__main__":
    unittest.main()
