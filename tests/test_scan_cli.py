"""Experimental scan report and CLI acceptance for one configured project."""

import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from graphql_desk.cli import main
from graphql_desk.scan import scan_project
from graphql_desk.scan_reporting import json_report
from graphql_desk.source_graph import (
    ModuleSnapshot,
    ProjectBindings,
    ProjectSources,
)
from graphql_desk.source_graph import resolve_project as resolve_project_impl


class _Terminal(io.StringIO):
    def isatty(self) -> bool:
        return True


class ScanCLITests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        (self.root / "schema.graphql").write_text(
            "type Query { users: [User!]! } type User { name: String! id: ID! }",
            encoding="utf-8",
        )
        (self.root / "pyproject.toml").write_text(
            '[tool.graphql-desk]\nschema="schema.graphql"\n'
            'schema_output="graphql_desk_generated/default.pyi"\n',
            encoding="utf-8",
        )
        self.source = self.root / "app.py"
        self.source.write_text(
            "from graphql_desk_generated.default import graphql\n"
            'document = graphql("""query Users { users { ...UserName } }\n'
            'fragment UserName on User { name }""")\n',
            encoding="utf-8",
        )

    def invoke(
        self, argv: list[str], *, terminal: bool = False
    ) -> tuple[int, str, str]:
        stdout = _Terminal() if terminal else io.StringIO()
        stderr = io.StringIO()
        with patch.object(Path, "cwd", return_value=self.root):
            with redirect_stdout(stdout), redirect_stderr(stderr):
                code = main(argv)
        return code, stdout.getvalue(), stderr.getvalue()

    def test_callable_json_and_cli_are_equivalent(self) -> None:
        source_before = self.source.read_bytes()
        with patch.object(Path, "cwd", return_value=self.root):
            report = scan_project()
        code, output, error = self.invoke(["scan", "--format", "json"])
        self.assertEqual((code, error), (0, ""))
        self.assertEqual(output, json_report(report))
        payload = json.loads(output)
        self.assertEqual(payload["version"], 1)
        self.assertEqual(
            payload["overview"]["coverage"],
            {
                "usedFields": 2,
                "totalFields": 3,
                "percent": 67,
            },
        )
        self.assertEqual(payload["operations"][0]["name"], "Users")
        self.assertEqual(payload["fragments"][0]["name"], "UserName")
        self.assertEqual(
            payload["rules"]["operation-complexity"][0]["data"]["score"], 9
        )
        self.assertEqual(self.source.read_bytes(), source_before)

    def test_graph_is_pure_and_contains_schema_and_fragment_edges(self) -> None:
        code, output, error = self.invoke(["scan", "--format", "json", "--graph"])
        self.assertEqual((code, error), (0, ""))
        graph = json.loads(output)
        self.assertEqual(set(graph), {"nodes", "edges"})
        self.assertTrue(any(node["kind"] == "schemaField" for node in graph["nodes"]))
        self.assertTrue(any(edge["kind"] == "spreads" for edge in graph["edges"]))
        self.assertTrue(any(edge["kind"] == "onType" for edge in graph["edges"]))
        self.assertEqual(
            graph,
            json.loads(self.invoke(["scan", "--graph"])[1]),
        )

    def test_project_scan_resolves_imported_fragment_through_the_full_pipeline(
        self,
    ) -> None:
        users = self.root / "features" / "users"
        shared = self.root / "features" / "shared"
        users.mkdir(parents=True)
        shared.mkdir(parents=True)
        (self.root / "features" / "__init__.py").write_text("", encoding="utf-8")
        (users / "__init__.py").write_text("", encoding="utf-8")
        (shared / "__init__.py").write_text("", encoding="utf-8")
        fragment_source = shared / "fragments.py"
        fragment_source.write_text(
            "from graphql_desk_generated.default import graphql\n"
            'card = graphql("fragment UserName on User { name }")\n',
            encoding="utf-8",
        )
        self.source.write_text("", encoding="utf-8")
        (users / "queries.py").write_text(
            "from graphql_desk_generated.default import graphql\n"
            "from features.shared.fragments import card\n"
            'document = graphql("query Users { users { ...UserName } }", [card])\n',
            encoding="utf-8",
        )

        with patch.object(Path, "cwd", return_value=self.root):
            report = scan_project()

        operation = report.operations[0]
        fragment = report.fragments[0]
        self.assertEqual(operation["module"], "features.users.queries")
        self.assertEqual(operation["loc"]["file"], "features/users/queries.py")
        self.assertEqual(fragment["module"], "features.shared.fragments")
        self.assertEqual(fragment["loc"]["file"], "features/shared/fragments.py")
        edges = {
            (item["from"], item["to"], item["kind"]) for item in report.graph["edges"]
        }
        self.assertIn(
            (
                "module:features.users.queries",
                "module:features.shared.fragments",
                "imports",
            ),
            edges,
        )
        self.assertIn(
            (f"operation:{operation['id']}", f"fragment:{fragment['id']}", "spreads"),
            edges,
        )
        cross_feature = report.rules["cross-feature-fragments"]
        self.assertEqual(len(cross_feature), 1)
        self.assertEqual(
            cross_feature[0]["data"],
            {
                "definingArea": "features/shared",
                "consumerAreas": ("features/users",),
                "spreadCount": 1,
            },
        )

    def test_repeated_configured_document_uses_keep_composition_edges_stable(
        self,
    ) -> None:
        shared = self.root / "features" / "shared"
        users = self.root / "features" / "users"
        shared.mkdir(parents=True)
        users.mkdir(parents=True)
        (self.root / "features" / "__init__.py").write_text("", encoding="utf-8")
        (shared / "__init__.py").write_text("", encoding="utf-8")
        (users / "__init__.py").write_text("", encoding="utf-8")
        (shared / "documents.py").write_text(
            "from graphql_desk_generated.default import graphql\n"
            'QUERY = "query Users { users { ...UserFields } }"\n'
            'by_id = graphql("fragment UserFields on User { id }")\n'
            'by_name = graphql("fragment UserFields on User { name }")\n',
            encoding="utf-8",
        )
        self.source.write_text("", encoding="utf-8")
        (users / "queries.py").write_text(
            "from graphql_desk_generated.default import graphql\n"
            "from features.shared.documents import QUERY, by_id, by_name\n"
            "first = graphql(QUERY, fragments=[by_id])\n"
            "second = graphql(QUERY, fragments=[by_name])\n",
            encoding="utf-8",
        )

        with patch.object(Path, "cwd", return_value=self.root):
            report = scan_project()

        def resolve_reversed(
            modules: tuple[ModuleSnapshot, ...],
            *,
            bindings: tuple[ProjectBindings, ...] = (),
            collect_document_imports: bool = False,
        ) -> ProjectSources:
            return resolve_project_impl(
                tuple(reversed(modules)),
                bindings=bindings,
                collect_document_imports=collect_document_imports,
            )

        with (
            patch.object(Path, "cwd", return_value=self.root),
            patch(
                "graphql_desk.scan.resolve_project",
                side_effect=resolve_reversed,
            ),
        ):
            reordered = scan_project()

        self.assertEqual(report.operations, reordered.operations)
        self.assertEqual(report.fragments, reordered.fragments)
        self.assertEqual(report.graph, reordered.graph)
        operation_ids = [item["id"] for item in report.operations]
        fragment_ids = [item["id"] for item in report.fragments]
        self.assertEqual(
            operation_ids,
            ["project:default:query:Users", "project:default:query:Users~2"],
        )
        self.assertEqual(
            fragment_ids,
            [
                "project:default:fragment:UserFields",
                "project:default:fragment:UserFields~2",
            ],
        )
        spread_edges = {
            (item["from"], item["to"])
            for item in report.graph["edges"]
            if item["kind"] == "spreads"
        }
        self.assertEqual(
            spread_edges,
            {
                (
                    "operation:project:default:query:Users",
                    "fragment:project:default:fragment:UserFields",
                ),
                (
                    "operation:project:default:query:Users~2",
                    "fragment:project:default:fragment:UserFields~2",
                ),
            },
        )

    def test_output_file_leaves_stdout_free_and_cannot_replace_source(self) -> None:
        output_path = self.root / "scan.json"
        code, output, error = self.invoke(
            ["scan", "--format", "json", "--output", str(output_path)]
        )
        self.assertEqual((code, output, error), (0, "", ""))
        self.assertEqual(
            json.loads(output_path.read_text(encoding="utf-8"))["version"], 1
        )
        before = self.source.read_bytes()
        code, output, error = self.invoke(
            ["scan", "--format", "json", "--output", str(self.source)]
        )
        self.assertEqual((code, output), (1, ""))
        self.assertIn("command failed", error)
        self.assertEqual(self.source.read_bytes(), before)

    def test_pipe_and_output_default_to_json(self) -> None:
        code, output, error = self.invoke(["scan"])
        self.assertEqual((code, error), (0, ""))
        self.assertEqual(json.loads(output)["version"], 1)
        destination = self.root / "scan-default.json"
        code, output, error = self.invoke(["scan", "--output", str(destination)])
        self.assertEqual((code, output, error), (0, "", ""))
        self.assertEqual(
            json.loads(destination.read_text(encoding="utf-8"))["version"], 1
        )

    def test_explicit_text_format_is_honored_for_pipe_and_output(self) -> None:
        code, output, error = self.invoke(["scan", "--format", "text"])
        self.assertEqual((code, error), (0, ""))
        self.assertTrue(output.startswith("GraphQL Desk scan"))
        destination = self.root / "scan.txt"
        code, output, error = self.invoke(
            ["scan", "-f", "text", "--output", str(destination)]
        )
        self.assertEqual((code, output, error), (0, "", ""))
        self.assertTrue(
            destination.read_text(encoding="utf-8").startswith("GraphQL Desk scan")
        )

    def test_fail_on_warn_keeps_warnings_in_machine_output(self) -> None:
        (self.root / "broken.py").write_text("def broken(:\n", encoding="utf-8")
        code, output, _error = self.invoke(["scan", "-f", "json", "-w"])
        self.assertEqual(code, 1)
        payload = json.loads(output)
        self.assertTrue(payload["warnings"])
        self.assertEqual(payload["warnings"][0]["file"], "broken.py")

    def test_scan_reads_python_encoding_cookie_and_sanitizes_invalid_encoding(
        self,
    ) -> None:
        self.source.write_bytes(
            b"# coding: latin-1\nlabel = 'caf\xe9'\n"
            b"from graphql_desk_generated.default import graphql\n"
            b"document = graphql('query Users { users { name } }')\n"
        )
        self.assertEqual(self.invoke(["scan", "--format", "json"])[0], 0)
        self.source.write_bytes(b"\xff\xfe\xf8\n")
        code, output, error = self.invoke(["scan", "--format", "json"])
        self.assertEqual((code, output), (1, ""))
        self.assertIn("command failed", error)
        self.assertNotIn("Traceback", error)

    def test_terminal_input_and_directive_findings_have_locations(self) -> None:
        (self.root / "schema.graphql").write_text(
            "directive @audit on FIELD "
            "input UserFilter { name: String } "
            "type Query { users(filter: UserFilter): [User!]! } "
            "type User { name: String! id: ID! }",
            encoding="utf-8",
        )
        self.source.write_text(
            "from graphql_desk_generated.default import graphql\n"
            "document = graphql('query Users { "
            'users(filter: {name: \\"A\\"}) @audit { name } }\')\n',
            encoding="utf-8",
        )
        code, output, error = self.invoke(["scan"], terminal=True)
        self.assertEqual((code, error), (0, ""))
        input_line = next(
            line for line in output.splitlines() if "UserFilter.name" in line
        )
        directive_line = next(line for line in output.splitlines() if "audit" in line)
        self.assertIn("app.py:", input_line)
        self.assertIn("app.py:", directive_line)
        code, output, error = self.invoke(["scan", "--format", "json"])
        self.assertEqual((code, error), (0, ""))
        rules = json.loads(output)["rules"]
        input_data = next(
            item["data"]
            for item in rules["input-usage"]
            if item["ref"]["typeName"] == "UserFilter"
        )
        directive_data = rules["directive-usage"][0]["data"]
        self.assertEqual(set(input_data), {"kind", "count"})
        self.assertEqual(set(directive_data), {"count"})

    def test_terminal_report_includes_rule_and_coverage(self) -> None:
        code, output, error = self.invoke(["scan"], terminal=True)
        self.assertEqual((code, error), (0, ""))
        self.assertIn("67%", output)
        self.assertIn("operation-complexity", output)
        self.assertIn("Users", output)
        self.assertIn("app.py:", output)

    def test_named_schema_scan_keeps_coordinates_isolated(self) -> None:
        (self.root / "first.graphql").write_text(
            "type Query { item: String }", encoding="utf-8"
        )
        (self.root / "second.graphql").write_text(
            "type Query { item: Int }", encoding="utf-8"
        )
        (self.root / "pyproject.toml").write_text(
            "[[tool.graphql-desk.schemas]]\nname='first'\n"
            "schema='first.graphql'\n"
            "schema_output='graphql_desk_generated/first.pyi'\n"
            "[[tool.graphql-desk.schemas]]\nname='second'\n"
            "schema='second.graphql'\n"
            "schema_output='graphql_desk_generated/second.pyi'\n",
            encoding="utf-8",
        )
        self.source.write_text(
            "from graphql_desk_generated.first import graphql as first\n"
            "from graphql_desk_generated.second import graphql as second\n"
            "one = first('query One { item }')\n"
            "two = second('query Two { item }')\n",
            encoding="utf-8",
        )
        with patch.object(Path, "cwd", return_value=self.root):
            report = scan_project()
        points = report.rules["field-usage"]
        self.assertEqual(
            {item["ref"]["schemaName"] for item in points}, {"first", "second"}
        )
        self.assertEqual(
            {
                name: value["percent"]
                for name, value in report.overview["coverageBySchema"].items()
            },
            {"first": 100, "second": 100},
        )
        code, output, error = self.invoke(
            ["scan", "--schema", "first", "--format", "json"]
        )
        self.assertEqual((code, error), (0, ""))
        self.assertEqual(
            {item["schemaName"] for item in json.loads(output)["operations"]},
            {"first"},
        )

    def test_explicit_external_source_root_uses_nonabsolute_report_paths(self) -> None:
        external_temp = tempfile.TemporaryDirectory(
            prefix="graphql-desk-external-", dir=self.root.parent
        )
        self.addCleanup(external_temp.cleanup)
        external = Path(external_temp.name)
        (external / "outside.py").write_text(
            "from graphql_desk_generated.default import graphql\n"
            "document = graphql('query Outside { users { name } }')\n",
            encoding="utf-8",
        )
        (self.root / "pyproject.toml").write_text(
            '[tool.graphql-desk]\nschema="schema.graphql"\n'
            'schema_output="graphql_desk_generated/default.pyi"\n'
            f'source_roots=["../{external.name}"]\n',
            encoding="utf-8",
        )
        with patch.object(Path, "cwd", return_value=self.root):
            report = scan_project(config=self.root / "pyproject.toml")
        location = report.operations[0]["loc"]
        self.assertEqual(location["file"], "external-source-root-1/outside.py")
        self.assertNotIn(str(external), json_report(report))


if __name__ == "__main__":
    unittest.main()
