import json
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from graphql import build_schema, graphql_sync  # pyright: ignore[reportUnknownVariableType]

from graphql_desk.operation_types import render_operation_types


class ConditionalCompositeTypesTests(unittest.TestCase):
    def test_variable_defer_on_inline_and_named_fragments_types_both_shapes(
        self,
    ) -> None:
        schema = build_schema(
            "directive @defer(if: Boolean = true) on INLINE_FRAGMENT | FRAGMENT_SPREAD "
            "type User { id: ID! name: String! title: String! } "
            "type Query { user: User! }"
        )
        source = (
            "query($inline:Boolean!,$named:Boolean!){ user { id "
            "... @defer(if:$inline) { name } ...Later @defer(if:$named) } } "
            "fragment Later on User { title }"
        )
        rendered = render_operation_types(
            schema, source, name="Read", disable_masking=True
        )
        self.assertEqual(rendered.count("ReadResult_user ="), 1)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "generated.py").write_text(rendered, encoding="utf-8")
            (root / "fixture.py").write_text(
                "from generated import ReadResult\n"
                "base: ReadResult = {'user': {'id': '1'}}\n"
                "inline: ReadResult = {'user': {'id': '1', 'name': 'N'}}\n"
                "named: ReadResult = {'user': {'id': '1', 'title': 'T'}}\n"
                "both: ReadResult = {'user': {'id': '1', 'name': 'N', 'title': 'T'}}\n"
                "bad: ReadResult = {'user': {'name': 'N'}}  # BAD\n",
                encoding="utf-8",
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
            self.assertEqual(
                {
                    int(line)
                    for line in re.findall(r"fixture\.py:(\d+): error:", mypy.stdout)
                },
                {6},
                mypy.stdout,
            )

            (root / "pyrightconfig.json").write_text(
                json.dumps(
                    {
                        "include": ["fixture.py", "generated.py"],
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
            self.assertEqual(
                {item["range"]["start"]["line"] + 1 for item in diagnostics},
                {6},
                diagnostics,
            )

    def test_inline_and_named_conditions_merge_repeated_composite_fields_soundly(
        self,
    ) -> None:
        schema = build_schema(
            "interface Node { id: ID! profile: Profile! } "
            "type User implements Node { id: ID! profile: Profile! } "
            "type Org implements Node { id: ID! profile: Profile! } "
            "type Profile { id: ID! first: String! last: String! } "
            "type Query { node: Node! }"
        )
        source = (
            "query($inline:Boolean!,$named:Boolean!){ node { kind:__typename "
            "profile { id } ... on User @include(if:$inline) "
            "{ profile { first } } ...Name @include(if:$named) } } "
            "fragment Name on Node { profile { last } }"
        )
        rendered = render_operation_types(
            schema, source, name="Read", disable_masking=True
        )
        self.assertEqual(rendered.count("ReadResult_node ="), 1)
        self.assertEqual(rendered.count("'first': 'String'"), 2)
        self.assertEqual(rendered.count("'last': 'String'"), 3)
        self.assertGreaterEqual(rendered.count("closed=True"), 8)

        for kind in ("User", "Org"):
            for inline in (False, True):
                for named in (False, True):
                    result = graphql_sync(
                        schema,
                        source,
                        root_value={
                            "node": {
                                "__typename": kind,
                                "id": "n",
                                "profile": {
                                    "id": "p",
                                    "first": "A",
                                    "last": "B",
                                },
                            }
                        },
                        variable_values={"inline": inline, "named": named},
                    )
                    self.assertIsNone(result.errors)
                    expected_profile = {"id": "p"}
                    if inline and kind == "User":
                        expected_profile["first"] = "A"
                    if named:
                        expected_profile["last"] = "B"
                    self.assertEqual(
                        result.data,
                        {
                            "node": {
                                "kind": kind,
                                "profile": expected_profile,
                            }
                        },
                    )

        with self.assertRaises(ValueError):
            render_operation_types(
                schema,
                source.replace("if:$named", 'if:"yes"'),
                name="Read",
                disable_masking=True,
            )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "generated.py").write_text(rendered, encoding="utf-8")
            fixture = "\n".join(
                (
                    "from generated import ReadResult",
                    "base_user: ReadResult = {",
                    "    'node': {'kind': 'User', 'profile': {'id': 'p'}}",
                    "}",
                    "named_user: ReadResult = {",
                    "    'node': {'kind': 'User', 'profile': {'id': 'p', 'last': 'B'}}",
                    "}",
                    "inline_user: ReadResult = {",
                    "    'node': {'kind': 'User', 'profile': "
                    "{'id': 'p', 'first': 'A'}}",
                    "}",
                    "both_user: ReadResult = {",
                    "    'node': {'kind': 'User', 'profile': "
                    "{'id': 'p', 'first': 'A', 'last': 'B'}}",
                    "}",
                    "base_org: ReadResult = {",
                    "    'node': {'kind': 'Org', 'profile': {'id': 'p'}}",
                    "}",
                    "named_org: ReadResult = {",
                    "    'node': {'kind': 'Org', 'profile': {'id': 'p', 'last': 'B'}}",
                    "}",
                    "missing_id: ReadResult = "
                    "{'node': {'kind': 'User', 'profile': {}}}  # BAD",
                    "wrong_concrete_field: ReadResult = "
                    "{'node': {'kind': 'Org', 'profile': "
                    "{'id': 'p', 'first': 'A'}}}  # BAD",
                    "unknown_child: ReadResult = "
                    "{'node': {'kind': 'User', 'profile': "
                    "{'id': 'p', 'unknown': 'x'}}}  # BAD",
                )
            )
            (root / "fixture.py").write_text(fixture + "\n", encoding="utf-8")

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
            self.assertEqual(
                {
                    int(line)
                    for line in re.findall(r"fixture\.py:(\d+): error:", mypy.stdout)
                },
                {20, 21, 22},
                mypy.stdout,
            )

            (root / "pyrightconfig.json").write_text(
                json.dumps(
                    {
                        "include": ["fixture.py", "generated.py"],
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
            self.assertEqual(
                {item["range"]["start"]["line"] + 1 for item in diagnostics},
                {20, 21, 22},
                diagnostics,
            )
