import ast
import unittest
from collections.abc import Mapping
from pathlib import Path

from graphql import build_schema

from graphql_desk.fragments import SchemaContext, index_fragments
from graphql_desk.scan_graph import build_scan_graph
from graphql_desk.scan_rules import FieldUsagePoint, field_usage_rule
from graphql_desk.source import Span
from graphql_desk.source_graph import (
    ModuleDependency,
    ModuleSnapshot,
    ProjectSources,
    ResolvedCall,
    StringContribution,
)


def _call(
    project: str, module: str, graph: str, schema_name: str, imports: str = ""
) -> tuple[ResolvedCall, ModuleSnapshot]:
    source = f"{imports}doc = graphql({graph!r})\n"
    snapshot = ModuleSnapshot(
        project, module, f"file:///{project}/{module}.py", 1, source
    )
    dependency = ModuleDependency(project, module, snapshot.uri, snapshot.version)
    tree = ast.parse(source)
    literal = next(
        node
        for node in ast.walk(tree)
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
    calls: tuple[ResolvedCall, ...],
    modules: tuple[ModuleSnapshot, ...],
    schemas: Mapping[tuple[str, str | None], SchemaContext],
) -> tuple[FieldUsagePoint, ...]:
    sources = ProjectSources(calls, ())
    index = index_fragments(sources, schemas=schemas)
    paths = {
        (module.project, module.module): Path(
            {"queries": "queries/api.py", "other": "features/other.py"}.get(
                module.module, f"{module.module}.py"
            )
        )
        for module in modules
    }
    graph = build_scan_graph(
        sources,
        index,
        modules,
        paths=paths,
        roots={project: Path.cwd() / "scan-rules-fixture" for project, _ in schemas},
    )
    return field_usage_rule(graph, index, schemas)


class ScanRuleTests(unittest.TestCase):
    def test_field_usage_deduplicates_per_definition_and_expands_fragment_reach(
        self,
    ) -> None:
        project = "shop-project"
        schema = build_schema(
            """
            type Query { user: User! }
            type User {
              id: ID!
              name: String!
              email: String
              oldName: String @deprecated(reason: "Use name")
              unused: String
            }
            """
        )
        context = SchemaContext("shop", schema, "shop-schema")
        schemas: dict[tuple[str, str | None], SchemaContext] = {
            (project, "shop"): context
        }
        queries, queries_module = _call(
            project,
            "queries",
            "query GetUser { user { id name display: name ...UserInfo } } "
            "fragment UserInfo on User { email oldName __typename }",
            "shop",
        )
        other, other_module = _call(
            project, "other", "query GetShort { user { name } }", "shop"
        )
        entry = ModuleSnapshot(
            project,
            "entry",
            f"file:///{project}/entry.py",
            1,
            "import queries\nimport other\n",
        )
        points = _scan((queries, other), (entry, queries_module, other_module), schemas)
        self.assertEqual(
            [(point.ref["typeName"], point.ref["fieldName"]) for point in points],
            [
                ("Query", "user"),
                ("User", "name"),
                ("User", "email"),
                ("User", "id"),
                ("User", "oldName"),
            ],
        )
        by_coordinate = {
            (
                point.ref["schemaName"],
                point.ref["typeName"],
                point.ref["fieldName"],
            ): point
            for point in points
        }

        user = by_coordinate[("shop", "Query", "user")]
        self.assertEqual(user.data["count"], 2)
        self.assertEqual(
            tuple(item["defId"] for item in user.data["directUsages"]),
            (
                "shop-project:shop:query:GetShort",
                "shop-project:shop:query:GetUser",
            ),
        )
        self.assertEqual(
            user.data["operations"],
            (
                "shop-project:shop:query:GetShort",
                "shop-project:shop:query:GetUser",
            ),
        )
        self.assertEqual(
            user.data["reach"],
            {
                "modules": ("entry", "other", "queries"),
                "areas": ("(root)", "features", "queries"),
                "entryPoints": ("entry",),
            },
        )
        self.assertEqual(user.weight, 1)

        name = by_coordinate[("shop", "User", "name")]
        self.assertEqual(name.data["count"], 2)
        self.assertEqual(len(name.data["directUsages"]), 2)
        self.assertEqual(len(name.data["operations"]), 2)

        email = by_coordinate[("shop", "User", "email")]
        self.assertEqual(
            email.data["directUsages"],
            ({"defId": "shop-project:shop:fragment:UserInfo", "module": "queries"},),
        )
        self.assertEqual(email.data["operations"], ("shop-project:shop:query:GetUser",))
        self.assertEqual(email.data["reach"]["modules"], ("entry", "queries"))
        self.assertEqual(email.data["reach"]["areas"], ("(root)", "queries"))

        old_name = by_coordinate[("shop", "User", "oldName")]
        self.assertTrue(old_name.data["deprecated"])
        self.assertEqual(old_name.data.get("deprecationReason"), "Use name")
        self.assertNotIn(("shop", "User", "unused"), by_coordinate)
        self.assertNotIn(("shop", "User", "__typename"), by_coordinate)

    def test_module_count_is_fallback_when_project_has_no_entrypoint(self) -> None:
        project = "cycle-project"
        context = SchemaContext(
            "shop", build_schema("type Query { hello: String }"), "cycle-schema"
        )
        schemas: dict[tuple[str, str | None], SchemaContext] = {
            (project, "shop"): context
        }
        call, module_a = _call(
            project, "a", "query Hello { hello }", "shop", imports="import b\n"
        )
        module_b = ModuleSnapshot(
            project, "b", f"file:///{project}/b.py", 1, "import a\n"
        )
        (point,) = _scan((call,), (module_a, module_b), schemas)
        self.assertEqual(point.data["reach"]["modules"], ("a", "b"))
        self.assertEqual(point.data["reach"]["entryPoints"], ())
        self.assertEqual(point.weight, 2)

    def test_same_coordinate_is_kept_separate_by_schema(self) -> None:
        project = "multi-schema"
        shop = SchemaContext(
            "shop",
            build_schema("type Query { value: String! }"),
            "shop-schema",
        )
        admin = SchemaContext(
            "admin",
            build_schema("type Query { value: Int! }"),
            "admin-schema",
        )
        schemas: dict[tuple[str, str | None], SchemaContext] = {
            (project, "shop"): shop,
            (project, "admin"): admin,
        }
        shop_call, shop_module = _call(project, "shop", "query Shop { value }", "shop")
        admin_call, admin_module = _call(
            project, "admin", "query Admin { value }", "admin"
        )
        points = _scan((shop_call, admin_call), (shop_module, admin_module), schemas)
        values = {point.ref["schemaName"]: point.data["fieldType"] for point in points}
        self.assertEqual(values, {"admin": "Int!", "shop": "String!"})


if __name__ == "__main__":
    unittest.main()
