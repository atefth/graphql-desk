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
    def test_abstract_variants_preserve_selection_scope_and_discriminators(
        self,
    ) -> None:
        schema = build_schema(
            "interface Node { id: ID! label: String next: Node } "
            "type User implements Node { "
            "id: ID! label: String! next: User name: String! } "
            "type Org implements Node { "
            "id: ID! label: String next: Node title: String! } "
            "union Search = User | Org "
            "type Query { node: Node search: [Search!]! }"
        )
        output = render_operation_types(
            schema,
            "{ node { kind: __typename id label ... on User { strong: label name } "
            "... on Org { title } next { __typename id } } "
            "search { ... on Node { id } ... on User { ... { name } } } }",
            name="Read",
        )
        self.assertIn("'kind': \"Literal['User']\"", output)
        self.assertIn("'kind': \"Literal['Org']\"", output)
        self.assertIn("'__typename': NotRequired[\"Literal['User']\"]", output)
        self.assertIn("'label': 'String | None'", output)
        self.assertIn("'strong': 'String'", output)
        self.assertIn("'search': 'list[ReadResult_search]'", output)
        self.assertIn("'__typename': \"Literal['User']\"", output)
        self.assertIn("'name': 'String'", output)
        self.assertEqual(
            render_operation_types(schema, "{ node { ... { id } } }", name="Read"),
            render_operation_types(schema, "{ node { id } }", name="Read"),
        )
        merged = render_operation_types(
            schema,
            "{ node { next { id } ... on User { next { name } } } }",
            name="Read",
        )
        self.assertIn("'id': 'ID',\n    'name': 'String',", merged)
        for source in (
            "{ node { label ... on User { label } } }",
            "{ node { ... on Query { node { id } } } }",
        ):
            with self.subTest(source=source), self.assertRaises(ValueError):
                render_operation_types(schema, source, name="Read")

    def test_abstract_runtime_metadata_does_not_add_response_keys(self) -> None:
        from graphql import graphql_sync  # pyright: ignore[reportUnknownVariableType]

        schema = build_schema(
            "interface Node { id: ID! } type User implements Node { id: ID! } "
            "type Query { node: Node! }"
        )
        source = "{ node { kind: __typename id } }"
        result = graphql_sync(
            schema, source, root_value={"node": {"__typename": "User", "id": "1"}}
        )
        self.assertIsNone(result.errors)
        self.assertEqual(result.data, {"node": {"kind": "User", "id": "1"}})
        alias_source = "{ node { __typename: id kind: __typename } }"
        alias_result = graphql_sync(
            schema,
            alias_source,
            root_value={"node": {"__typename": "User", "id": "1"}},
        )
        self.assertIsNone(alias_result.errors)
        self.assertEqual(
            alias_result.data, {"node": {"__typename": "1", "kind": "User"}}
        )
        alias_output = render_operation_types(schema, alias_source, name="Alias")
        self.assertIn("'__typename': 'ID'", alias_output)
        self.assertNotIn("NotRequired[", alias_output)
        output = render_operation_types(schema, source, name="Read")
        with tempfile.TemporaryDirectory() as directory:
            generated = Path(directory) / "generated.py"
            generated.write_text(output, encoding="utf-8")
            spec = importlib.util.spec_from_file_location("generated", generated)
            assert spec is not None and spec.loader is not None
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            branch = module.ReadResult_node
            self.assertEqual(branch.__required_keys__, {"kind", "id"})
            self.assertEqual(branch.__optional_keys__, {"__typename"})

    def test_abstract_checker_narrowing_and_uninhabited_outputs(self) -> None:
        schema = build_schema(
            "interface Node { id: ID! } "
            "type User implements Node { id: ID! name: String! } "
            "type Org implements Node { id: ID! title: String! } "
            "interface Empty { id: ID! } enum Never { VALUE } "
            "type Query { node: Node! empty: Empty empties: [Empty!]! }"
        )
        output = render_operation_types(
            schema,
            "{ node { kind: __typename ... on User { name } ... on Org { title } } "
            "empty { id } empties { id } }",
            name="Read",
        )
        self.assertIn("Never as __graphql_desk_Never", output)
        self.assertIn("ReadResult_empty = __graphql_desk_Never", output)
        self.assertIn("'empty': 'ReadResult_empty | None'", output)
        self.assertIn("'empties': 'list[ReadResult_empties]'", output)
        fixture = (
            "from generated import ReadResult\n"
            "def narrow(result: ReadResult) -> str:\n"
            "    node = result['node']\n"
            "    if node['kind'] == 'User':\n"
            "        return node['name']\n"
            "    return node['title']\n"
            "good: ReadResult = {'node': {'kind': 'User', 'name': 'x'}, "
            "'empty': None, 'empties': []}\n"
            "bad_branch: ReadResult = {'node': {'kind': 'User', 'title': 'x'}, "
            "'empty': None, 'empties': []}\n"
            "bad_empty: ReadResult = {'node': {'kind': 'Org', 'title': 'x'}, "
            "'empty': {}, 'empties': []}\n"
            "bad_list: ReadResult = {'node': {'kind': 'Org', 'title': 'x'}, "
            "'empty': None, 'empties': [{}]}\n"
            "bad_null: ReadResult = {'node': None, 'empty': None, 'empties': []}\n"
            "def aid(result: ReadResult) -> str:\n"
            "    node = result['node']\n"
            "    if '__typename' in node and node['__typename'] == 'User':\n"
            "        return node['name']\n"
            "    return 'other'\n"
            "from aliases import AliasResult\n"
            "alias: AliasResult = {'node': {'kind': 'User', '__typename': '123'}}\n"
            "bad_alias: AliasResult = {'node': {'kind': 'User', '__typename': 1}}\n"
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "generated.py").write_text(output, encoding="utf-8")
            (root / "aliases.py").write_text(
                render_operation_types(
                    schema,
                    "{ node { __typename: id kind: __typename } }",
                    name="Alias",
                ),
                encoding="utf-8",
            )
            (root / "fixture.py").write_text(fixture, encoding="utf-8")
            runtime = subprocess.run(
                [sys.executable, "-c", "import generated"],
                cwd=root,
                capture_output=True,
                text=True,
            )
            self.assertEqual(runtime.returncode, 0, runtime.stderr)
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
            self.assertIn("Found 6 errors in 1 file", mypy.stdout)
            (root / "pyrightconfig.json").write_text(
                json.dumps(
                    {
                        "include": ["fixture.py", "generated.py", "aliases.py"],
                        "pythonVersion": "3.10",
                        "typeCheckingMode": "strict",
                    }
                ),
                encoding="utf-8",
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
            self.assertEqual(len(diagnostics), 5, diagnostics)
            self.assertEqual(
                {item["range"]["start"]["line"] for item in diagnostics},
                {7, 8, 9, 10, 18},
            )

    def test_abstract_mapped_outputs_and_nullable_list_wrappers(self) -> None:
        schema = build_schema(
            "scalar Date interface Node { when: Date } "
            "type User implements Node { when: Date } "
            "interface Empty { id: ID! } "
            "type Query { nodes: [Node] empty: [Empty] }"
        )
        output = render_operation_types(
            schema,
            "{ nodes { when } empty { id } }",
            name="Read",
            scalars=(ScalarMapping("Date", "builtins.str", "datetime.datetime"),),
        )
        self.assertIn("'when': 'DateOutput | None'", output)
        self.assertIn("'nodes': 'list[ReadResult_nodes | None] | None'", output)
        self.assertIn("'empty': 'list[ReadResult_empty | None] | None'", output)

    def test_one_of_operation_defaults_and_graphql_literal_validation(self) -> None:
        schema = build_schema(
            "input Choice @oneOf { text: String count: Int } "
            "type Query { f(choice: Choice): Int }"
        )
        for source in (
            '{ f(choice: {text: "x"}) }',
            "{ f(choice: null) }",
            "query($choice: Choice) { f(choice: $choice) }",
            "query($text: String!) { f(choice: {text: $text}) }",
            'query($choice: Choice = {text: "x"}) { f(choice: $choice) }',
            'query($choice: Choice! = {text: "x"}) { f(choice: $choice) }',
        ):
            with self.subTest(source=source):
                output = render_operation_types(schema, source, name="Run")
                self.assertIn("RunResult =", output)
        self.assertIn(
            "'choice': NotRequired['Choice']",
            render_operation_types(
                schema,
                'query($choice: Choice! = {text: "x"}) { f(choice: $choice) }',
                name="Run",
            ),
        )
        for source in (
            "{ f(choice: {}) }",
            '{ f(choice: {text: "x", count: 1}) }',
            "{ f(choice: {text: null}) }",
            '{ f(choice: {unknown: "x"}) }',
            "query($choice: Choice = {}) { f(choice: $choice) }",
            "query($text: String) { f(choice: {text: $text}) }",
        ):
            with self.subTest(source=source), self.assertRaises(ValueError):
                render_operation_types(schema, source, name="Run")

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
            'fragment F on User { id } query Q { user(id: "1") { ...F } }',
        ):
            with self.subTest(source=source), self.assertRaises(ValueError):
                render_operation_types(schema, source, name="Run")
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
