import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from graphql import build_schema

from graphql_desk.config import ScalarMapping
from graphql_desk.operation_types import render_operation_types

_SCHEMA = """
    enum Color { RED BLUE }
    input Filter { color: Color threshold: Int! = 10 }
    type User {
        id: ID! name: String color: Color!
        friends: [User!]! a: [String] b: [String!] c: [String]! d: [String!]!
    }
    type Query {
        user(id: ID!, limit: Int, filter: Filter): User
        greeting: String!
    }
    type Mutation { update: User! }
    type Subscription { events: [User!]! }
"""
_SOURCE = """
    query Book($id: ID!, $limit: Int! = 1, $filter: Filter) {
        user(id: $id, limit: $limit, filter: $filter) {
            key: id name color __typename friends { id } a b c d
        }
        user(id: $id, limit: $limit, filter: $filter) { friends { name } }
    }
"""


class OperationTypesTests(unittest.TestCase):
    def test_mapped_types_runtime_and_checker_boundaries(self) -> None:
        schema = build_schema(
            "scalar Date scalar Loose scalar class scalar Literal scalar list "
            "enum Color { RED BLUE } "
            "input Filter { when: Date! nested: [Filter!] color: Color dates: [Date] } "
            "type Query { today(when: Date!, filter: Filter): Date! "
            "color: Color! all: [Date!]! }"
        )
        source = (
            "query Work($when: Date!, $filter: Filter) { "
            "today(when: $when, filter: $filter) color all }"
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            marker = root / "imported.txt"
            (root / "domain.py").write_text(
                "from enum import Enum\nfrom pathlib import Path\n"
                "Path('imported.txt').write_text('executed')\n"
                "class Color(Enum):\n RED = 'RED'\n BLUE = 'BLUE'\n"
            )
            output = render_operation_types(
                schema,
                source,
                name="Mapped",
                scalars=(
                    ScalarMapping("Date", "builtins.str", "datetime.datetime"),
                    ScalarMapping("Loose", "typing.Any", "typing.Any"),
                    ScalarMapping("class", "builtins.str", "builtins.str"),
                    ScalarMapping("Literal", "builtins.str", "builtins.str"),
                    ScalarMapping("list", "builtins.str", "builtins.str"),
                ),
                enums=(("Color", "domain.Color"),),
            )
            self.assertFalse(
                marker.exists(), "Rendering must not execute configured modules"
            )
            self.assertIn("'when': 'DateInput'", output)
            self.assertIn("'today': 'DateOutput'", output)
            self.assertIn("'all': 'list[DateOutput]'", output)
            self.assertIn(
                "'dates': NotRequired['list[DateInput | None] | None']", output
            )
            (root / "generated.py").write_text(output)
            runtime = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    "import generated; from datetime import datetime; "
                    "assert generated.DateInput is str; "
                    "assert generated.DateOutput is datetime; "
                    "assert generated.classInput is str; "
                    "assert generated.LiteralOutput is str; "
                    "assert generated.listOutput is str; "
                    "assert generated.Color.RED.value == 'RED'",
                ],
                cwd=root,
                capture_output=True,
                text=True,
            )
            self.assertEqual(runtime.returncode, 0, runtime.stderr)
            self.assertTrue(
                marker.exists(),
                "Only importing generated declarations executes their imports",
            )
            (root / "fixture.py").write_text(
                "from datetime import datetime\n"
                "from generated import MappedVariables, MappedResult, Color\n"
                "good_vars: MappedVariables = {'when': 'x', "
                "'filter': {'when': 'x', 'color': Color.RED}}\n"
                "good_result: MappedResult = {'today': datetime(2020, 1, 1), "
                "'color': Color.RED, 'all': []}\n"
                "bad_vars: MappedVariables = {'when': datetime(2020, 1, 1)}\n"
                "bad_result: MappedResult = {'today': 'x', "
                "'color': Color.RED, 'all': []}\n"
                "bad_enum: MappedVariables = {'when': 'x', "
                "'filter': {'when': 'x', 'color': 'RED'}}\n"
                "bad_null: MappedVariables = {'when': None}\n"
            )
            mypy = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "mypy",
                    "--strict",
                    "--no-incremental",
                    str(root),
                ],
                cwd=root,
                capture_output=True,
                text=True,
            )
            self.assertEqual(mypy.returncode, 1, mypy.stdout + mypy.stderr)
            self.assertIn("Found 4 errors in 1 file", mypy.stdout)
            (root / "pyrightconfig.json").write_text(
                json.dumps(
                    {
                        "include": ["fixture.py", "generated.py", "domain.py"],
                        "pythonVersion": "3.10",
                        "typeCheckingMode": "strict",
                    }
                )
            )
            pyright = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "pyright",
                    "--project",
                    str(root),
                    "--pythonpath",
                    sys.executable,
                    "--outputjson",
                ],
                cwd=root,
                capture_output=True,
                text=True,
            )
            self.assertEqual(pyright.returncode, 1, pyright.stdout + pyright.stderr)
            diagnostics = json.loads(pyright.stdout)["generalDiagnostics"]
            self.assertEqual(len(diagnostics), 4, diagnostics)
            self.assertEqual(
                {item["range"]["start"]["line"] for item in diagnostics}, {4, 5, 6, 7}
            )

    def test_nested_operation_declaration_cannot_overwrite_mapping_alias(self) -> None:
        schema = build_schema(
            "scalar XResult_ type Child { id: ID! } "
            "type Query { child(v: XResult_!): Child! }"
        )
        with self.assertRaisesRegex(ValueError, "Colliding"):
            render_operation_types(
                schema,
                "query($v: XResult_!) { Output: child(v: $v) { id } }",
                name="X",
                scalars=(ScalarMapping("XResult_", "builtins.str", "builtins.str"),),
            )

    def test_wrappers_aliases_merges_and_variable_defaults(self) -> None:
        output = render_operation_types(build_schema(_SCHEMA), _SOURCE, name="Book")
        for expected in (
            "'user': 'BookResult_user | None'",
            "'key': 'ID'",
            "'name': 'String | None'",
            "'color': 'Color'",
            "\"Literal['User']\"",
            "'friends': 'list[BookResult_user_friends]'",
            "'a': 'list[String | None] | None'",
            "'b': 'list[String] | None'",
            "'c': 'list[String | None]'",
            "'d': 'list[String]'",
            "'limit': NotRequired['Int']",
            "'filter': NotRequired['Filter | None']",
        ):
            with self.subTest(expected=expected):
                self.assertIn(expected, output)
        self.assertEqual(output.count("BookResult_user_friends ="), 1)
        self.assertIn("'id': 'ID'", output)

    def test_query_mutation_subscription_and_operation_selection(self) -> None:
        schema = build_schema(_SCHEMA)
        for source, expected in (
            ("{ greeting }", "'greeting': 'String'"),
            ("mutation { update { id } }", "'update': 'RunResult_update'"),
            ("subscription { events { id } }", "'events': 'list[RunResult_events]'"),
        ):
            with self.subTest(source=source):
                self.assertIn(
                    expected, render_operation_types(schema, source, name="Run")
                )
        source = "query First { greeting } query Second { greeting }"
        with self.assertRaisesRegex(ValueError, "explicitly"):
            render_operation_types(schema, source, name="Run")
        self.assertIn(
            "RunResult =",
            render_operation_types(schema, source, name="Run", operation="Second"),
        )
        with self.assertRaisesRegex(ValueError, "Unknown selected"):
            render_operation_types(schema, source, name="Run", operation="Missing")

    def test_nullable_and_non_null_variable_defaults_stay_distinct(self) -> None:
        output = render_operation_types(
            build_schema(_SCHEMA),
            "query($a:Int=2,$b:Int!=1) { "
            'a:user(id:"1",limit:$a){id} b:user(id:"2",limit:$b){id} }',
            name="Run",
        )
        self.assertIn("'a': NotRequired['Int | None']", output)
        self.assertIn("'b': NotRequired['Int']", output)

    def test_invalid_and_unsupported_documents_fail(self) -> None:
        schema = build_schema(_SCHEMA)
        for source in (
            "{ missing }",
            "{ __schema { queryType { name } } }",
            "query Same { greeting } query Same { greeting }",
            '{ a:greeting a:user(id: "1") { id } }',
            "{ greeting @skip(if: false) }",
            '{ user(id: "1") { ... { id } } }',
            'fragment F on User { id } query Q { user(id: "1") { ...F } }',
        ):
            with self.subTest(source=source), self.assertRaises(ValueError):
                render_operation_types(schema, source, name="Run")
        abstract = build_schema(
            "interface I { id: ID! } type U implements I { id: ID! } "
            "type Query { i: I }"
        )
        with self.assertRaisesRegex(ValueError, "Unsupported output"):
            render_operation_types(abstract, "{ i { id } }", name="Run")
        custom = build_schema("scalar Date type Query { date: Date }")
        with self.assertRaisesRegex(ValueError, "Custom scalar"):
            render_operation_types(custom, "{ date }", name="Run")

    def test_unsafe_and_colliding_names_fail(self) -> None:
        schema = build_schema(_SCHEMA)
        for name in ("class", "bad-name", "__hidden", "", "𝑩ook"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                render_operation_types(schema, "{ greeting }", name=name)
        collision = build_schema("enum RunResult { A } type Query { value: RunResult }")
        with self.assertRaisesRegex(ValueError, "Colliding"):
            render_operation_types(collision, "{ value }", name="Run")
        nested = build_schema(
            "type N { b: N a_b: N a: N value: String } type Query { n: N }"
        )
        with self.assertRaisesRegex(ValueError, "Colliding"):
            render_operation_types(
                nested, "{ n { a_b { value } a { b { value } } } }", name="Run"
            )

    def test_unicode_normalization_cannot_overwrite_schema_declarations(self) -> None:
        schema = build_schema(
            "input BookResult { x: Int } type Query { value(input: BookResult): Int }"
        )
        with self.assertRaisesRegex(ValueError, "Unsafe Python operation name"):
            render_operation_types(
                schema, "query($v:BookResult){value(input:$v)}", name="𝑩ook"
            )
        self.assertIn(
            "CaféResult =",
            render_operation_types(schema, "{ value }", name="Café"),
        )

    def test_output_is_deterministic_and_preserves_keyword_keys(self) -> None:
        schema = build_schema(_SCHEMA)
        self.assertEqual(
            render_operation_types(schema, '{ user(id: "1") { id name } }', name="Run"),
            render_operation_types(schema, '{ user(id: "1") { name id } }', name="Run"),
        )
        self.assertIn(
            "'class': 'String'",
            render_operation_types(schema, "{ class:greeting }", name="Run"),
        )

    def test_runtime_and_generated_positive_negative_checker_contracts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            generated = root / "generated.py"
            generated.write_text(
                render_operation_types(build_schema(_SCHEMA), _SOURCE, name="Book")
            )
            spec = importlib.util.spec_from_file_location("generated", generated)
            assert spec is not None and spec.loader is not None
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            self.assertEqual(module.BookVariables.__required_keys__, {"id"})
            self.assertEqual(
                module.BookVariables.__optional_keys__, {"limit", "filter"}
            )
            self.assertEqual(
                module.BookResult_user_friends.__required_keys__, {"id", "name"}
            )
            fixture = root / "fixture.py"
            fixture.write_text(
                "from generated import (BookResult, BookVariables, "
                "BookResult_user_friends)\n"
                "ok: BookVariables = {'id':'1', 'filter': {'threshold': None}}\n"
                "result: BookResult = {'user': None}\n"
                "friend: BookResult_user_friends = {'id':'1','name':None}\n"
                "missing: BookVariables = {}\n"
                "null: BookVariables = {'id':'1','limit':None}\n"
                "enum: BookVariables = {'id':'1','filter':{'color':'GREEN'}}\n"
                "wrong: BookResult_user_friends = {'id':None,'name':None}\n"
            )
            mypy = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "mypy",
                    "--strict",
                    "--no-incremental",
                    "--python-version",
                    "3.10",
                    str(root),
                ],
                cwd=root,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(mypy.returncode, 1, mypy.stdout + mypy.stderr)
            self.assertIn("Found 4 errors in 1 file", mypy.stdout)
            config = root / "pyrightconfig.json"
            config.write_text(
                json.dumps(
                    {
                        "include": ["generated.py", "fixture.py"],
                        "pythonVersion": "3.10",
                        "typeCheckingMode": "strict",
                    }
                )
            )
            pyright = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "pyright",
                    "--project",
                    str(config),
                    "--outputjson",
                ],
                cwd=root,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(pyright.returncode, 1, pyright.stdout + pyright.stderr)
            diagnostics = json.loads(pyright.stdout)["generalDiagnostics"]
            self.assertEqual(len(diagnostics), 4, diagnostics)
            self.assertEqual(
                {item["range"]["start"]["line"] for item in diagnostics}, {4, 5, 6, 7}
            )
