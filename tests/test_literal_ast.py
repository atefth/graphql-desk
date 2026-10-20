"""Literal AST annotations retain the real parser's shapes and consumers."""

import ast
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from graphql import FragmentDefinitionNode, parse

from graphql_desk import gql
from graphql_desk.literal_ast import render_document_ast_types, render_literal_ast_types

_SOURCE = """query Read(
  $v:[Int!]=[1,2] @var(flag:{nested:[true,null]})
  $obj:Input={a:[1,true,null,BLUE,1.5,"s",{nested:[2]}]}
)
@tag(x:{a:true,b:null,c:BLUE,d:1.5,e:"hello"}) {
  alias:user(id:$v) { name ... on User { id } ...F }
}
fragment F on User { label }
"""


class LiteralAstTests(unittest.TestCase):
    def test_existing_composed_runtime_ast_retains_empty_directive_tuples(self) -> None:
        fields = gql("fragment Fields on Book @_unmask { title }")
        other = gql("fragment Other on Book { id }")
        document = gql("query Q {book {...Fields ...Other}}", (fields, other))
        original_definitions = document.ast.definitions
        stripped = original_definitions[1]
        ordinary = original_definitions[2]
        assert isinstance(stripped, FragmentDefinitionNode)
        assert isinstance(ordinary, FragmentDefinitionNode)
        self.assertEqual(stripped.directives, ())
        self.assertIsNone(ordinary.directives)
        output = render_document_ast_types(document.ast, name="Exact")
        self.assertIs(document.ast.definitions, original_definitions)
        self.assertIs(document.ast.definitions[1], fields.ast.definitions[0])
        self.assertIn("directives: tuple[()]", output)
        self.assertIn("directives: None", output)
        for invalid in ("class", "__Unsafe"):
            with self.assertRaises(ValueError):
                render_document_ast_types(document.ast, name=invalid)
        with self.assertRaisesRegex(ValueError, "executable"):
            render_document_ast_types(parse("scalar Date"), name="Exact")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "exact.pyi").write_text(output, encoding="utf8")
            (root / "exact.py").write_text("", encoding="utf8")
            (root / "fixture.py").write_text(
                "from typing import Literal, cast\n"
                "from typing_extensions import assert_type\n"
                "from exact import ExactDocument\n"
                "value = cast(ExactDocument, None)\n"
                "assert_type(value.definitions[0].name.value, Literal['Q'])\n"
                "assert_type(value.definitions[1].name.value, Literal['Fields'])\n"
                "assert_type(value.definitions[1].directives, tuple[()])\n"
                "assert_type(value.definitions[2].directives, None)\n",
                encoding="utf8",
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
            self.assertEqual(mypy.returncode, 0, mypy.stdout + mypy.stderr)
            (root / "pyrightconfig.json").write_text(
                json.dumps(
                    {
                        "include": ["."],
                        "typeCheckingMode": "strict",
                        "pythonVersion": "3.10",
                    }
                ),
                encoding="utf8",
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
            self.assertEqual(pyright.returncode, 0, pyright.stdout + pyright.stderr)

    def test_executable_shapes_are_deterministic_and_schema_agnostic(self) -> None:
        output = render_literal_ast_types(_SOURCE, name="Read")
        self.assertEqual(output, render_literal_ast_types(_SOURCE, name="Read"))
        ast.parse(output, feature_version=(3, 10))
        for fragment in (
            "class ReadDocument(_ast.DocumentNode)",
            "_Literal['document']",
            "_Literal['operation_definition']",
            "_Literal['fragment_definition']",
            "_Literal['fragment_spread']",
            "_Literal['inline_fragment']",
            "_Literal['variable_definition']",
            "_Literal['list_type']",
            "_Literal['non_null_type']",
            "_Literal['object_value']",
            "_Literal['null_value']",
            "_Literal['BLUE']",
            "_Literal['1.5']",
            "_Literal[True]",
            "_Literal['alias']",
            "_Literal[_ast.OperationType.QUERY]",
            "loc: _ast.Location",
            "directives: None",
            "arguments: None",
        ):
            self.assertIn(fragment, output)
        self.assertNotIn("Any", output)
        self.assertNotIn("start:", output)
        self.assertNotIn("end:", output)

    def test_parse_options_and_real_attribute_values(self) -> None:
        for operation in ("query", "mutation", "subscription"):
            source = operation + " Named { value }"
            for no_location in (False, True):
                with self.subTest(operation=operation, no_location=no_location):
                    document = parse(source, no_location=no_location)
                    output = render_literal_ast_types(
                        source, name="Read", no_location=no_location
                    )
                    self.assertIn(
                        f"_Literal[_ast.OperationType.{operation.upper()}]", output
                    )
                    self.assertIn(
                        "loc: None" if document.loc is None else "loc: _ast.Location",
                        output,
                    )
                    self.assertEqual(
                        document.definitions[0].kind, "operation_definition"
                    )
        output = render_literal_ast_types("{ field(arg:[]) }", name="Read")
        self.assertIn("values: tuple[()]", output)
        self.assertIn(
            "block: _Literal[False]",
            render_literal_ast_types('{ field(arg:"text") }', name="Read"),
        )
        self.assertIn(
            "block: _Literal[True]",
            render_literal_ast_types('{ field(arg:"""text""") }', name="Read"),
        )

    def test_names_and_unsupported_definitions_fail_clearly(self) -> None:
        for name in ("", "class", "Read.Name", "__Read", "𝑩ook", "bad-name"):
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, "Unsafe"):
                render_literal_ast_types("{field}", name=name)
        self.assertIn(
            "class CaféDocument", render_literal_ast_types("{field}", name="Café")
        )
        for source in (
            "type User { id: ID }",
            "scalar Date",
            "extend type Query{x:Int}",
        ):
            with (
                self.subTest(source=source),
                self.assertRaisesRegex(ValueError, "executable"),
            ):
                render_literal_ast_types(source, name="Read")

    def test_excessive_nesting_fails_without_partial_declarations(self) -> None:
        source = "{" + "field {" * 1000 + "leaf" + "}" * 1001
        with self.assertRaisesRegex(ValueError, "nesting limits"):
            render_literal_ast_types(source, name="Read")

    def test_generated_stubs_check_and_real_ast_identity_is_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "generated.pyi").write_text(
                render_literal_ast_types(_SOURCE, name="Read"), encoding="utf-8"
            )
            # The future generator supplies runtime bindings, not these classes.
            (root / "generated.py").write_text("", encoding="utf-8")
            (root / "unlocated.pyi").write_text(
                render_literal_ast_types("{value}", name="NoLoc", no_location=True),
                encoding="utf-8",
            )
            (root / "unlocated.py").write_text("", encoding="utf-8")
            fixture = """from typing import TYPE_CHECKING, Literal, cast
from typing_extensions import assert_type
from graphql import DocumentNode, Node, OperationType, parse, print_ast
from graphql import ListValueNode, OperationDefinitionNode
if TYPE_CHECKING:
    from generated import ReadDocument
    from unlocated import NoLocDocument
original = parse(SOURCE)
node = cast("ReadDocument", original)
assert node is original
assert_type(node.kind, Literal["document"])
assert_type(node.definitions[0].operation, Literal[OperationType.QUERY])
assert_type(node.definitions[0].name.value, Literal["Read"])
field = node.definitions[0].selection_set.selections[0]
assert_type(field.alias.value, Literal["alias"])
assert_type(node.definitions[0].selection_set.selections[0].name.value, Literal["user"])
default = node.definitions[0].variable_definitions[0].default_value
assert_type(default.values[0].value, Literal["1"])
mixed = node.definitions[0].variable_definitions[1].default_value.fields[0].value
assert_type(mixed.values[0].value, Literal["1"])
assert_type(mixed.values[1].value, Literal[True])
assert_type(mixed.values[2].kind, Literal["null_value"])
assert_type(mixed.values[3].value, Literal["BLUE"])
assert_type(mixed.values[4].value, Literal["1.5"])
assert_type(mixed.values[5].value, Literal["s"])
assert_type(mixed.values[6].fields[0].value.values[0].value, Literal["2"])
const = node.definitions[0].variable_definitions[0].directives[0].arguments[0].value
assert_type(const.fields[0].value.values[0].value, Literal[True])
fields = node.definitions[0].directives[0].arguments[0].value.fields
assert_type(fields[0].value.value, Literal[True])
assert_type(fields[1].value.kind, Literal["null_value"])
assert_type(node.definitions[1].type_condition.name.value, Literal["User"])
assert_type(node.loc.start, int)
def consume(value: Node) -> None:
    assert value is original
consume(node)
def unchanged(value: DocumentNode) -> None:
    assert type(value) is DocumentNode
    assert value is original
    operation = value.definitions[0]
    assert isinstance(operation, OperationDefinitionNode)
    assert operation.variable_definitions is not None
    assert type(operation.variable_definitions[0].default_value) is ListValueNode
unchanged(node)
print_ast(node)
dynamic = parse("{ other }")
assert_type(dynamic, DocumentNode)
unlocated = cast("NoLocDocument", parse("{value}", no_location=True))
assert_type(unlocated.loc, None)
assert_type(unlocated.definitions[0].selection_set.selections[0].loc, None)
""".replace("SOURCE", repr(_SOURCE))
            (root / "fixture.py").write_text(fixture, encoding="utf-8")
            runtime = subprocess.run(
                [sys.executable, str(root / "fixture.py")],
                cwd=root,
                capture_output=True,
                text=True,
            )
            self.assertEqual(runtime.returncode, 0, runtime.stdout + runtime.stderr)
            (root / "pyrightconfig.json").write_text(
                json.dumps(
                    {
                        "include": ["fixture.py", "generated.pyi", "unlocated.pyi"],
                        "pythonVersion": "3.10",
                        "typeCheckingMode": "strict",
                        "reportIncompatibleVariableOverride": "error",
                    }
                ),
                encoding="utf-8",
            )
            self.checkers(root, success=True)
            negative = (
                'wrong: Literal["other"] = node.definitions[0].name.value\n'
                "outside = node.definitions[2]\n"
                'node.definitions[0].name.value = "changed"\n'
            )
            (root / "fixture.py").write_text(fixture + negative, encoding="utf-8")
            self.checkers(root, success=False, first_bad_line=len(fixture.splitlines()))

    def checkers(self, root: Path, *, success: bool, first_bad_line: int = 0) -> None:
        mypy = subprocess.run(
            [
                sys.executable,
                "-m",
                "mypy",
                "--strict",
                "--python-version",
                "3.10",
                "--no-incremental",
                str(root),
            ],
            cwd=root,
            capture_output=True,
            text=True,
        )
        self.assertEqual(
            mypy.returncode, 0 if success else 1, mypy.stdout + mypy.stderr
        )
        if not success:
            errors = [line for line in mypy.stdout.splitlines() if ": error:" in line]
            self.assertEqual(len(errors), 3, mypy.stdout)
            self.assertEqual(
                {int(line.split(":")[1]) for line in errors},
                set(range(first_bad_line + 1, first_bad_line + 4)),
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
        self.assertEqual(
            pyright.returncode, 0 if success else 1, pyright.stdout + pyright.stderr
        )
        diagnostics = json.loads(pyright.stdout)["generalDiagnostics"]
        self.assertEqual(len(diagnostics), 0 if success else 3, diagnostics)
        if not success:
            self.assertEqual(
                {item["range"]["start"]["line"] for item in diagnostics},
                set(range(first_bad_line, first_bad_line + 3)),
            )


if __name__ == "__main__":
    unittest.main()
