import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from graphql import (
    GraphQLInputField,
    GraphQLInputObjectType,
    GraphQLInt,
    GraphQLNonNull,
    build_schema,
)

from graphql_desk.schema_types import render_schema_types

_SCHEMA = """
    enum Color { RED BLUE }
    input Filter {
        id: ID!
        color: Color
        child: Filter
        a: [String]
        b: [String!]
        c: [String]!
        d: [String!]!
        defaulted: Int! = 10
        nullableDefault: Int = 10
        nullDefault: Int = null
        class: Boolean
    }
    type Query { search(filter: Filter): String }
"""


class SchemaTypesTests(unittest.TestCase):
    def test_wrappers_defaults_and_keyword_fields(self) -> None:
        output = render_schema_types(build_schema(_SCHEMA))
        for expected in (
            "Color = Literal['BLUE', 'RED']",
            "'id': 'ID'",
            "'child': NotRequired['Filter | None']",
            "'a': NotRequired['list[String | None] | None']",
            "'b': NotRequired['list[String] | None']",
            "'c': 'list[String | None]'",
            "'d': 'list[String]'",
            "'defaulted': NotRequired['Int | None']",
            "'nullableDefault': NotRequired['Int | None']",
            "'class': NotRequired['Boolean | None']",
        ):
            with self.subTest(expected=expected):
                self.assertIn(expected, output)

    def test_programmatic_default_is_optional_and_nullable(self) -> None:
        schema = build_schema(_SCHEMA)
        input_type = schema.get_type("Filter")
        assert isinstance(input_type, GraphQLInputObjectType)
        input_type.fields["legacy"] = GraphQLInputField(
            GraphQLNonNull(GraphQLInt), default_value=10
        )
        self.assertIn(
            "'legacy': NotRequired['Int | None']", render_schema_types(schema)
        )

    def test_order_is_deterministic(self) -> None:
        first = "enum E { B A } input I { b: Int a: String }"
        second = "input I { a: String b: Int } enum E { A B }"
        query = " type Query { f(i: I): E }"
        self.assertEqual(
            render_schema_types(build_schema(first + query)),
            render_schema_types(build_schema(second + query)),
        )

    def test_unsupported_types_and_colliding_names_fail(self) -> None:
        for definition in (
            "scalar Date type Query { f: Date }",
            "input I @oneOf { x: Int } type Query { f(i: I): Int }",
            "input class { x: Int } type Query { f(i: class): Int }",
            "enum Literal { A } type Query { f: Literal }",
            "input str { x: Int } type Query { f(i: str): Int }",
        ):
            with self.subTest(definition=definition), self.assertRaises(ValueError):
                render_schema_types(build_schema(definition))

    def test_generated_module_imports_and_passes_checkers(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            generated = root / "generated.py"
            generated.write_text(render_schema_types(build_schema(_SCHEMA)))
            spec = importlib.util.spec_from_file_location("generated", generated)
            assert spec is not None and spec.loader is not None
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            self.assertIs(module.ID, str)
            self.assertEqual(module.Filter.__required_keys__, {"id", "c", "d"})
            self.assertIn("defaulted", module.Filter.__optional_keys__)
            fixture = root / "fixture.py"
            fixture.write_text(
                "from generated import Filter\n"
                "good: Filter = {'id': '1', 'c': [None], 'd': [], "
                "'child': {'id': '2', 'c': [], 'd': []}, 'defaulted': None}\n"
                "bad_missing: Filter = {'c': [], 'd': []}\n"
                "bad_enum: Filter = {'id': '1', 'c': [], 'd': [], 'color': 'GREEN'}\n"
                "bad_null: Filter = {'id': None, 'c': [], 'd': []}\n"
                "bad_list: Filter = {'id': '1', 'c': [], 'd': [None]}\n"
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
                {item["range"]["start"]["line"] for item in diagnostics},
                {2, 3, 4, 5},
            )
